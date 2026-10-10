"""Explicit assembly of the 18 built-in MCP DB tools.

Production code never scans the package or imports tools dynamically.
Each module exposes the original handler name plus ``definition()``.

핸들러 함수에는 의도적으로 docstring 을 두지 않는다. 도구의 의미·파라미터·
사용 시점은 ``definition()`` 의 ``description`` 이 단일 출처이고 그 문자열이
그대로 MCP ``tools/list`` 로 나가 에이전트가 읽는다. 핸들러에 같은 설명을
복사하면 두 곳이 갈라진다. 핸들러 내부의 비자명한 판단만 인라인 주석으로 남긴다.
"""

from __future__ import annotations

from ..registry import ToolDefinition
from . import (
    catalog_refresh,
    compare_schema,
    count_rows,
    cross_query,
    describe_table,
    explain,
    list_connections,
    list_tables,
    my_scope,
    partition_info,
    query,
    query_summary,
    resolve_object,
    sample_rows,
    search_catalog,
    suggest_joins,
    table_profile,
    validate_sql,
)

_TOOL_MODULES = (
    list_connections,
    my_scope,
    list_tables,
    describe_table,
    search_catalog,
    catalog_refresh,
    sample_rows,
    count_rows,
    query,
    resolve_object,
    validate_sql,
    explain,
    partition_info,
    table_profile,
    cross_query,
    query_summary,
    suggest_joins,
    compare_schema,
)


def definitions() -> tuple[ToolDefinition, ...]:
    """Return a fresh ToolDefinition for each built-in tool in contract order."""
    return tuple(module.definition() for module in _TOOL_MODULES)


__all__ = ["definitions"]
