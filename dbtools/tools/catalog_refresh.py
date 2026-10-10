"""Recollect the catalog cache without blocking readers."""

from __future__ import annotations

from typing import Any

from ..context import ToolContext
from ..registry import ParamSpec, ToolDefinition


def catalog_refresh(context: ToolContext, *, connection: str | None = None) -> dict[str, Any]:
    return context.catalog.refresh(connection)


def definition() -> ToolDefinition:
    return ToolDefinition(
        name="catalog_refresh",
        description=(
            "카탈로그 캐시를 다시 수집한다. 최근 생성된 객체가 `UNKNOWN_OBJECT`로 조회되지 않을 때 호출하라. "
            "갱신 중에는 기존 캐시로 응답하며, 커넥션당 수 초~수십 초 걸리므로 자주 호출하지 마라."
        ),
        params=(ParamSpec("connection", "string", required=False),),
        example={"name": "catalog_refresh", "arguments": {"connection": "dw-prod"}},
        handler=catalog_refresh,
    )


__all__ = ["definition", "catalog_refresh"]
