"""Schemas and objects the current account can SELECT."""

from __future__ import annotations

from typing import Any

from ..catalog import SCOPE_OBJECT_LIMIT
from ..context import ToolContext
from ..registry import ParamSpec, ToolDefinition


def my_scope(context: ToolContext, *, connection: str, owner: str | None = None) -> dict[str, Any]:
    return context.catalog.scopes(connection, owner, object_limit=SCOPE_OBJECT_LIMIT)


def definition() -> ToolDefinition:
    return ToolDefinition(
        name="my_scope",
        description=(
            "커넥션 선택 후 접근 범위를 파악할 때 호출한다. `owner`를 생략하면 전체 객체를 나열하지 않고 "
            "스키마별 tables·views·partitioned 개수만 반환하며, 지정하면 해당 스키마의 objects를 반환한다. "
            "이름 패턴은 `list_tables`, 키워드는 `search_catalog`로 좁히고, `NO_ACCESS` 시 이 도구로 범위를 재확인하라."
        ),
        params=(
            ParamSpec("connection", "string", required=True),
            ParamSpec("owner", "string", required=False),
        ),
        example={"name": "my_scope", "arguments": {"connection": "dw-prod"}},
        handler=my_scope,
    )


__all__ = ["definition", "my_scope"]
