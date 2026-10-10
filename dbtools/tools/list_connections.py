"""Registered DB connections with status and cached schema/table counts."""

from __future__ import annotations

from typing import Any, Mapping

from ..context import ToolContext
from ..registry import ToolDefinition

def _common_code(context: ToolContext) -> dict[str, Any] | None:
    """공통코드 테이블 위치. 이름 규칙으로 찾을 수 없으므로 설정값만 신뢰한다."""
    raw = context.config.get("common_code")
    if not isinstance(raw, Mapping):
        return None
    table = raw.get("table")
    if not isinstance(table, str) or not table.strip():
        return None
    out: dict[str, Any] = {"table": table.strip().upper()}
    connection = raw.get("connection")
    if isinstance(connection, str) and connection.strip():
        out["connection"] = connection.strip()
    return out


def list_connections(context: ToolContext) -> dict[str, Any]:
    connections: list[dict[str, Any]] = []
    for status in context.catalog.connection_stats():
        item = {key: status[key] for key in ("name", "dsn", "state")}
        if status["state"] == "error" and status.get("error"):
            item["error"] = status["error"]
        for key in ("schema_count", "table_count", "cache_refreshed_at"):
            if status.get(key) is not None:
                item[key] = status[key]
        connections.append(item)
    payload: dict[str, Any] = {"connections": connections}
    common = _common_code(context)
    if common is not None:
        payload["common_code"] = common
    return payload


def definition() -> ToolDefinition:
    return ToolDefinition(
        name="list_connections",
        description=(
            "등록된 DB 커넥션 이름·상태(ok/error)와 캐시된 스키마·테이블 수를 반환한다. "
            "세션 시작 시 호출해 다른 도구의 `connection` 값을 정하라(파라미터 없음). "
            "`common_code.table`이 있으면 공통코드 테이블이고, 없으면 미등록 상태다. "
            "코드값은 `describe_table`로 구조를 확인한 뒤 `query`로 조회·조인해 해석하라. "
            "대상 테이블의 코드 컬럼명이 코드 그룹키이므로 공통코드 테이블의 그룹 컬럼을 "
            "그 이름으로 필터링하라."
        ),
        params=(),
        example={"name": "list_connections", "arguments": {}},
        handler=list_connections,
    )


__all__ = ["definition", "list_connections"]
