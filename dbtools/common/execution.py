"""Query execution helper for tool handlers.

``database.execute`` has exactly one signature (see :mod:`dbtools.database`),
so handlers call it directly. There is deliberately no signature probing: a
``TypeError`` here is a real bug and must surface, not be retried under
another shape and re-raised with the original cause lost.

Depends on ``results`` for row-shaping; must not import ``params`` or
``catalog_access``.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..context import ToolContext
from .results import row_maps

# 딕셔너리 뷰 조회처럼 행 수를 미리 알 수 없는 내부 질의의 상한.
DICT_FETCH_CAP = 1_000_000


def execute(
    context: ToolContext,
    connection: str,
    sql: str,
    binds: Mapping[str, Any] | None = None,
    *,
    max_rows: int,
) -> Any:
    """Run SQL on a named connection under the context's query timeout."""
    return context.database.execute(
        connection,
        sql,
        binds=binds,
        max_rows=max_rows,
        timeout_s=context.query_timeout,
    )


def query_maps(
    context: ToolContext,
    connection: str,
    sql: str,
    binds: Mapping[str, Any] | None = None,
    *,
    max_rows: int = DICT_FETCH_CAP,
) -> tuple[list[dict[str, Any]], bool]:
    """Execute and return ``(rows as upper-cased dicts, truncated)``."""
    return row_maps(execute(context, connection, sql, binds, max_rows=max_rows))


__all__ = [
    "DICT_FETCH_CAP",
    "execute",
    "query_maps",
]
