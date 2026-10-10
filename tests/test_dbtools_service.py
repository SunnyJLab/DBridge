#!/usr/bin/env python3
"""End-to-end tests for all built-in DB tools using an in-memory Oracle facade."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dbtools.database import AdapterError, ConnectionConfig, ConnectionStatus, QueryResult
from transport.config import validate_config
from dbtools.service import BUILTIN_TOOL_NAMES, ToolService


class FakeOracle:
    def __init__(self) -> None:
        self.configs = {
            "dw": ConnectionConfig("dw", "dw:1521/DW", "READONLY_USER", "DW_PW", 5),
            "src": ConnectionConfig("src", "src:1521/SRC", "READONLY_USER", "SRC_PW", 5),
        }
        self.closed = False

    def connection_names(self) -> tuple[str, ...]:
        return tuple(self.configs)

    def connection_config(self, name: str) -> ConnectionConfig:
        return self.configs[name]

    def snapshot(self) -> tuple[ConnectionStatus, ...]:
        return tuple(ConnectionStatus(name, cfg.dsn, "ok", 2, 0, cfg.pool_max)
                     for name, cfg in self.configs.items())

    def close(self) -> None:
        self.closed = True

    def parse(self, connection: str, sql: str, timeout_s: float | None = None) -> bool:
        return True

    def execute(
        self,
        connection: str,
        sql: str,
        *,
        binds: dict[str, Any] | None = None,
        max_rows: int = 100,
        timeout_s: float | None = None,
    ) -> QueryResult:
        upper = " ".join(sql.upper().split())
        binds = binds or {}

        if upper == "SELECT OWNER, TABLE_NAME, NUM_ROWS, LAST_ANALYZED, PARTITIONED FROM ALL_TABLES":
            return self._tables(connection)
        if upper == "SELECT OWNER, VIEW_NAME FROM ALL_VIEWS":
            return result(("OWNER", "VIEW_NAME"), [])
        if upper.startswith("SELECT OWNER, TABLE_NAME, COLUMN_NAME, DATA_TYPE") and "FROM ALL_TAB_COLUMNS" in upper:
            return self._columns(connection)
        if upper.startswith("SELECT COLUMN_NAME, DATA_TYPE") and "NULLABLE, DATA_DEFAULT" in upper:
            rows = [
                ("BASE_DT", "DATE", 7, None, None, "N", None, 1, 0),
                ("STAT_CD", "VARCHAR2", 2, None, None, "N", None, 2, 2),
                ("PROD_CD", "VARCHAR2", 10, None, None, "N", None, 3, 10),
                ("SALE_AMT", "NUMBER", 22, 15, 2, "Y", None, 4, 0),
            ]
            return result(("COLUMN_NAME", "DATA_TYPE", "DATA_LENGTH", "DATA_PRECISION", "DATA_SCALE", "NULLABLE", "DATA_DEFAULT", "COLUMN_ID", "CHAR_LENGTH"), rows)
        if upper.startswith("SELECT COLUMN_NAME, DATA_TYPE") and "FROM ALL_TAB_COLUMNS" in upper:
            rows = [
                ("BASE_DT", "DATE", 7, None, None, 0),
                ("STAT_CD", "VARCHAR2", 2, None, None, 2),
                ("SALE_AMT", "NUMBER", 22, 15, 2, 0),
            ]
            return result(("COLUMN_NAME", "DATA_TYPE", "DATA_LENGTH", "DATA_PRECISION", "DATA_SCALE", "CHAR_LENGTH"), rows)
        if upper.startswith("SELECT COMMENTS FROM ALL_TAB_COMMENTS"):
            return result(("COMMENTS",), [("매출 팩트",)])
        if upper.startswith("SELECT COLUMN_NAME, COMMENTS FROM ALL_COL_COMMENTS"):
            return result(("COLUMN_NAME", "COMMENTS"), [
                ("BASE_DT", "기준일자"),
                ("STAT_CD", "매출상태코드"),
                ("PROD_CD", "상품코드"),
            ])
        if upper.startswith("SELECT ACC.COLUMN_NAME FROM ALL_CONSTRAINTS"):
            return result(("COLUMN_NAME",), [("BASE_DT",)])
        if upper.startswith("SELECT AI.INDEX_NAME") and "FROM ALL_INDEXES AI" in upper:
            return result(
                ("INDEX_NAME", "UNIQUENESS", "LOCALITY", "COLUMN_NAME", "COLUMN_POSITION"),
                [("IX_F_SALES_01", "NONUNIQUE", "LOCAL", "BASE_DT", 1)],
            )
        if upper.startswith("SELECT PARTITIONING_TYPE FROM ALL_PART_TABLES"):
            return result(("PARTITIONING_TYPE",), [("RANGE",)])
        if upper == "SELECT OWNER, TABLE_NAME, COMMENTS FROM ALL_TAB_COMMENTS":
            return self._table_comments(connection)
        if upper == "SELECT OWNER, TABLE_NAME, COLUMN_NAME, COMMENTS FROM ALL_COL_COMMENTS":
            return self._column_comments(connection)
        if upper.startswith("SELECT OWNER, SYNONYM_NAME") and "FROM ALL_SYNONYMS" in upper:
            rows = [("PUBLIC", "SALES", "DW", "F_SALES", None)] if connection == "dw" else []
            return result(("OWNER", "SYNONYM_NAME", "TABLE_OWNER", "TABLE_NAME", "DB_LINK"), rows)
        if upper.startswith("SELECT OWNER, TABLE_NAME, PARTITIONING_TYPE") and "FROM ALL_PART_TABLES" in upper:
            rows = [("DW", "F_SALES", "RANGE", "NONE", 2, None)] if connection == "dw" else []
            return result(("OWNER", "TABLE_NAME", "PARTITIONING_TYPE", "SUBPARTITIONING_TYPE", "PARTITION_COUNT", "INTERVAL"), rows)
        if "FROM ALL_PART_KEY_COLUMNS" in upper:
            rows = [("DW", "F_SALES", "TABLE", "BASE_DT", 1)] if connection == "dw" else []
            if upper.startswith("SELECT COLUMN_NAME"):
                if binds.get("object_type") != "TABLE":
                    raise AssertionError("partition key lookup must select TABLE objects")
                return result(("COLUMN_NAME",), [(row[3],) for row in rows])
            if "OBJECT_TYPE, COLUMN_NAME" in upper:
                return result(("OWNER", "NAME", "OBJECT_TYPE", "COLUMN_NAME", "COLUMN_POSITION"), rows)
            return result(("OWNER", "NAME", "COLUMN_NAME", "COLUMN_POSITION"),
                          [(owner, name, column, position)
                           for owner, name, _kind, column, position in rows])

        if "FROM ALL_TAB_PARTITIONS" in upper:
            rows = [
                ("F_SALES", "P202608", "TO_DATE('2026-09-01','YYYY-MM-DD')", 1, 3, "2026-09-01"),
                ("F_SALES", "P202609", "TO_DATE('2026-10-01','YYYY-MM-DD')", 2, 0, None),
            ]
            return result(("TABLE_NAME", "PARTITION_NAME", "HIGH_VALUE", "PARTITION_POSITION", "NUM_ROWS", "LAST_ANALYZED"), rows)
        if "FROM ALL_TAB_COL_STATISTICS" in upper:
            rows = [
                ("BASE_DT", 2, None, None, 0, 1, "2026-09-01", 7, "NONE"),
                ("STAT_CD", 3, None, None, 0, 3, "2026-09-01", 2, "FREQUENCY"),
                ("SALE_AMT", 3, None, None, 0, 1, "2026-09-01", 8, "NONE"),
            ]
            return result(("COLUMN_NAME", "NUM_DISTINCT", "LOW_VALUE", "HIGH_VALUE", "NUM_NULLS", "NUM_BUCKETS", "LAST_ANALYZED", "AVG_COL_LEN", "HISTOGRAM"), rows)
        if upper.startswith("SELECT NUM_ROWS, LAST_ANALYZED FROM ALL_TABLES"):
            return result(("NUM_ROWS", "LAST_ANALYZED"), [(3, "2026-09-01")])
        if "FROM ALL_TAB_MODIFICATIONS" in upper:
            return result(("INSERTS", "UPDATES", "DELETES"), [(0, 0, 0)])
        if "FROM ALL_CONSTRAINTS" in upper:
            rows = [("COMMON", "PK_CODE_TABLES", "P", "CODE_TABLES")] if connection == "dw" else []
            return result(("OWNER", "CONSTRAINT_NAME", "CONSTRAINT_TYPE", "TABLE_NAME"), rows)
        if "FROM ALL_CONS_COLUMNS" in upper:
            rows = [
                ("COMMON", "PK_CODE_TABLES", "CODE_TABLES", "GRP_CD", 1),
                ("COMMON", "PK_CODE_TABLES", "CODE_TABLES", "CD", 2),
            ] if connection == "dw" else []
            return result(("OWNER", "CONSTRAINT_NAME", "TABLE_NAME", "COLUMN_NAME", "POSITION"), rows)

        if "COMMON\".\"CODE_TABLES" in upper or "COMMON.CODE_TABLES" in upper:
            # 관례: 그룹키 == 코드 컬럼명. STAT_CD 컬럼 -> STAT_CD 그룹.
            code_rows = [
                ("STAT_CD", "01", "정상", "Y", 1),
                ("STAT_CD", "03", "취소", "Y", 2),
                ("STAT_CD", "09", "해지", "N", 3),
                ("PROD_CD", "01", "접수", "Y", 1),
            ]
            return result(("GRP_CD", "CD", "CD_NM", "USE_YN", "SORT_ORD"), code_rows)

        if "SELECT DISTINCT \"STAT_CD\" AS CODE_VALUE" in upper:
            return result(("CODE_VALUE",), [("01",), ("03",), ("09",)])
        if "COUNT(*) AS CNT" in upper:
            if "COUNT(DISTINCT" in upper:
                return result(("CNT", "ND", "NC", "MN", "MX"), [(3, 3, 0, "01", "09")])
            return result(("CNT",), [(3,)])
        if "AS VAL, COUNT(*) AS CNT" in upper:
            return result(("VAL", "CNT"), [("01", 1), ("03", 1), ("09", 1)])
        if "DBTOOLS_PROBE" in upper:
            return result(("STAT_CD",), [("01",)])
        if 'COUNT(*) AS "ROW_COUNT"' in upper:
            return result(("ROW_COUNT", "C0_MIN", "C0_MAX", "C0_DIST", "C0_NULLS"), [(3, "01", "09", 3, 0)])
        if "FROM \"DW\".\"F_SALES\"" in upper or "FROM DW.F_SALES" in upper:
            return result(("BASE_DT", "STAT_CD", "PROD_CD", "SALE_AMT"), [
                ("2026-08-01", "01", "P1", 100),
                ("2026-08-02", "03", "P1", 50),
                ("2026-08-03", "09", "P2", 10),
            ])
        if "FROM \"SRC\".\"CUSTOMER\"" in upper:
            return result(("CUST_ID", "CUST_NM"), [(1, "A")])
        if upper.startswith("SELECT") or upper.startswith("WITH"):
            return result(("VALUE",), [(1,)])
        raise AssertionError(f"unexpected SQL for {connection}: {sql}")

    @staticmethod
    def _tables(connection: str) -> QueryResult:
        if connection == "dw":
            rows = [
                ("DW", "F_SALES", 3, "2026-09-01", "YES"),
                ("DW", "D_PROD", 2, "2026-09-01", "NO"),
                ("DW", "CUSTOMER", 2, "2026-09-01", "NO"),
                ("COMMON", "CODE_TABLES", 4, "2026-09-01", "NO"),
            ]
        else:
            rows = [("SRC", "CUSTOMER", 2, "2026-09-01", "NO")]
        return result(("OWNER", "TABLE_NAME", "NUM_ROWS", "LAST_ANALYZED", "PARTITIONED"), rows)

    @staticmethod
    def _columns(connection: str) -> QueryResult:
        rows: list[tuple[Any, ...]] = []
        specs = []
        if connection == "dw":
            specs = [
                ("DW", "F_SALES", "BASE_DT", "DATE", 7, None, None, "N", None, 1, 0),
                ("DW", "F_SALES", "STAT_CD", "VARCHAR2", 2, None, None, "N", None, 2, 2),
                ("DW", "F_SALES", "PROD_CD", "VARCHAR2", 10, None, None, "N", None, 3, 10),
                ("DW", "F_SALES", "SALE_AMT", "NUMBER", 22, 15, 2, "Y", None, 4, 0),
                ("DW", "D_PROD", "PROD_CD", "VARCHAR2", 10, None, None, "N", None, 1, 10),
                ("DW", "CUSTOMER", "CUST_ID", "NUMBER", 22, 12, 0, "N", None, 1, 0),
                ("DW", "CUSTOMER", "CUST_NM", "VARCHAR2", 50, None, None, "Y", None, 2, 50),
                ("DW", "CUSTOMER", "ETL_LOAD_DT", "DATE", 7, None, None, "Y", None, 3, 0),
                ("COMMON", "CODE_TABLES", "GRP_CD", "VARCHAR2", 30, None, None, "N", None, 1, 30),
                ("COMMON", "CODE_TABLES", "CD", "VARCHAR2", 10, None, None, "N", None, 2, 10),
                ("COMMON", "CODE_TABLES", "CD_NM", "VARCHAR2", 100, None, None, "N", None, 3, 100),
                ("COMMON", "CODE_TABLES", "USE_YN", "CHAR", 1, None, None, "Y", None, 4, 1),
                ("COMMON", "CODE_TABLES", "SORT_ORD", "NUMBER", 22, 5, 0, "Y", None, 5, 0),
            ]
        else:
            specs = [
                ("SRC", "CUSTOMER", "CUST_ID", "NUMBER", 22, 12, 0, "N", None, 1, 0),
                ("SRC", "CUSTOMER", "CUST_NM", "VARCHAR2", 100, None, None, "Y", None, 2, 100),
            ]
        rows.extend(specs)
        return result(("OWNER", "TABLE_NAME", "COLUMN_NAME", "DATA_TYPE", "DATA_LENGTH", "DATA_PRECISION", "DATA_SCALE", "NULLABLE", "DATA_DEFAULT", "COLUMN_ID", "CHAR_LENGTH"), rows)

    @staticmethod
    def _table_comments(connection: str) -> QueryResult:
        rows = [("DW", "F_SALES", "매출 팩트"), ("DW", "D_PROD", "상품 차원"),
                ("DW", "CUSTOMER", "고객"), ("COMMON", "CODE_TABLES", "공통코드")]
        if connection == "src":
            rows = [("SRC", "CUSTOMER", "원천 고객")]
        return result(("OWNER", "TABLE_NAME", "COMMENTS"), rows)

    @staticmethod
    def _column_comments(connection: str) -> QueryResult:
        rows = []
        if connection == "dw":
            rows = [
                ("DW", "F_SALES", "BASE_DT", "기준일자"),
                ("DW", "F_SALES", "STAT_CD", "매출상태코드"),
                ("DW", "F_SALES", "PROD_CD", "상품코드"),
                ("COMMON", "CODE_TABLES", "GRP_CD", "코드그룹"),
                ("COMMON", "CODE_TABLES", "CD", "코드"),
                ("COMMON", "CODE_TABLES", "CD_NM", "코드명"),
                ("COMMON", "CODE_TABLES", "USE_YN", "사용여부"),
                ("COMMON", "CODE_TABLES", "SORT_ORD", "정렬순서"),
            ]
        return result(("OWNER", "TABLE_NAME", "COLUMN_NAME", "COMMENTS"), rows)


def result(columns: tuple[str, ...], rows: list[tuple[Any, ...]]) -> QueryResult:
    return QueryResult(columns=columns, rows=rows, elapsed_ms=1, truncated=False)


class ToolServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = FakeOracle()
        config = {
            "proxy": {"query_timeout": 5},
            "connections": [
                {"name": "dw", "dsn": "dw:1521/DW"},
                {"name": "src", "dsn": "src:1521/SRC"},
            ],
            "common_code": {"connection": "dw", "table": "COMMON.CODE_TABLES"},
        }
        self.service = ToolService(config, base_dir=self.temp.name, database=self.db)

    def tearDown(self) -> None:
        self.service.close()
        self.temp.cleanup()

    def call(self, tool_name: str, **arguments: Any) -> Any:
        result = self.service.dispatch(tool_name, arguments)
        if isinstance(result, dict) and "error" in result:
            self.fail(f"{tool_name} returned error: {result}")
        return result

    def test_registry_contains_exactly_all_builtin_tools(self) -> None:
        self.assertEqual(tuple(item["name"] for item in self.service.list_tools()), BUILTIN_TOOL_NAMES)
        self.assertEqual(len(BUILTIN_TOOL_NAMES), 18)
        for gone in ("lookup_code", "query_to_file"):
            self.assertNotIn(gone, BUILTIN_TOOL_NAMES)

    def test_catalog_and_basic_tools(self) -> None:
        self.assertEqual(len(self.call("list_connections")["connections"]), 2)
        self.assertIn("DW", [row["owner"] for row in self.call("my_scope", connection="dw")["schemas"]])
        self.assertGreaterEqual(self.call("list_tables", connection="dw")["row_count"], 3)
        self.assertEqual(self.call("describe_table", connection="dw", table="DW.F_SALES")["partition"]["key_columns"], ["BASE_DT"])
        self.assertTrue(self.call("search_catalog", keyword="상태", connection="dw")["matches"])
        self.assertIn("dw", self.call("catalog_refresh", connection="dw")["refreshed"])
        self.assertEqual(self.call("sample_rows", connection="dw", table="DW.F_SALES", limit=2)["row_count"], 2)
        self.assertEqual(self.call("count_rows", connection="dw", table="DW.F_SALES")["count"], 3)
        self.assertEqual(self.call("query", connection="dw", sql="SELECT * FROM DW.F_SALES", max_rows=2)["row_count"], 2)
        self.assertEqual(self.call("resolve_object", connection="dw", name="SALES")["resolved"], "DW.F_SALES")

    def test_describe_table_live_reads_dictionary_metadata(self) -> None:
        described = self.call("describe_table", connection="dw", table="DW.F_SALES", live=True)
        self.assertEqual(described["columns"][1]["comment"], "매출상태코드")
        self.assertEqual(described["primary_key"], ["BASE_DT"])
        self.assertEqual(described["indexes"][0]["columns"], ["BASE_DT"])
        self.assertEqual(described["partition"]["key_columns"], ["BASE_DT"])

    def test_search_catalog_normalizes_scope(self) -> None:
        found = self.call("search_catalog", keyword="상태", connection="dw", scope=" COLUMN ")
        self.assertTrue(found["matches"])
        self.assertTrue(all(item["column"] for item in found["matches"]))

    def test_catalog_refresh_preserves_snapshot_on_adapter_error(self) -> None:
        before = self.call("describe_table", connection="dw", table="DW.F_SALES")
        execute = self.db.execute

        def fail_on_constraints(connection, sql, **kwargs):
            if "FROM ALL_CONSTRAINTS" in sql.upper():
                raise AdapterError("catalog unavailable")
            return execute(connection, sql, **kwargs)

        with self.assertLogs("tool", level="ERROR"), patch.object(
            self.db, "execute", side_effect=fail_on_constraints
        ):
            with self.assertRaises(AdapterError):
                self.service.dispatch("catalog_refresh", {"connection": "dw"})
        self.assertEqual(self.call("describe_table", connection="dw", table="DW.F_SALES"), before)

    def test_sample_rows_projects_columns_and_reports_truncation(self) -> None:
        sampled_result = result(("BASE_DT",), [("2026-08-01",), ("2026-08-02",), ("2026-08-03",)])
        with patch.object(self.db, "execute", return_value=sampled_result) as execute:
            sampled = self.call(
                "sample_rows", connection="dw", table="DW.F_SALES", columns=["BASE_DT"], limit=2
            )
        self.assertEqual(sampled["columns"], ["BASE_DT"])
        self.assertEqual((sampled["row_count"], sampled["truncated"]), (2, True))
        self.assertIn('SELECT "BASE_DT" FROM "DW"."F_SALES"', execute.call_args.args[1])
        self.assertEqual(execute.call_args.kwargs["binds"], {"n": 3})

    def test_count_rows_exact_where_and_bind_contract(self) -> None:
        exact = self.call("count_rows", connection="dw", table="DW.F_SALES", exact=True)
        filtered = self.call(
            "count_rows",
            connection="dw",
            table="DW.F_SALES",
            where="STAT_CD = :status",
            binds={"status": "01"},
        )
        self.assertTrue(exact["exact"])
        self.assertTrue(filtered["exact"])
        rejected = self.service.dispatch(
            "count_rows",
            {"connection": "dw", "table": "DW.F_SALES", "binds": {"status": "01"}},
        )
        self.assertEqual(rejected["error"]["code"], "BAD_PARAM")

    def test_count_rows_rejects_empty_exact_result(self) -> None:
        with patch.object(self.db, "execute", return_value=result(("CNT",), [])):
            with self.assertRaises(AdapterError):
                self.service.dispatch(
                    "count_rows",
                    {"connection": "dw", "table": "DW.F_SALES", "exact": True},
                )

    def test_analysis_tools(self) -> None:
        self.assertTrue(self.call("validate_sql", connection="dw", sql="SELECT 1 FROM dual")["valid"])
        explain = self.service.dispatch("explain", {"connection": "dw", "sql": "SELECT * FROM DW.F_SALES"})
        self.assertEqual(explain["error"]["code"], "NO_ACCESS")
        partitions = self.call("partition_info", connection="dw", table="DW.F_SALES")
        self.assertEqual(partitions["key_columns"], ["BASE_DT"])
        self.assertTrue(partitions["issues"])
        profile = self.call("table_profile", connection="dw", table="DW.F_SALES")
        self.assertEqual(profile["source"], "stats")
        self.assertEqual({item["name"] for item in profile["columns"]}, {"BASE_DT", "STAT_CD", "SALE_AMT"})

    def test_advanced_tools(self) -> None:
        cross = self.call("cross_query", sql="SELECT 1 FROM dual", connections=["dw", "src"])
        self.assertEqual(len(cross["results"]), 2)
        summary = self.call("query_summary", connection="dw", sql="SELECT STAT_CD FROM DW.F_SALES")
        self.assertEqual(summary["row_count"], 3)
        joins = self.call("suggest_joins", connection="dw", table="DW.F_SALES")
        self.assertTrue(any(item["table"] == "DW.D_PROD" for item in joins["joins"]))
        diff = self.call("compare_schema", left={"connection": "src", "owner": "SRC"}, right={"connection": "dw", "owner": "DW"}, table="CUSTOMER")
        self.assertFalse(diff["identical"])
        self.assertEqual(diff["type_diff"][0]["column"], "CUST_NM")

    def test_list_connections_advertises_the_common_code_table(self) -> None:
        """에이전트는 이 값으로 공통코드 테이블 위치를 안다. 이름 규칙으로는 못 찾는다."""
        listed = self.call("list_connections")
        self.assertEqual(listed["common_code"],
                         {"connection": "dw", "table": "COMMON.CODE_TABLES"})

    def test_list_connections_omits_common_code_when_unset(self) -> None:
        config = json.loads(json.dumps(self.service.config))
        config.pop("common_code")
        service = ToolService(config, base_dir=self.temp.name, database=self.db)
        self.addCleanup(service.close)
        self.assertNotIn("common_code", service.dispatch("list_connections", {}))

    def test_agent_decodes_codes_with_plain_sql(self) -> None:
        """전용 도구 없이 query만으로 코드↔코드명을 얻을 수 있어야 한다."""
        described = self.call("describe_table", connection="dw", table="COMMON.CODE_TABLES")
        self.assertEqual([item["comment"] for item in described["columns"]][:3],
                         ["코드그룹", "코드", "코드명"])
        # ORDER BY SORT_ORD 는 SELECT 목록에 없다 — sql_safety 스코프 수정이 없으면 거부된다.
        rows = self.call("query", connection="dw", sql=(
            "SELECT CD, CD_NM FROM COMMON.CODE_TABLES "
            "WHERE GRP_CD = :g ORDER BY SORT_ORD"), binds={"g": "STAT_CD"})
        self.assertEqual(rows["columns"][:2], ["GRP_CD", "CD"])
        self.assertTrue(rows["row_count"])

    def test_bad_common_code_config_is_rejected_on_save(self) -> None:
        """설정 오타는 첫 조회가 아니라 저장/기동 시 거부한다."""
        for broken, why in (
            ({"connection": "dw"}, "table 누락"),
            ({"table": "CODE_TABLES"}, "OWNER 없음"),
            ({"table": "COMMON.CODE_TABLES", "group": "GRP_CD"}, "역할 컬럼은 미지 키"),
            ({"table": "COMMON.CODE_TABLES", "connection": "nope"}, "미등록 커넥션"),
        ):
            config = json.loads(json.dumps(self.service.config))
            config["proxy"].update({"host": "127.0.0.1", "port": 8811, "log_level": "INFO"})
            for item in config["connections"]:
                item.setdefault("user", "READONLY_USER")
                item.setdefault("password", "x")
                item.setdefault("pool_max", 5)
            config["common_code"] = broken
            with self.subTest(why=why), self.assertRaises(ValueError):
                validate_config(config)

    def test_tools_d_is_ignored_by_reload(self) -> None:
        root = Path(self.temp.name)
        tools_dir = root / "tools.d"
        tools_dir.mkdir()
        (tools_dir / "valid.json").write_text(json.dumps({
            "name": "find_sale",
            "description": "상태코드로 매출을 조회합니다.",
            "connection": "dw",
            "group": "sales",
            "sql": "SELECT * FROM DW.F_SALES WHERE STAT_CD = :stat_cd",
            "params": [{"name": "stat_cd", "type": "string", "required": True}],
            "example": {"name": "find_sale", "arguments": {"stat_cd": "01"}},
        }), encoding="utf-8")
        receipt = self.service.reload_extensions()
        self.assertNotIn("tools", receipt)
        dispatchable = tuple(item["name"] for item in self.service.list_tools())
        self.assertEqual(dispatchable, BUILTIN_TOOL_NAMES)
        self.assertNotIn("find_sale", dispatchable)
        rejected = self.service.dispatch("find_sale", {"stat_cd": "01"})
        self.assertEqual(rejected["error"]["code"], "BAD_PARAM")

    def test_relationships_load_and_surface_errors(self) -> None:
        root = Path(self.temp.name)
        relationships_dir = root / "relationships.d"
        relationships_dir.mkdir()
        (relationships_dir / "sales.json").write_text(json.dumps({
            "connection": "dw",
            "table": "DW.F_SALES",
            "joins": [{"table": "DW.D_PROD", "on": [["PROD_ID", "PROD_ID"]], "direction": "parent"}],
        }), encoding="utf-8")
        (relationships_dir / "broken.json").write_text("not json", encoding="utf-8")
        receipt = self.service.reload_extensions()
        self.assertNotIn("codes", receipt)
        self.assertEqual(receipt["relationships"], 1)
        self.assertEqual(len(receipt["errors"]), 1)

    def test_invalid_sql_is_rejected_before_database(self) -> None:
        result = self.service.dispatch("query", {"connection": "dw", "sql": "DELETE FROM DW.F_SALES"})
        self.assertEqual(result["error"]["code"], "INVALID_SQL")


if __name__ == "__main__":
    unittest.main()
