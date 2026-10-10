"""Summarize SQL results with row counts and per-column stats."""

from __future__ import annotations

import time
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from ..context import ToolContext
from ..errors import (
    BadParamError,
    InvalidSqlError,
)
from ..registry import ParamSpec, ToolDefinition

from ..common.catalog_access import LOB_TYPES
from ..common.params import bound_sql
from ..common.execution import execute

_SUMMARY_COLUMN_CAP = 100


def _quote_label(name: str) -> str:
    if not name or any(ch in name for ch in "\x00\n\r"):
        raise BadParamError("컬럼 이름에 제어 문자가 있습니다")
    return '"' + name.replace('"', '""') + '"'


def _python_type_name(value: Any) -> str:
    if value is None:
        return "UNKNOWN"
    if isinstance(value, bool):
        return "NUMBER"
    if isinstance(value, (int, float, Decimal)):
        return "NUMBER"
    if isinstance(value, datetime):
        return "DATE"
    if isinstance(value, date):
        return "DATE"
    if isinstance(value, (bytes, bytearray)):
        return "RAW"
    if isinstance(value, str):
        return "VARCHAR2"
    type_name = type(value).__name__.upper()
    if "CLOB" in type_name or "NCLOB" in type_name:
        return "CLOB"
    if "BLOB" in type_name or "BFILE" in type_name:
        return "BLOB"
    if "LOB" in type_name:
        return "CLOB"
    if callable(getattr(value, "read", None)):
        return "CLOB"
    return "UNKNOWN"


def _is_lob_type(type_name: str) -> bool:
    text = (type_name or "").strip().upper()
    if not text:
        return False
    if text in LOB_TYPES:
        return True
    base = text.split("(")[0].strip()
    if base in LOB_TYPES:
        return True
    if "CLOB" in text or "BLOB" in text or "BFILE" in text or "XMLTYPE" in text:
        return True
    if text.endswith(" LOB") or " LOB " in f" {text} ":
        return True
    return False


def _summary_column_kind(
    declared: str | None, sample: Any
) -> tuple[str, bool, bool]:
    """Return (type_label, skip_minmax_distinct, allow_null_count)."""
    if declared:
        if declared in {"LONG", "LONG_RAW", "LONG_NVARCHAR", "OBJECT", "CURSOR"}:
            return declared, True, False
        if _is_lob_type(declared):
            return declared.split("(")[0].strip() or declared, True, True
        return declared.split("(")[0].strip() or declared, False, True
    if sample is None:
        return "UNKNOWN", True, True
    inferred = _python_type_name(sample)
    if inferred == "UNKNOWN" or _is_lob_type(inferred):
        return inferred, True, False
    return inferred, False, True


def query_summary(
    context: ToolContext,
    *,
    connection: str,
    sql: str,
    binds: dict[str, Any] | None = None,
) -> dict[str, Any]:
    context.get_connection(connection)
    stripped, mapped = bound_sql(sql, binds)
    probe_sql = f"SELECT * FROM (\n{stripped}\n) dbtools_probe FETCH FIRST 1 ROWS ONLY"
    started = time.perf_counter()
    probe = execute(context, connection, probe_sql, mapped, max_rows=1)
    columns = probe.columns
    probe_row = probe.rows[0] if probe.rows else None
    if len(columns) > _SUMMARY_COLUMN_CAP:
        raise BadParamError(
            f"요약할 컬럼이 {_SUMMARY_COLUMN_CAP}개를 초과합니다",
            hint="필요한 컬럼만 선택한 뒤 다시 호출하세요",
        )
    select_parts = ['COUNT(*) AS "ROW_COUNT"']
    meta: list[tuple[str, str, bool, bool]] = []
    declared_types = probe.column_types
    for index, name in enumerate(columns):
        quoted = _quote_label(name)
        sample = probe_row[index] if probe_row is not None and index < len(probe_row) else None
        declared = declared_types[index] if index < len(declared_types) else None
        inferred, skip_minmax, allow_nulls = _summary_column_kind(declared, sample)
        meta.append((name, inferred, skip_minmax, allow_nulls))
        if skip_minmax:
            if allow_nulls:
                select_parts.append(f"COUNT(CASE WHEN t.{quoted} IS NULL THEN 1 END) AS C{index}_NULLS")
        else:
            select_parts.extend(
                [
                    f"MIN(t.{quoted}) AS C{index}_MIN",
                    f"MAX(t.{quoted}) AS C{index}_MAX",
                    f"COUNT(DISTINCT t.{quoted}) AS C{index}_DIST",
                    f"(COUNT(*) - COUNT(t.{quoted})) AS C{index}_NULLS",
                ]
            )
    summary_sql = (
        "SELECT "
        + ", ".join(select_parts)
        + f" FROM (\n{stripped}\n) t"
    )
    result = execute(context, connection, summary_sql, mapped, max_rows=1)
    rows = result.rows
    if not rows:
        raise InvalidSqlError("요약 쿼리가 행을 반환하지 않았습니다")
    data = dict(zip(result.columns, rows[0]))
    summaries: list[dict[str, Any]] = []
    for index, (name, inferred, skip_minmax, allow_nulls) in enumerate(meta):
        summaries.append(
            {
                "name": name,
                "type": inferred,
                "min": None if skip_minmax else data.get(f"C{index}_MIN"),
                "max": None if skip_minmax else data.get(f"C{index}_MAX"),
                "distinct": None if skip_minmax else data.get(f"C{index}_DIST"),
                "nulls": data.get(f"C{index}_NULLS") if allow_nulls else None,
            }
        )
    elapsed = max(
        0,
        int((time.perf_counter() - started) * 1000),
        probe.elapsed_ms + result.elapsed_ms,
    )
    return {
        "row_count": data.get("ROW_COUNT"),
        "elapsed_ms": elapsed,
        "columns": summaries,
    }


def definition() -> ToolDefinition:
    return ToolDefinition(
        name="query_summary",
        description=(
            "SQL 결과 행을 반환하지 않고 행수와 컬럼별 min/max/distinct/null 요약만 반환한다. "
            "explain이 HIGH_ROWS 경고를 냈거나 결과 크기를 모를 때 query 대신 먼저 호출하라."
        ),
        params=(
            ParamSpec("connection", "string", required=True),
            ParamSpec("sql", "string", required=True),
            ParamSpec("binds", "dict", required=False),
        ),
        example={
            "name": "query_summary",
            "arguments": {
                "connection": "dw-prod",
                "sql": "SELECT SALE_AMT FROM DW.F_SALES WHERE BASE_DT >= :dt",
                "binds": {"dt": "2026-08-01"},
            },
        },
        handler=query_summary,
    )


__all__ = ["definition", "query_summary"]
