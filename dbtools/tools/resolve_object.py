"""Resolve a name through synonyms to OWNER.TABLE."""

from __future__ import annotations

from typing import Any

from ..context import ToolContext
from ..registry import ParamSpec, ToolDefinition

from ..common.params import require_nonempty_string


def resolve_object(context: ToolContext, *, connection: str, name: str) -> dict[str, Any]:
    context.get_connection(connection)
    require_nonempty_string(name, "name")
    return context.catalog.resolve(connection, name)


def definition() -> ToolDefinition:
    return ToolDefinition(
        name="resolve_object",
        description=(
            "이름 하나를 시노님 해석을 거쳐 `OWNER.TABLE`과 접근 가능 여부로 확정한다. "
            "사용자가 스키마 없이 테이블명만 말했거나 시노님으로 보일 때 먼저 호출하라."
        ),
        params=(
            ParamSpec("connection", "string", required=True),
            ParamSpec("name", "string", required=True),
        ),
        example={
            "name": "resolve_object",
            "arguments": {"connection": "dw-prod", "name": "SALES"},
        },
        handler=resolve_object,
    )


__all__ = ["definition", "resolve_object"]
