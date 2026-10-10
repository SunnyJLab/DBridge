"""Keyword search across table/column names and comments."""

from __future__ import annotations

from typing import Any

from ..catalog import SEARCH_MAX_LIMIT
from ..common.params import bounded_limit
from ..context import ToolContext
from ..registry import ParamSpec, ToolDefinition


def search_catalog(
    context: ToolContext,
    *,
    keyword: str,
    connection: str | None = None,
    owner: str | None = None,
    scope: str = "all",
    limit: int = 50,
) -> dict[str, Any]:
    cap = bounded_limit(limit, cap=SEARCH_MAX_LIMIT, name="limit")
    return context.catalog.search(keyword, connection, owner, scope, cap)


def definition() -> ToolDefinition:
    return ToolDefinition(
        name="search_catalog",
        description=(
            "`keyword`로 테이블·컬럼 이름과 코멘트를 통합 검색한다. `connection`을 생략하면 "
            "모든 커넥션을 검색하고, `scope`로 테이블/컬럼만 좁힌다. 캐시 검색이며 이름을 알면 "
            "`describe_table`을 바로 호출하라."
        ),
        params=(
            ParamSpec("keyword", "string", required=True),
            ParamSpec("connection", "string", required=False),
            ParamSpec("owner", "string", required=False),
            ParamSpec("scope", "string", required=False, default="all"),
            ParamSpec("limit", "int", required=False, default=50),
        ),
        example={
            "name": "search_catalog",
            "arguments": {"keyword": "고객등급", "scope": "column"},
        },
        handler=search_catalog,
    )


__all__ = ["definition", "search_catalog"]
