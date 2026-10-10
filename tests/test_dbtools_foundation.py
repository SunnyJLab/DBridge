#!/usr/bin/env python3
"""Foundation unit tests for DB tool guardrails and registry."""

from __future__ import annotations

import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dbtools.errors import (
    BadParamError,
    InvalidSqlError,
    NoAccessError,
    ToolError,
    UnknownObjectError,
    serialize_error,
    user_message,
)
from dbtools.registry import ParamSpec, ToolDefinition, ToolRegistry
from dbtools.sql_safety import (
    authorize_select_sql,
    quote_identifier,
    validate_identifier,
    validate_select_sql,
)


class SqlSafetyTests(unittest.TestCase):
    def test_accepts_select_and_with(self) -> None:
        self.assertEqual(validate_select_sql("SELECT 1 FROM dual"), "SELECT 1 FROM dual")
        self.assertTrue(validate_select_sql("/* mcp */ WITH q AS (SELECT 1 n FROM dual) SELECT n FROM q").startswith("WITH"))

    def test_literals_and_comments_do_not_trigger_keyword_filter(self) -> None:
        sql = "SELECT 'DELETE; DROP TABLE x' value /* UPDATE t */ FROM dual"
        self.assertEqual(validate_select_sql(sql), sql)
        quoted = "SELECT q'[DELETE; DROP]' value FROM dual"
        self.assertEqual(validate_select_sql(quoted), quoted)

    def test_rejects_mutation_and_multiple_statements(self) -> None:
        for sql in (
            "DELETE FROM t",
            "SELECT * FROM t; SELECT * FROM u",
            "WITH q AS (SELECT 1 FROM dual) DELETE FROM t",
            "SELECT * FROM t FOR UPDATE",
        ):
            with self.subTest(sql=sql), self.assertRaises(InvalidSqlError):
                validate_select_sql(sql)

    def test_rejects_unterminated_comment_and_literal(self) -> None:
        for sql in ("/* open SELECT 1 FROM dual", "SELECT 'open FROM dual"):
            with self.subTest(sql=sql), self.assertRaises(InvalidSqlError):
                validate_select_sql(sql)

    def test_identifier_validation_and_quoting(self) -> None:
        self.assertEqual(validate_identifier("dw.f_sales"), "DW.F_SALES")
        self.assertEqual(quote_identifier("dw.f_sales"), '"DW"."F_SALES"')
        with self.assertRaises(BadParamError):
            validate_identifier("DW.F_SALES WHERE 1=1")
        with self.assertRaises(BadParamError):
            validate_identifier('"MixedCase"')


class _FakeCatalog:
    """authorize_select_sql이 필요한 최소 표면. CODES(GRP_CD, CD, CD_NM, USE_YN, SORT_ORD)."""

    TABLES = {
        "COMMON.CODES": ("GRP_CD", "CD", "CD_NM", "USE_YN", "SORT_ORD"),
        "DW.F_SALES": ("BASE_DT", "STAT_CD", "SALE_AMT"),
    }

    def require_object(self, connection: str, table: str):
        columns = self.TABLES.get(table.upper())
        if columns is None:
            raise UnknownObjectError(f"카탈로그에 없는 식별자: {table}")
        owner, name = table.upper().split(".", 1)
        return type("Obj", (), {
            "owner": owner, "name": name, "type": "TABLE",
            "qualified": f"{owner}.{name}",
            "columns": tuple(type("Col", (), {"name": item})() for item in columns),
        })()


class SqlSafetyScopeTests(unittest.TestCase):
    """ORDER BY 스코프·SELECT 별칭·미해석 한정자 진단 (전부 회귀 방지용)."""

    def setUp(self) -> None:
        self.catalog = _FakeCatalog()

    def ok(self, sql: str) -> None:
        authorize_select_sql(sql, self.catalog, "dw")

    def rejected(self, sql: str) -> ToolError:
        with self.assertRaises(ToolError) as caught:
            authorize_select_sql(sql, self.catalog, "dw")
        return caught.exception

    def test_order_by_may_reference_a_column_not_in_the_select_list(self) -> None:
        self.ok("SELECT CD, CD_NM FROM COMMON.CODES ORDER BY SORT_ORD")
        self.ok("SELECT CD FROM COMMON.CODES ORDER BY USE_YN DESC NULLS LAST")
        self.ok("SELECT s.STAT_CD FROM DW.F_SALES s ORDER BY s.BASE_DT")

    def test_order_by_still_resolves_projected_columns_without_ambiguity(self) -> None:
        self.ok("SELECT CD, CD_NM FROM COMMON.CODES ORDER BY CD")

    def test_order_by_may_reference_a_select_alias(self) -> None:
        self.ok("SELECT CD AS C, CD_NM FROM COMMON.CODES ORDER BY C")

    def test_select_list_accepts_column_aliases(self) -> None:
        self.ok("SELECT CD AS C FROM COMMON.CODES")
        self.ok("SELECT a.CD_NM AS FIRST_NAME, b.CD_NM AS SECOND_NAME "
                "FROM COMMON.CODES a JOIN COMMON.CODES b ON b.GRP_CD = a.GRP_CD")

    def test_set_operation_order_by_is_still_output_only(self) -> None:
        self.ok("SELECT CD FROM COMMON.CODES UNION SELECT CD FROM COMMON.CODES ORDER BY CD")
        error = self.rejected(
            "SELECT CD FROM COMMON.CODES UNION SELECT CD FROM COMMON.CODES ORDER BY SORT_ORD")
        self.assertEqual(error.code, "UNKNOWN_OBJECT")

    def test_unknown_names_are_still_rejected(self) -> None:
        for sql in (
            "SELECT NOPE FROM COMMON.CODES",
            "SELECT c.NOPE FROM COMMON.CODES c",
            "SELECT CD FROM COMMON.CODES ORDER BY NOPE",
        ):
            with self.subTest(sql=sql):
                self.assertEqual(self.rejected(sql).code, "UNKNOWN_OBJECT")

    def test_unresolved_qualifier_names_itself_instead_of_blaming_packages(self) -> None:
        error = self.rejected("SELECT x.FOO FROM COMMON.CODES c")
        self.assertEqual(error.code, "UNKNOWN_OBJECT")
        self.assertIn("X.FOO", error.message)


