"""Return up to N sample rows from a table."""

from __future__ import annotations

from typing import Any

from ..context import ToolContext
from ..errors import BadParamError
from ..registry import ParamSpec, ToolDefinition
from ..sql_safety import (
    quote_identifier,
    quote_qualified_name,
)

from ..common.catalog_access import owner_name, require_column, require_object
from ..common.execution import execute
from ..common.params import bounded_limit, require_nonempty_string
from ..common.results import query_payload

_SAMPLE_ROWS_CAP = 20


def sample_rows(
    context: ToolContext,
    *,
    connection: str,
    table: str,
    limit: int = 10,
    columns: list[Any] | None = None,
) -> dict[str, Any]:
    require_nonempty_string(table, "table")
    cap = bounded_limit(limit, cap=_SAMPLE_ROWS_CAP, name="limit")
    obj = require_object(context.catalog, connection, table)
    owner, name = owner_name(obj, table)
    quoted_table = quote_qualified_name((owner, name))
    qualified = f"{owner}.{name}"
    if columns is None:
        col_sql = "*"
    else:
        if not columns:
            raise BadParamError(
                "columns가 비어 있습니다",
                hint="컬럼 이름 배열을 전달하거나 생략하세요",
            )
        quoted_cols = []
        for item in columns:
            if not isinstance(item, str):
                raise BadParamError(
                    "columns 항목은 문자열이어야 합니다",
                    hint="컬럼 이름을 문자열 배열로 전달하세요",
                )
            col = require_column(context.catalog, connection, qualified, item)
            quoted_cols.append(quote_identifier(col, max_parts=1))
        col_sql = ", ".join(quoted_cols)
    sql = f"SELECT {col_sql} FROM {quoted_table} FETCH FIRST :n ROWS ONLY"
    result = execute(context, connection, sql, {"n": cap + 1}, max_rows=cap + 1)
    return query_payload(result, max_rows=cap)


def definition() -> ToolDefinition:
    return ToolDefinition(
        name="sample_rows",
        description=(
            f"테이블 상위 N행(최대 {_SAMPLE_ROWS_CAP})을 반환한다. 컬럼이 많으면 `columns`로 "
            "필요한 것만 지정하고, 값 분포·건수는 `table_profile`/`count_rows`를 사용하라."
        ),
        params=(
            ParamSpec("connection", "string", required=True),
            ParamSpec("table", "string", required=True),
            ParamSpec("limit", "int", required=False, default=10),
            ParamSpec("columns", "list", required=False),
        ),
        example={
            "name": "sample_rows",
            "arguments": {
                "connection": "dw-prod",
                "table": "DW.F_SALES",
                "limit": 5,
                "columns": ["BASE_DT", "STAT_CD"],
            },
        },
        handler=sample_rows,
    )


__all__ = ["definition", "sample_rows"]
