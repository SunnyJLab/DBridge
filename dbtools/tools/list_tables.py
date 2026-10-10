"""Schema table/view listing filtered by a name LIKE pattern."""

from __future__ import annotations

from typing import Any

from ..catalog import LIST_MAX_LIMIT
from ..common.params import bounded_limit, require_nonempty_string
from ..context import ToolContext
from ..registry import ParamSpec, ToolDefinition
from ..sql_safety import validate_identifier


def list_tables(
    context: ToolContext,
    *,
    connection: str,
    owner: str | None = None,
    pattern: str = "%",
    include_views: bool = True,
    limit: int = 100,
) -> dict[str, Any]:
    context.get_connection(connection)
    owner_ident = validate_identifier(owner, max_parts=1) if owner is not None else None
    require_nonempty_string(pattern, "pattern")
    cap = bounded_limit(limit, cap=LIST_MAX_LIMIT, name="limit")
    return context.catalog.list_objects(
        connection,
        owner_ident,
        pattern,
        include_views=include_views,
        limit=cap,
    )


def definition() -> ToolDefinition:
    return ToolDefinition(
        name="list_tables",
        description=(
            "`owner`와 이름 LIKE 패턴으로 테이블·뷰의 식별 정보(owner·name·type·comment)와 통계 기반 근사 행수(num_rows)를 반환한다. "
            "정확한 건수는 `count_rows`, 컬럼 상세는 `describe_table`, 파티션 목록·개수는 `partition_info`(owner 지정)를 사용하라. "
            "이름 대신 코멘트·컬럼 키워드로 찾을 때는 `search_catalog`를 사용하라."
        ),
        params=(
            ParamSpec("connection", "string", required=True),
            ParamSpec("owner", "string", required=False),
            ParamSpec("pattern", "string", required=False, default="%"),
            ParamSpec("include_views", "bool", required=False, default=True),
            ParamSpec("limit", "int", required=False, default=100),
        ),
        example={
            "name": "list_tables",
            "arguments": {"connection": "dw-prod", "owner": "DW", "pattern": "%SALES%"},
        },
        handler=list_tables,
    )


__all__ = ["definition", "list_tables"]
