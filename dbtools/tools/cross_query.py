"""Run the same SQL on multiple connections in parallel."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ..context import ToolContext
from ..errors import (
    NO_ACCESS,
    BadParamError,
    NoAccessError,
    ToolError,
)
from ..registry import ParamSpec, ToolDefinition

from ..common.params import bound_sql, bounded_int, require_nonempty_string, tool_setting
from ..common.execution import execute
from ..common.results import ora_fields, query_payload

_LOG = logging.getLogger("tool")
_INTERNAL_ERROR = "INTERNAL_ERROR"
_ACCESS_ORA_CODES = frozenset(
    {
        942,
        980,
        995,
        1004,
        1005,
        1017,
        1018,
        1031,
        1035,
        1045,
        1745,
        1917,
        1940,
        28000,
        12154,
        12505,
        12514,
        12541,
        12545,
    }
)

_DEFAULT_WORKERS = 4  # config.tools.cross_query_workers 로 조정 (pool_max 와 함께 봐야 한다)
_CROSS_QUERY_ROWS_CAP = 1000


def _is_known_permission_failure(exc: BaseException) -> bool:
    """True for ORA codes that mean "not authorized", not "query is wrong".

    ORA 코드 추출은 ``common.results.ora_fields`` 하나만 쓴다. 그 구현은 예외 체인
    (``__cause__``/``__context__``)까지 따라가므로 AdapterError 로 감싸인
    드라이버 오류도 잡힌다.
    """
    if isinstance(exc, NoAccessError):
        return True
    ora, _message, _position = ora_fields(exc)
    if isinstance(ora, str) and ora.startswith("ORA-"):
        digits = ora[4:]
        if digits.isdigit() and int(digits) in _ACCESS_ORA_CODES:
            return True
    return False


def _tool_error_item(connection: str, exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, ToolError):
        payload = exc.to_payload()["error"]
        return {"connection": connection, "error": payload}
    if _is_known_permission_failure(exc):
        ora, message, _position = ora_fields(exc)
        error: dict[str, Any] = {
            "code": NO_ACCESS,
            "message": message,
            "hint": "해당 커넥션의 권한·객체를 확인하세요",
        }
        if isinstance(ora, str) and ora.strip():
            error["ora"] = ora.strip()
        return {"connection": connection, "error": error}
    _LOG.exception("cross_query failed for connection %s", connection)
    ora, message, _position = ora_fields(exc)
    error = {
        "code": _INTERNAL_ERROR,
        "message": message,
        "hint": "내부 오류입니다. 서버 로그를 확인하세요",
    }
    if isinstance(ora, str) and ora.strip():
        error["ora"] = ora.strip()
    return {"connection": connection, "error": error}


def cross_query(
    context: ToolContext,
    *,
    sql: str,
    binds: dict[str, Any] | None = None,
    connections: list[Any] | None = None,
    max_rows: int = 100,
) -> dict[str, Any]:
    stripped, mapped = bound_sql(sql, binds)
    row_cap = bounded_int(max_rows, cap=_CROSS_QUERY_ROWS_CAP, name="max_rows")
    if connections is None:
        names = list(context.database.connection_names())
    else:
        if not isinstance(connections, list) or not connections:
            raise BadParamError(
                "connections는 비어 있지 않은 배열이어야 합니다",
                hint="커넥션 이름 목록을 전달하세요",
            )
        names = []
        seen: set[str] = set()
        for item in connections:
            name = require_nonempty_string(item, "connections")
            if name in seen:
                continue
            seen.add(name)
            context.get_connection(name)
            names.append(name)
    if not names:
        raise BadParamError("실행할 커넥션이 없습니다")

    def _run(name: str) -> dict[str, Any]:
        try:
            result = execute(context, name, stripped, mapped, max_rows=row_cap)
            return {"connection": name, **query_payload(result, max_rows=row_cap)}
        except Exception as exc:
            return _tool_error_item(name, exc)

    workers = min(tool_setting(context, "cross_query_workers", _DEFAULT_WORKERS), len(names))
    if workers <= 1:
        results = [_run(name) for name in names]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(_run, names))
    return {"results": results}


def definition() -> ToolDefinition:
    return ToolDefinition(
        name="cross_query",
        description=(
            "동일 SQL을 여러 커넥션에 병렬 실행해 커넥션명을 붙여 병합한다. "
            "일부 커넥션 실패는 해당 항목에만 error로 남는다. "
            "두 DB 결과의 차이를 보려는 목적이면 compare_schema(구조) 또는 결과를 받아 직접 비교하라. "
            "커넥션마다 테이블명이 다르면 사용할 수 없다."
        ),
        params=(
            ParamSpec("sql", "string", required=True),
            ParamSpec("binds", "dict", required=False),
            ParamSpec("connections", "list", required=False),
            ParamSpec("max_rows", "int", required=False, default=100),
        ),
        example={
            "name": "cross_query",
            "arguments": {
                "sql": "SELECT CUST_ID, COUNT(*) CNT FROM DW.F_SALES WHERE CUST_ID = :cust_id GROUP BY CUST_ID",
                "binds": {"cust_id": 1001},
                "connections": ["dw-prod", "erp-src"],
            },
        },
        handler=cross_query,
    )


__all__ = ["definition", "cross_query"]
