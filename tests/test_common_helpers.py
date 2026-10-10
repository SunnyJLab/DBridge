#!/usr/bin/env python3
"""Regression tests for dbtools/common's functional modules.

``params`` keeps three deliberately distinct bind normalizers; these lock that
difference in. ``execution``/``catalog_access`` call their single collaborator
signature directly, so the tests here assert that a ``TypeError`` surfaces
instead of being retried under another shape. Fakes capture exact calls;
no DB required.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dbtools.common.catalog_access import require_object
from dbtools.common.execution import execute
from dbtools.common.params import (
    bound_sql,
    bounded_int,
    bounded_limit,
    normalize_binds_lenient,
    normalize_binds_reserved_guard,
    normalize_binds_strict_name,
)
from dbtools.errors import BadParamError, UnknownObjectError
from dbtools.sql_safety import ROW_LIMIT_BIND


class _FakeDatabase:
    """Records every ``execute`` call and replays scripted responses in order."""

    def __init__(self, *responses: Any) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class _FakeContext:
    def __init__(self, database: Any, *, query_timeout: float = 7.5) -> None:
        self.database = database
        self.query_timeout = query_timeout


class BindNormalizationTests(unittest.TestCase):
    def test_reserved_guard_rejects_reserved_bind_case_insensitively(self) -> None:
        with self.assertRaises(BadParamError):
            normalize_binds_reserved_guard({f":{ROW_LIMIT_BIND.upper()}": 1})
        self.assertEqual(normalize_binds_reserved_guard({":stat_cd": "01"}), {"stat_cd": "01"})

    def test_lenient_strips_colon_and_permits_nonidentifier_name(self) -> None:
        # Lenient variant has no charset/identifier check at all, unlike the
        # reserved-guard and strict-name variants.
        out = normalize_binds_lenient({":1-bad name!": "v"})
        self.assertEqual(out, {"1-bad name!": "v"})
        with self.assertRaises(BadParamError):
            normalize_binds_lenient({" ": "v"})

    def test_strict_name_validates_identifiers_and_strips_colon(self) -> None:
        self.assertEqual(normalize_binds_strict_name({":stat_cd": "01"}), {"stat_cd": "01"})
        with self.assertRaises(BadParamError):
            normalize_binds_strict_name({":1bad": "v"})
        # Unlike the reserved-guard variant, the reserved bind name itself is
        # not special-cased here (only identifier shape matters).
        self.assertEqual(
            normalize_binds_strict_name({f":{ROW_LIMIT_BIND}": 1}),
            {ROW_LIMIT_BIND: 1},
        )

    def test_strict_name_rejects_normalized_duplicates(self) -> None:
        for binds in ({":x": 1, "x": 2}, {"x": 1, "X": 2}):
            with self.subTest(binds=binds), self.assertRaises(BadParamError):
                normalize_binds_strict_name(binds)

    def test_bound_sql_enforces_missing_unused_and_case_insensitive_match(self) -> None:
        with self.subTest("missing"), self.assertRaises(BadParamError):
            bound_sql("SELECT * FROM t WHERE id = :id", {})
        with self.subTest("unused"), self.assertRaises(BadParamError):
            bound_sql("SELECT * FROM t WHERE id = :id", {"id": 1, "extra": 2})
        with self.subTest("case_insensitive"):
            sql, mapped = bound_sql("SELECT * FROM t WHERE id = :ID", {"id": 5})
            self.assertEqual(mapped, {"ID": 5})
            self.assertIn("SELECT", sql)

    def test_bounded_int_allows_lower_minimum_than_bounded_limit(self) -> None:
        # advanced's bounded_int has a configurable minimum (default 1) while
        # basic/analysis's bounded_limit hard-codes "< 1" as the reject floor.
        self.assertEqual(bounded_int(0, cap=10, name="n", minimum=0), 0)
        with self.assertRaises(BadParamError):
            bounded_limit(0, cap=10, name="n")


class ExecuteTests(unittest.TestCase):
    def test_execute_direct_passes_timeout_s_once(self) -> None:
        db = _FakeDatabase("ok")
        context = _FakeContext(db)
        self.assertEqual(
            execute(context, "dw", "SELECT 1 FROM dual", {"a": 1}, max_rows=5), "ok"
        )
        self.assertEqual(len(db.calls), 1)
        args, kwargs = db.calls[0]
        self.assertEqual(args, ("dw", "SELECT 1 FROM dual"))
        self.assertEqual(kwargs, {"binds": {"a": 1}, "max_rows": 5, "timeout_s": 7.5})

    def test_execute_direct_does_not_retry_typeerror(self) -> None:
        """시그니처는 하나뿐이다. TypeError는 진짜 버그이므로 그대로 올라와야 한다."""
        db = _FakeDatabase(TypeError("execute() got an unexpected keyword argument 'timeout_s'"))
        with self.assertRaises(TypeError):
            execute(_FakeContext(db), "dw", "SELECT 1 FROM dual", None, max_rows=1)
        self.assertEqual(len(db.calls), 1)


class RequireObjectTests(unittest.TestCase):
    def test_require_object_delegates_with_the_real_signature(self) -> None:
        class Catalog:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def require_object(self, connection: str, table: str) -> str:
                self.calls.append((connection, table))
                return "obj"

        catalog = Catalog()
        self.assertEqual(require_object(catalog, "dw", "DW.F_SALES"), "obj")
        self.assertEqual(catalog.calls, [("dw", "DW.F_SALES")])

    def test_require_object_propagates_catalog_errors(self) -> None:
        class Catalog:
            def require_object(self, connection: str, table: str) -> Any:
                raise UnknownObjectError(f"카탈로그에 없는 식별자: {table}")

        with self.assertRaises(UnknownObjectError):
            require_object(Catalog(), "dw", "DW.NOPE")


if __name__ == "__main__":
    unittest.main()
