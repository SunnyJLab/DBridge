"""Return approximate stats or exact COUNT(*) for a table."""

from __future__ import annotations

from typing import Any

from ..context import ToolContext
from ..database import AdapterError
from ..errors import BadParamError, NoStatsError
from ..registry import ParamSpec, ToolDefinition
from ..sql_safety import (
    quote_qualified_name,
    render_restricted_where,
)

from ..common.catalog_access import object_column_names, owner_name, require_object
from ..common.execution import execute
from ..common.params import normalize_binds_reserved_guard, require_nonempty_string
from ..common.results import attr_or_key, query_payload


def count_rows(
    context: ToolContext,
    *,
    connection: str,
    table: str,
    where: str | None = None,
    binds: dict[str, Any] | None = None,
    exact: bool = False,
) -> dict[str, Any]:
    require_nonempty_string(table, "table")
    if where is None and binds:
        raise BadParamError("binds는 where와 함께 전달해야 합니다")
    obj = require_object(context.catalog, connection, table)
    owner, name = owner_name(obj, table)
    quoted_table = quote_qualified_name((owner, name))
    if not exact and where is None:
        num_rows = attr_or_key(obj, "num_rows")
        last_analyzed = attr_or_key(obj, "last_analyzed")
        if num_rows is None or last_analyzed is None:
            raise NoStatsError(
                f"{owner}.{name}: 통계가 없어 근사 행수를 반환할 수 없습니다",
                hint="exact=true로 count_rows를 호출하거나 table_profile mode=live를 사용하세요",
            )
        return {
            "count": int(num_rows),
            "exact": False,
            "source": "stats",
            "last_analyzed": last_analyzed,
        }
    if where is not None:
        clause = require_nonempty_string(where, "where")
        rendered, selected = render_restricted_where(
            clause,
            allowed_columns=object_column_names(obj),
            table_name=name,
            owner=owner,
            binds=normalize_binds_reserved_guard(binds),
        )
        sql = f"SELECT COUNT(*) AS CNT FROM {quoted_table} WHERE ({rendered})"
        result = execute(context, connection, sql, selected, max_rows=1)
    else:
        sql = f"SELECT COUNT(*) AS CNT FROM {quoted_table}"
        result = execute(context, connection, sql, None, max_rows=1)
    payload = query_payload(result, max_rows=1)
    if not payload["rows"]:
        raise AdapterError(f"{owner}.{name}: COUNT 결과가 비었습니다")
    count = payload["rows"][0][0]
    return {"count": int(count), "exact": True, "elapsed_ms": payload["elapsed_ms"]}


def definition() -> ToolDefinition:
    return ToolDefinition(
        name="count_rows",
        description=(
            "행수를 반환한다. `where`와 `exact: true`가 없으면 통계 근사치(`exact: false`)를 반환하고, "
            "둘 중 하나라도 지정하면 실제 `COUNT(*)`를 실행한다. 통계가 없으면 `exact: true`로 다시 호출하라. "
            "대형 테이블은 `partition_info`에서 파티션 키를 확인해 `where`로 범위를 줄여라."
        ),
        params=(
            ParamSpec("connection", "string", required=True),
            ParamSpec("table", "string", required=True),
            ParamSpec("where", "string", required=False),
            ParamSpec("binds", "dict", required=False),
            ParamSpec("exact", "bool", required=False, default=False),
        ),
        example={
            "name": "count_rows",
            "arguments": {"connection": "dw-prod", "table": "DW.F_SALES"},
        },
        handler=count_rows,
    )


__all__ = ["definition", "count_rows"]
