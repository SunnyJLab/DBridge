"""Execute a single SELECT/WITH statement with bind variables."""

from __future__ import annotations

from typing import Any

from ..context import ToolContext
from ..errors import BadParamError
from ..registry import ParamSpec, ToolDefinition
from ..sql_safety import (
    ROW_LIMIT_BIND,
    authorize_select_sql,
    sql_mentions_bind,
    wrap_select_rownum_cap,
)

from ..common.execution import execute
from ..common.params import bounded_limit, normalize_binds_reserved_guard
from ..common.results import query_payload

_QUERY_ROWS_CAP = 1000


def query(
    context: ToolContext,
    *,
    connection: str,
    sql: str,
    binds: dict[str, Any] | None = None,
    max_rows: int = 100,
) -> dict[str, Any]:
    context.get_connection(connection)
    cap = bounded_limit(max_rows, cap=_QUERY_ROWS_CAP, name="max_rows")
    bind_values = normalize_binds_reserved_guard(binds)
    if sql_mentions_bind(sql, ROW_LIMIT_BIND):
        raise BadParamError(
            f"SQL에 예약 바인드 {ROW_LIMIT_BIND}는 사용할 수 없습니다",
            hint="다른 바인드 이름을 사용하세요",
        )
    stripped = authorize_select_sql(sql, context.catalog, connection)
    to_run = wrap_select_rownum_cap(stripped, bind_name=ROW_LIMIT_BIND)
    bind_values[ROW_LIMIT_BIND] = cap + 1
    result = execute(context, connection, to_run, bind_values, max_rows=cap)
    return query_payload(result, max_rows=cap)


def definition() -> ToolDefinition:
    return ToolDefinition(
        name="query",
        description=(
            "SELECT/WITH 단일 문장을 바인드 변수와 함께 실행한다. 결과가 `max_rows`에서 잘리면 "
            "`truncated: true` — 조건을 좁히거나 WHERE로 범위를 줄여라. 집계만 필요하면 "
            "`query_summary`가 낫다. 무거운 쿼리는 먼저 "
            "`explain`으로 파티션 프루닝 여부를 확인하라. 식별자는 카탈로그에 존재해야 하며 값은 "
            "반드시 `:name` 바인드로 넘겨라."
        ),
        params=(
            ParamSpec("connection", "string", required=True),
            ParamSpec("sql", "string", required=True),
            ParamSpec("binds", "dict", required=False),
            ParamSpec("max_rows", "int", required=False, default=100),
        ),
        example={
            "name": "query",
            "arguments": {
                "connection": "dw-prod",
                "sql": "SELECT * FROM DW.F_SALES WHERE BASE_DT >= :from_dt",
                "binds": {":from_dt": "2026-08-01"},
                "max_rows": 50,
            },
        },
        handler=query,
    )


__all__ = ["definition", "query"]