class ErrorTests(unittest.TestCase):
    def test_error_serialization_contract(self) -> None:
        payload = serialize_error(NoAccessError("권한 없음", ora="ORA-00942"))
        self.assertEqual(payload["error"]["code"], "NO_ACCESS")
        self.assertEqual(payload["error"]["ora"], "ORA-00942")
        self.assertIn("hint", payload["error"])

    def test_user_message_hides_driver_and_python_text(self) -> None:
        from dbtools.database import AdapterError, OracleDatabase

        connect = user_message(
            NoAccessError("TNS: no listener Help: https://example", ora="ORA-12541")
        )
        self.assertEqual(
            connect,
            "접속할 수 없습니다. 호스트·서비스명·계정을 확인하세요 (ORA-12541)",
        )
        self.assertNotIn("no listener", connect)
        self.assertNotIn("Help:", connect)

        driver = user_message(AdapterError("oracledb driver is not installed"))
        self.assertEqual(driver, "DB 드라이버를 사용할 수 없습니다")
        self.assertNotIn("oracledb", driver)

        py = user_message(ModuleNotFoundError("No module named 'oracledb'"))
        self.assertEqual(py, "DB 드라이버를 사용할 수 없습니다")
        self.assertNotIn("oracledb", py)
        self.assertNotIn("No module named", py)

        secret = "super-secret-pw"
        masked = user_message(
            RuntimeError(f"connect failed for user READONLY_USER with password {secret}")
        )
        self.assertNotIn(secret, masked)
        self.assertNotIn("READONLY_USER", masked)
        self.assertNotIn("connect failed", masked)

        verbose = user_message(
            AdapterError(
                "DPY-6005: cannot connect to database. Help: https://example",
                ora="DPY-6005",
            )
        )
        self.assertEqual(
            verbose,
            "접속할 수 없습니다. 호스트·서비스명·계정을 확인하세요 (DPY-6005)",
        )

        db = OracleDatabase(
            [{"name": "dw", "dsn": "localhost:1521/WRONG", "user": "u", "password": "p"}]
        )

        def boom(*_a, **_k):
            raise AdapterError("oracledb driver is not installed")

        db.execute = boom  # type: ignore[method-assign]
        status = db._status("dw")
        self.assertEqual(status.state, "error")
        self.assertIsNone(status.error)

        def boom_code(*_a, **_k):
            raise AdapterError("DPY-6001: cannot connect to database. Help: https://example")

        db.execute = boom_code  # type: ignore[method-assign]
        status = db._status("dw")
        self.assertEqual(status.state, "error")
        self.assertEqual(status.error, "DPY-6001")
        self.assertNotIn("cannot connect", status.error or "")

    def test_user_message_classifies_credential_as_connect(self) -> None:
        from dbtools.database import AdapterError

        auth = user_message(
            AdapterError("ORA-01017: invalid credential", ora="ORA-01017")
        )
        self.assertEqual(
            auth,
            "접속할 수 없습니다. 호스트·서비스명·계정을 확인하세요 (ORA-01017)",
        )
        self.assertNotIn("드라이버", auth)

        locked = user_message(
            AdapterError("ORA-28000: account locked", ora="ORA-28000")
        )
        self.assertEqual(
            locked,
            "접속할 수 없습니다. 호스트·서비스명·계정을 확인하세요 (ORA-28000)",
        )


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list[tuple[object, str, int]] = []

        def handler(context: object, *, name: str, limit: int) -> dict[str, object]:
            self.calls.append((context, name, limit))
            return {"name": name, "limit": limit}

        self.registry = ToolRegistry()
        self.registry.register(ToolDefinition(
            name="sample",
            description="테스트 도구",
            params=(
                ParamSpec("name", "string"),
                ParamSpec("limit", "int", required=False, default=10),
            ),
            example={"name": "sample", "arguments": {"name": "x"}},
            handler=handler,
        ))

    def test_dispatch_applies_defaults(self) -> None:
        context = object()
        result = self.registry.dispatch("sample", {"name": "alpha"}, context)
        self.assertEqual(result, {"name": "alpha", "limit": 10})
        self.assertEqual(self.calls, [(context, "alpha", 10)])

    def test_dispatch_rejects_unknown_and_wrong_types(self) -> None:
        with self.assertRaises(BadParamError):
            self.registry.dispatch("sample", {"name": "a", "extra": 1}, object())
        with self.assertRaises(BadParamError):
            self.registry.dispatch("sample", {"name": "a", "limit": True}, object())

    def test_metadata_is_defensively_copied(self) -> None:
        metadata = self.registry.list_metadata()
        metadata[0]["name"] = "changed"
        self.assertEqual(self.registry.list_metadata()[0]["name"], "sample")


if __name__ == "__main__":
    unittest.main()
