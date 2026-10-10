"""Behavior and work-count regressions for the complete tool review (no Oracle)."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dbtools.database import AdapterError, ConnectionStatus, OracleDatabase, QueryResult
from dbtools.errors import InvalidSqlError, NoAccessError
from dbtools.service import ToolService
from dbtools.tools.compare_schema import compare_schema
from dbtools.tools.explain import explain, _summarize_plan
from dbtools.tools.query_summary import query_summary
from tests.test_dbtools_service import FakeOracle, result


class ToolOptimizationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = FakeOracle()
        self.service = ToolService(
            {"connections": [{"name": name, "dsn": name} for name in ("dw", "src")]},
            base_dir=temp.name, database=self.db,
        )
        self.addCleanup(self.service.close)

    def call(self, name, **arguments):
        return self.service.dispatch(name, arguments)

    def test_cross_query_runs_all_connections_in_order_with_one_or_many_workers(self):
        for workers in (1, 2):
            self.service.config["tools"] = {"cross_query_workers": workers}
            def execute(connection, *args, **kwargs):
                if connection == "src":
                    raise NoAccessError("denied")
                return result(("V",), [(1,), (2,)])
            with self.subTest(workers=workers), patch.object(self.db, "execute", side_effect=execute) as run:
                payload = self.call("cross_query", sql="SELECT 1 FROM dual", max_rows=1)
                self.assertEqual(run.call_count, 2)
                rows = payload["results"]
                self.assertEqual([row["connection"] for row in rows], ["dw", "src"])
                self.assertEqual(rows[0]["rows"], [[1]])
                self.assertTrue(rows[0]["truncated"])
                self.assertEqual(rows[1]["error"]["code"], "NO_ACCESS")

    def test_connection_status_uses_one_snapshot_and_only_reports_failed_row_errors(self):
        statuses = (
            ConnectionStatus("dw", "dw", "ok", 1, 0, 5, "old failure"),
            ConnectionStatus("src", "src", "error", None, 0, 5, "unavailable"),
        )
        with patch.object(self.db, "snapshot", return_value=statuses) as snapshot:
            rows = self.call("list_connections")["connections"]
        snapshot.assert_called_once_with()
        self.assertNotIn("error", rows[0])
        self.assertEqual(rows[1]["error"], "unavailable")
        self.assertGreater(rows[0]["table_count"], 0)

    def test_cached_describe_resolves_object_once(self):
        catalog = self.service.catalog
        with patch.object(catalog, "require_object", wraps=catalog.require_object) as lookup:
            self.call("describe_table", connection="dw", table="SALES")
        lookup.assert_called_once_with("dw", "SALES")

    def test_list_tables_returns_only_identity_fields(self):
        payload = self.call("list_tables", connection="dw")
        self.assertGreaterEqual(payload["row_count"], 1)
        self.assertIn("truncated", payload)
        for row in payload["tables"]:
            # 식별용 필드 + 규모 감용 num_rows만. partitioned·last_analyzed는 전용 도구 몸이다.
            self.assertEqual(set(row), {"owner", "name", "type", "comment", "num_rows"})
            self.assertNotIn("partitioned", row)
            self.assertNotIn("last_analyzed", row)

    def test_my_scope_without_owner_is_counts_only(self):
        payload = self.call("my_scope", connection="dw")
        # 현황 단계: 스키마 개수 요약만, 전체 objects 덤프는 없다.
        self.assertNotIn("objects", payload)
        self.assertTrue(payload["schemas"])
        for schema in payload["schemas"]:
            self.assertEqual(set(schema), {"owner", "tables", "views", "partitioned"})

    def test_my_scope_with_owner_lists_objects(self):
        owner = self.call("my_scope", connection="dw")["schemas"][0]["owner"]
        payload = self.call("my_scope", connection="dw", owner=owner)
        self.assertIn("objects", payload)
        self.assertTrue(all(row["owner"] == owner for row in payload["objects"]))

    def test_catalog_list_objects_is_slim(self):
        # partitioned·last_analyzed는 소비자가 없어 제거됨. num_rows는 규모 감용으로 유지.
        payload = self.service.catalog.list_objects("dw", None, "%", include_views=True, limit=100)
        self.assertTrue(payload["tables"])
        for row in payload["tables"]:
            self.assertEqual(set(row), {"owner", "name", "type", "comment", "num_rows"})

    def test_live_profile_combines_aggregates_and_applies_filters_to_every_query(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute("ATTACH DATABASE ':memory:' AS DW")
        db.execute("CREATE TABLE DW.F_SALES (STAT_CD TEXT, SALE_AMT INTEGER)")
        db.executemany("INSERT INTO DW.F_SALES VALUES (?, ?)", [("01", 10), ("01", 30), ("03", None), ("09", 99)])
        def execute(connection, sql, *, binds, **kwargs):
            sql = sql.replace("FETCH FIRST :top_n ROWS ONLY", "LIMIT :top_n")
            cursor = db.execute(sql, binds)
            return result(tuple(col[0] for col in cursor.description), cursor.fetchall())
        with patch.object(self.db, "execute", side_effect=execute) as run:
            payload = self.call("table_profile", connection="dw", table="DW.F_SALES", mode="live",
                                columns=["STAT_CD", "SALE_AMT"], where="STAT_CD <> :excluded",
                                binds={"excluded": "09"}, top_n=2)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(payload["scanned_rows"], 3)
        status, amount = payload["columns"]
        self.assertEqual((status["num_distinct"], status["null_count"]), (2, 0))
        self.assertEqual(status["top_values"], [{"value": "01", "count": 2}, {"value": "03", "count": 1}])
        self.assertEqual((amount["min"], amount["max"], amount["null_count"]), (10, 30, 1))
        self.assertTrue(all(call.kwargs["binds"]["excluded"] == "09" for call in run.call_args_list))

    def test_live_profile_rejects_empty_where_and_reserved_bind_before_execution(self):
        for where, binds in (("", {}), ("STAT_CD = :TOP_N", {"TOP_N": "01"})):
            with self.subTest(where=where), patch.object(self.db, "execute") as run:
                payload = self.call("table_profile", connection="dw", table="DW.F_SALES", mode="live",
                                    columns=["STAT_CD"], where=where, binds=binds)
                self.assertEqual(payload["error"]["code"], "BAD_PARAM")
                run.assert_not_called()

    def test_profile_rejects_parameters_the_selected_mode_would_ignore(self):
        cases = (
            {"mode": "stats", "where": "STAT_CD = :status", "binds": {"status": "01"}},
            {"mode": "live", "columns": ["STAT_CD"], "binds": {"status": "01"}},
            {"mode": "live", "columns": ["STAT_CD"], "format": "detailed"},
        )
        for arguments in cases:
            with self.subTest(arguments=arguments), patch.object(self.db, "execute") as run:
                payload = self.call("table_profile", connection="dw", table="DW.F_SALES", **arguments)
                self.assertEqual(payload["error"]["code"], "BAD_PARAM")
                run.assert_not_called()

    def test_live_profile_rejects_lob_aggregates(self):
        columns = [{"name": "STAT_CD", "type": "CLOB"}]
        with patch("dbtools.tools.table_profile.object_columns", return_value=columns), patch.object(
            self.db, "execute"
        ) as run:
            payload = self.call(
                "table_profile",
                connection="dw",
                table="DW.F_SALES",
                mode="live",
                columns=["STAT_CD"],
            )
        self.assertEqual(payload["error"]["code"], "BAD_PARAM")
        run.assert_not_called()

    def test_stats_profile_aggregates_modifications_and_fails_closed_on_no_access(self):
        original = self.db.execute

        def changed(connection, sql, **kwargs):
            if "FROM ALL_TAB_MODIFICATIONS" in sql.upper():
                self.assertIn("SUM(NVL(INSERTS, 0))", sql)
                self.assertIn("MAX(TRUNCATED)", sql)
                self.assertEqual(kwargs["max_rows"], 1)
                return result(("INSERTS", "UPDATES", "DELETES", "TRUNCATED"), [(1, 0, 0, "NO")])
            return original(connection, sql, **kwargs)

        with patch.object(self.db, "execute", side_effect=changed):
            self.assertTrue(self.call("table_profile", connection="dw", table="DW.F_SALES")["stale"])

        def denied(connection, sql, **kwargs):
            if "FROM ALL_TAB_MODIFICATIONS" in sql.upper():
                raise NoAccessError("denied")
            return original(connection, sql, **kwargs)

        with patch.object(self.db, "execute", side_effect=denied):
            self.assertTrue(self.call("table_profile", connection="dw", table="DW.F_SALES")["stale"])

    def test_live_profile_rejects_empty_aggregate_result(self):
        original = self.db.execute

        def empty_aggregate(connection, sql, **kwargs):
            if sql.upper().startswith("SELECT COUNT(*) AS CNT"):
                return result(("CNT",), [])
            return original(connection, sql, **kwargs)

        with patch.object(self.db, "execute", side_effect=empty_aggregate):
            with self.assertRaises(AdapterError):
                self.call(
                    "table_profile",
                    connection="dw",
                    table="DW.F_SALES",
                    mode="live",
                    columns=["STAT_CD"],
                )

    def test_joins_skip_unwanted_direction_and_below_threshold_name_scan(self):
        fk = result(("CONSTRAINT_NAME", "OTHER_OWNER", "OTHER_TABLE", "SRC_COL", "DST_COL", "POSITION"),
                    [("FK_PROD", "DW", "D_PROD", "PROD_CD", "PROD_CD", 1)])
        with patch.object(self.db, "execute", return_value=fk) as run, patch.object(self.service.catalog, "list_objects") as listing:
            payload = self.call("suggest_joins", connection="dw", table="DW.F_SALES",
                                direction="parents", min_confidence=0.9)
        self.assertEqual(run.call_count, 1)
        listing.assert_not_called()
        self.assertEqual(payload["joins"][0]["on"], [["PROD_CD", "PROD_CD"]])
        self.assertEqual(payload["joins"][0]["source"], "fk")

    def test_manual_relationship_internal_typeerror_is_not_retried(self):
        with patch.object(self.service.extensions, "relationships", side_effect=TypeError("bug")) as read:
            with self.assertRaisesRegex(TypeError, "bug"):
                self.call("suggest_joins", connection="dw", table="DW.F_SALES")
        read.assert_called_once_with("dw")

    def test_compare_counts_all_tables_after_output_limit(self):
        def obj(connection, table):
            name = table.split(".")[1]
            dtype = "VARCHAR2(10)" if connection == "src" and name in ("A", "B") else "NUMBER"
            return SimpleNamespace(columns=[SimpleNamespace(name="ID", data_type=dtype, nullable=True)])
        catalog = SimpleNamespace(
            require_owner=lambda connection, owner: owner,
            require_object=obj,
            cache_timestamps=Mock(return_value={"dw": "left", "src": "right"}),
            list_objects=lambda *a, **k: {"tables": [{"name": name} for name in ("A", "B", "C", "D")], "truncated": False},
        )
        context = SimpleNamespace(catalog=catalog, get_connection=lambda name: None)
        payload = compare_schema(context, left={"connection": "dw", "owner": "DW"},
                                 right={"connection": "src", "owner": "SRC"}, limit=1)
        self.assertEqual((payload["common"], payload["identical"], payload["different"]), (4, 2, 2))
        self.assertEqual(len(payload["tables"]), 1)
        self.assertTrue(payload["truncated"])
        catalog.cache_timestamps.assert_called_once_with()

    def test_compare_bad_list_member_is_bad_param(self):
        payload = self.call("compare_schema", left={"connection": "dw", "owner": "DW"},
                            right={"connection": "src", "owner": "SRC"}, compare=[{}])
        self.assertEqual(payload["error"]["code"], "BAD_PARAM")

    def test_compare_honors_selected_fields_and_reports_order_reason(self):
        def obj(connection, table):
            names = ("A", "B") if connection == "dw" else ("B", "A")
            return SimpleNamespace(columns=[
                SimpleNamespace(name=name, data_type="NUMBER", nullable=True, column_id=index)
                for index, name in enumerate(names, 1)
            ])

        catalog = SimpleNamespace(
            require_owner=lambda connection, owner: owner,
            require_object=obj,
            cache_timestamps=lambda: {},
            list_objects=lambda *a, **k: {
                "tables": [{"name": "T"}],
                "truncated": False,
            },
        )
        context = SimpleNamespace(catalog=catalog)
        side = lambda connection, owner: {"connection": connection, "owner": owner}
        single = compare_schema(
            context,
            left=side("dw", "DW"),
            right=side("src", "SRC"),
            table="T",
            compare=["types"],
        )
        self.assertTrue(single["identical"])
        self.assertFalse(single["order_diff"])
        schema = compare_schema(context, left=side("dw", "DW"), right=side("src", "SRC"))
        self.assertEqual(schema["different"], 1)
        self.assertTrue(schema["tables"][0]["order_diff"])

    def test_compare_rejects_non_string_ignore_columns(self):
        for item in (None, True):
            with self.subTest(item=item):
                payload = self.call(
                    "compare_schema",
                    left={"connection": "dw", "owner": "DW"},
                    right={"connection": "src", "owner": "SRC"},
                    ignore_columns=[item],
                )
                self.assertEqual(payload["error"]["code"], "BAD_PARAM")

    def test_compare_does_not_claim_side_only_from_truncated_lists(self):
        visible = {"left": ("A", "B"), "right": ("B", "C")}

        def obj(connection, table):
            return SimpleNamespace(columns=[
                SimpleNamespace(name="ID", data_type="NUMBER", nullable=True, column_id=1)
            ])

        catalog = SimpleNamespace(
            require_owner=lambda connection, owner: owner,
            require_object=obj,
            cache_timestamps=lambda: {},
            list_objects=lambda connection, **kwargs: {
                "tables": [{"name": name} for name in visible[connection]],
                "truncated": True,
            },
        )
        payload = compare_schema(
            SimpleNamespace(catalog=catalog),
            left={"connection": "left", "owner": "L"},
            right={"connection": "right", "owner": "R"},
        )
        self.assertEqual(payload["only_left_tables"], [])
        self.assertEqual(payload["only_right_tables"], [])
        self.assertTrue(payload["truncated"])
        self.assertIn("상한 내", payload["hint"])

    def test_compare_warns_when_number_integer_capacity_shrinks(self):
        def obj(connection, table):
            data_type = "NUMBER(10,2)" if connection == "dw" else "NUMBER(10,3)"
            return SimpleNamespace(columns=[
                SimpleNamespace(name="AMT", data_type=data_type, nullable=True, column_id=1)
            ])

        catalog = SimpleNamespace(
            require_owner=lambda connection, owner: owner,
            require_object=obj,
            cache_timestamps=lambda: {},
        )
        payload = compare_schema(
            SimpleNamespace(catalog=catalog),
            left={"connection": "dw", "owner": "DW"},
            right={"connection": "src", "owner": "SRC"},
            table="T",
        )
        self.assertEqual(payload["type_diff"][0]["hint"], "우측 길이 부족 — 절단 위험")

    def test_nonpartitioned_table_stops_after_metadata_lookup(self):
        with patch.object(self.db, "execute", return_value=result(("TABLE_NAME",), [])) as run:
            payload = self.call("partition_info", connection="dw", table="DW.D_PROD")
        self.assertFalse(payload["partitioned"])
        self.assertEqual(run.call_count, 1)

    def test_partition_schema_filter_does_not_report_false_truncation(self):
        from dbtools.tools import partition_info as module
        with patch.object(module, "_load_part_tables", return_value=([
            {"TABLE_NAME": "A"}, {"TABLE_NAME": "B"}], False)), \
             patch.object(module, "_load_part_keys", return_value={}), \
             patch.object(module, "_load_tab_partitions", return_value=({}, False)):
            payload = self.call("partition_info", connection="dw", owner="DW", only_issues=True, limit=1)
        self.assertEqual(payload["tables"], [])
        self.assertFalse(payload["truncated"])
        # not_partitioned 은 도구 목적(파티션 분석)과 무관한 토큰 부하라 제거되었다.
        self.assertNotIn("not_partitioned", payload)

    def test_validate_sql_preserves_internal_errors_and_converts_sql_errors(self):
        for error in (TypeError("bug"), NoAccessError("denied")):
            with patch.object(self.db, "parse", side_effect=error) as parse:
                if isinstance(error, TypeError):
                    with self.assertRaises(TypeError):
                        self.call("validate_sql", connection="dw", sql="SELECT 1 FROM dual")
                else:
                    self.assertEqual(self.call("validate_sql", connection="dw", sql="SELECT 1 FROM dual")["error"]["code"], "NO_ACCESS")
                parse.assert_called_once()
        with patch.object(self.db, "parse", side_effect=InvalidSqlError("syntax", position=7)):
            payload = self.call("validate_sql", connection="dw", sql="SELECT 1 FROM dual")
        self.assertFalse(payload["valid"])
        self.assertEqual(payload["position"], 7)


class ExplainAndResultTests(unittest.TestCase):
    def test_explain_uses_configured_table_and_full_skips_summary_fetch(self):
        for fmt, fail in (("summary", False), ("full", False), ("summary", True)):
            with self.subTest(format=fmt, fail=fail):
                cursor = Mock(description=[("PLAN_TABLE_OUTPUT",)])
                cursor.fetchmany.return_value = [("plan text",)] if fmt == "full" else []
                if fail:
                    cursor.execute.side_effect = [None, InvalidSqlError("fetch failed"), None]
                connection = SimpleNamespace(cursor=lambda: cursor)
                database = SimpleNamespace(transaction=lambda name, callback, **kw: callback(connection))
                context = SimpleNamespace(database=database, get_connection=lambda name: None,
                                          config={"explain": {"enabled": True, "plan_table": "APP_RO.PLAN_TABLE"}},
                                          query_timeout=5)
                if fail:
                    with self.assertRaises(InvalidSqlError):
                        explain(context, connection="dw", sql="SELECT 1 FROM dual", format=fmt)
                else:
                    explain(context, connection="dw", sql="SELECT 1 FROM dual", format=fmt)
                statements = [call.args[0] for call in cursor.execute.call_args_list]
                self.assertEqual(len(statements), 3)
                self.assertIn('INTO "APP_RO"."PLAN_TABLE" FOR', statements[0])
                self.assertTrue(statements[-1].startswith('DELETE FROM "APP_RO"."PLAN_TABLE"'))
                self.assertEqual("DBMS_XPLAN" in statements[1], fmt == "full")
                cursor.close.assert_called_once()

    def test_explain_reuses_partition_metadata_for_repeated_table_access(self):
        catalog = SimpleNamespace(
            partition_summary=Mock(return_value={"partitioned": True, "partition_count": 5, "key_columns": ["DT"]}),
            require_object=Mock(return_value=SimpleNamespace(last_analyzed=None)),
        )
        row = {"ID": 0, "OPERATION": "TABLE ACCESS", "OBJECT_OWNER": "DW", "OBJECT_NAME": "T",
               "OBJECT_TYPE": "TABLE", "PARTITION_START": "1", "PARTITION_STOP": "5"}
        payload = _summarize_plan(SimpleNamespace(catalog=catalog, config={}), "dw", [row, {**row, "ID": 1}])
        catalog.partition_summary.assert_called_once()
        catalog.require_object.assert_called_once()
        self.assertEqual([w["code"] for w in payload["warnings"]], ["FULL_PARTITION_SCAN", "NO_STATS"])

    def test_driver_preserves_types_even_without_rows(self):
        cursor = Mock(description=[("DOC", SimpleNamespace(name="DB_TYPE_CLOB")),
                                   ("AMT", SimpleNamespace(name="DB_TYPE_NUMBER"))])
        cursor.fetchmany.return_value = []
        conn = SimpleNamespace(cursor=lambda: cursor)
        db = OracleDatabase({"connections": [{"name": "dw", "dsn": "dw", "user": "AI", "password": "x", "pool_max": 1}]})
        with patch.object(db, "_with_connection", side_effect=lambda name, fn, **kw: fn(conn)):
            payload = db.execute("dw", "SELECT DOC, AMT FROM T")
        self.assertEqual(payload.column_types, ("CLOB", "NUMBER"))
        self.assertEqual(payload.rows, [])

    def test_summary_uses_declared_types_when_first_row_is_null(self):
        probe = QueryResult(("DOC", "AMT"), [(None, None)], 1, False, ("CLOB", "NUMBER"))
        summary = result(("ROW_COUNT", "C0_NULLS", "C1_MIN", "C1_MAX", "C1_DIST", "C1_NULLS"),
                         [(3, 1, 2, 8, 2, 1)])
        execute = Mock(side_effect=[probe, summary])
        context = SimpleNamespace(get_connection=lambda name: None, query_timeout=5,
                                  database=SimpleNamespace(execute=execute))
        payload = query_summary(context, connection="dw", sql="SELECT DOC, AMT FROM T")
        sql = execute.call_args_list[1].args[1]
        self.assertNotIn('MIN(t."DOC")', sql)
        self.assertNotIn('COUNT(DISTINCT t."DOC")', sql)
        self.assertIn('COUNT(CASE WHEN t."DOC" IS NULL THEN 1 END)', sql)
        self.assertIn('MIN(t."AMT")', sql)
        self.assertEqual([item["type"] for item in payload["columns"]], ["CLOB", "NUMBER"])
        self.assertEqual(payload["columns"][1]["max"], 8)


if __name__ == "__main__":
    unittest.main()
