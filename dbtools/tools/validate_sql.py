"""validate_sql tool extracted from analysis_tools."""

from __future__ import annotations

from typing import Any

from ..context import ToolContext
from ..errors import (
    InvalidSqlError,
)
from ..registry import ParamSpec, ToolDefinition
from ..sql_safety import validate_select_sql

from ..common.results import ora_fields


_VALIDATE_SQL_DESCRIPTION = (
    "Oracle 드라이버의 parse로 SQL을 실행 없이 구문·오브젝트·권한 검증한다. "
    "긴 SQL을 작성했을 때 실행 전 자가 점검용. 성능(스캔 범위·비용)까지 보려면 `explain`을 쓰라."
)


def _invalid_from_exc(exc: BaseException) -> dict[str, Any]:
    ora, message, position = ora_fields(exc)
    payload: dict[str, Any] = {
        "valid": False,
        "message": message,
        "hint": "오류 위치를 확인하고 SELECT/WITH 단일 문장만 전달하세요",
    }
    if ora:
        payload["ora"] = ora
    if position is not None:
        payload["position"] = position
    return payload


def validate_sql(context: ToolContext, *, connection: str, sql: str) -> dict[str, Any]:
    context.get_connection(connection)
    stripped = validate_select_sql(sql)
    try:
        context.database.parse(connection, stripped, timeout_s=context.query_timeout)
    except InvalidSqlError as exc:
        return _invalid_from_exc(exc)
    return {"valid": True}


def definition() -> ToolDefinition:
    return ToolDefinition(
        name="validate_sql",
        description=_VALIDATE_SQL_DESCRIPTION,
        params=(
            ParamSpec("connection", "string", required=True),
            ParamSpec("sql", "string", required=True),
        ),
        example={
            "name": "validate_sql",
            "arguments": {
                "connection": "dw-prod",
                "sql": "SELECT PROD_CD FROM DW.F_SALES WHERE BASE_DT >= :from_dt",
            },
        },
        handler=validate_sql,
    )


__all__ = ["definition", "validate_sql"]
