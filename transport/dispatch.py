"""JSON-RPC 2.0 MCP dispatch. Importing this module never opens a DB."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Mapping

from dbtools.errors import error_code, user_message
from dbtools.service import builtin_definitions, create_tool_service

from .config import build_database, collect_secrets, scrub_secrets

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

PROTOCOL_VERSION = "2025-03-26"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-03-26",)
SERVER_INFO = {"name": "dbridge", "version": "0.3.0"}
# MCP initialize.result.instructions — 클라이언트가 모델 시스템 컨텍스트에 주입한다.
# 에이전트가 도구를 쓰지 않고 테이블명을 추측하는 것을 막는 유일한 런타임 채널.
SERVER_INSTRUCTIONS = (
    "이 서버는 실 Oracle의 메타정보·데이터 검색구다. DB에 관한 모든 것을 이 도구로 찾아보고 그 결과로만 판단한다.\n"
    "기억·학습 지식·일반적인 통념으로 DB를 단정하지 마라. 테이블명·컬럼명·스키마명·타입·관계·값의 존재 여부는 전부 도구로 확인한다.\n"
    "원칙: DB 관련 질문·결정 앞에는 반드시 먼저 도구를 호출한다. 확인하지 않고 추측해서 답하면 안 된다. 모르면 물어보는 게 아니라 검색하는 게 이 서버의 존재 이유다.\n"
    "\n"
    "토큰 절약 — 넓게 나열하지 말고 좁혀서 검색하라(중요):\n"
    "- 대상 스키마에 테이블이 많다. 전체를 통째로 나열하면 응답이 거대해진다. 항상 keyword·owner·scope·pattern·limit으로 범위를 먼저 좁힌다.\n"
    "- 이름 후보를 찾을 때는 `list_tables` 전수 나열이 아니라 `search_catalog`(키워드 검색)를 우선한다.\n"
    "- 코멘트·컬럼 등 상세는 목록에서 한꺼번에 받지 말고, 필요한 테이블 하나에 `describe_table`로 좁혀서 받는다.\n"
    "- limit은 필요한 만큼만(기본으로 충분). 결과의 `truncated`가 true면 더 나열하지 말고 keyword·owner로 더 좁혀 재호출한다.\n"
    "\n"
    "드릴다운 순서(넓게→좁게, 현황 먼저):\n"
    "1) `list_connections` — 커넥션별 schema_count·table_count로 전체 규모를 본다(아주 쌀).\n"
    "2) `my_scope`(owner 없이) — 스키마별 개수 요약(tables·views·partitioned)만 받아 대상 스키마를 고른다. 여기서는 개별 객체 목록이 안 나온다(토큰 절약).\n"
    "3) 대상 스키마를 고르면 `my_scope`(owner 지정)로 그 스키마의 객체 목록을, 또는 곧바로 `search_catalog`(키워드)로 들어간다.\n"
    "4) 파티션 현황은 my_scope의 partitioned 개수로 감을 잡고, 스키마의 파티션 테이블 목록/개수는 `partition_info`(owner 지정)로 본다.\n"
    "\n"
    "메타정보 검색(주 용도):\n"
    "- `search_catalog` — 업무 용어·한글 코멘트로 테이블명·테이블 코멘트·컬럼명·컬럼 코멘트를 통합 검색한다(캐시 검색, DB 왕복 없음). 이름을 모를 때 가장 먼저 쓴다. `connection`·`owner`·`scope`로 좁혀라.\n"
    "- `describe_table` — 특정 테이블 하나의 컬럼·타입·코멘트·키를 확인한다.\n"
    "- `resolve_object` — 시노님/약칭을 실제 OWNER.TABLE로 해석한다.\n"
    "- `list_tables` — 이름 패턴을 알 때만, `pattern`으로 좁혀 나열한다. 패턴 없이 전체를 훑지 마라.\n"
    "- `suggest_joins` — 테이블 간 FK/조인 관계를 찾는다. `compare_schema` — 구조 차이 비교. `catalog_refresh` — 캐시가 오래됐을 때 갱신.\n"
    "\n"
    "데이터·프로파일 검색(대상을 먼저 확정한 뒤):\n"
    "- `count_rows` 건수, `sample_rows` 값 샘플, `table_profile` 컬럼 분포·통계, `partition_info` 파티션 구조.\n"
    "- `query_summary` 집계 요약, `cross_query` 다중 커넥션 횡단 조회.\n"
    "\n"
    "SQL이 꼭 필요할 때(마지막 수단):\n"
    "- 임의 SELECT는 `query`. SELECT만 허용되며 INSERT/UPDATE/DELETE/DDL은 거부된다.\n"
    "- 이름을 추측해 `query`를 던지지 말고, 대상은 위 검색 도구로 먼저 확정한다. 불확실하면 `validate_sql`·`explain`으로 먼저 검증한다.\n"
    "\n"
    "`NO_ACCESS` 오류는 `my_scope`로 범위를 재확인한다. 오류 메시지의 hint는 다음 도구 호출을 안내한다 — 무시하지 말고 따르라. 확인되지 않은 DB 사실은 답변에 쓰지 말고 도구로 먼저 확인한다."
)
MAX_BATCH = 64

_TYPE_MAP = {
    "string": "string",
    "int": "integer",
    "number": "number",
    "bool": "boolean",
    "list": "array",
    "dict": "object",
}

mcp_log = logging.getLogger("mcp")
tool_log = logging.getLogger("tool")
proxy_log = logging.getLogger("proxy")


def _builtin_metadata() -> list[dict[str, Any]]:
    """Tool metadata for ``tools/list``, taken from the single builtin registry."""
    return [item.to_metadata() for item in builtin_definitions()]


TOOLS: list[dict[str, Any]] = _builtin_metadata()
_TOOLS_OWNER: str | None = None


def claim_tools_owner(token: str) -> None:
    global _TOOLS_OWNER
    _TOOLS_OWNER = token


def publish_tools(metadata: list[dict[str, Any]], token: str) -> bool:
    global TOOLS
    if _TOOLS_OWNER != token:
        return False
    TOOLS[:] = [dict(item) for item in metadata]
    return True


def _input_schema(params: list[dict[str, Any]]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for spec in params:
        name = spec["name"]
        schema: dict[str, Any] = {"type": _TYPE_MAP.get(spec.get("type"), "string")}
        if "default" in spec:
            schema["default"] = spec["default"]
        properties[name] = schema
        if spec.get("required"):
            required.append(name)
    payload: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        payload["required"] = required
    return payload


def tools_as_mcp(metadata: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert internal tool metadata to MCP ``tools/list`` inputSchema shape."""
    tools: list[dict[str, Any]] = []
    for item in metadata:
        tools.append(
            {
                "name": item["name"],
                "description": item.get("description", ""),
                "inputSchema": _input_schema(list(item.get("params") or [])),
            }
        )
    return tools


def _rpc_error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": message},
    }


def _rpc_result(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def parse_message(raw: bytes) -> tuple[Any, dict[str, Any] | None]:
    """Parse one JSON-RPC body → ``(payload, error)``. 둘 중 하나만 채워진다."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, _rpc_error(None, PARSE_ERROR, "parse error")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None, _rpc_error(None, PARSE_ERROR, "parse error")
    return payload, None


def is_single_initialize(payload: Any) -> bool:
    """True for a lone ``initialize`` request — 세션을 새로 발급해야 하는 경우다."""
    return (
        isinstance(payload, dict)
        and payload.get("method") == "initialize"
        and "id" in payload
    )


class ToolGateway:
    """MCP tools/list·tools/call 의 단일 관문 + 카탈로그 워밍업 상태 보유자.

    ``warm_up()`` 이 백그라운드에서 카탈로그를 수집하는 동안에도 ``list_tools()``
    는 정적 메타데이터로 즉시 응답한다 (도구 목록은 DB 와 무관). ``call_tool()``
    은 워밍업 중이면 캐시 미완성으로 실패할 수 있고, 그건 UNKNOWN_OBJECT 로
    나간다. ``_tracker`` 는 종료 중 새 작업 유입을 막는다.
    """
    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        database: Any = None,
        base_dir: Any = None,
        owner_token: str | None = None,
        work_tracker: Any = None,
    ) -> None:
        self._secrets = collect_secrets(config)
        self._binding = build_database(config, database=database)
        self._tracker = work_tracker
        self._owner_token = owner_token
        self.database = self._binding.database
        self.probe_sql = self._binding.probe_sql
        self.driver = self._binding.driver
        self._query_timeout = _query_timeout(config)
        self.service = create_tool_service(
            scrub_secrets(config),
            base_dir=base_dir,
            database=self._binding.database,
            auto_refresh_catalog=False,
        )
        self.catalog_state = "pending"
        self.catalog_error: str | None = None
        self.extensions_state: dict[str, Any] = {
            "relationships": 0,
            "errors": [],
        }
        self.ready_since: float | None = None

    def warm_up(self, stop_event: Any = None) -> None:
        """Collect the catalog once, recording ready/error state for ``readiness()``.

        서버 기동 스레드에서 한 번 호출한다. ``stop_event`` 가 세팅되면 즉시 포기한다.
        실패해도 예외를 올리지 않고 ``catalog_error`` 에 남긴다 — DB 가 죽어도
        프록시와 UI 는 살아 있어야 한다.
        """
        if stop_event is not None and stop_event.is_set():
            return
        if self._tracker is not None and not self._tracker.begin():
            return
        try:
            if stop_event is not None and stop_event.is_set():
                return
            self.service.refresh_catalog()
            self.catalog_state = "ready"
            if stop_event is not None and stop_event.is_set():
                return
            receipt = self.service.reload_extensions()
            self.extensions_state = receipt
            for item in receipt.get("errors") or []:
                tool_log.error("extension load error: %s", item)
            if stop_event is not None and stop_event.is_set():
                return
            if self._owner_token is not None:
                publish_tools(self.service.list_tools(), self._owner_token)
            self.ready_since = time.time()
        except Exception as exc:
            self.catalog_state = "error"
            self.catalog_error = error_code(exc)
            proxy_log.warning(
                "warmup failed: %s: %s",
                type(exc).__name__,
                _safe_message(exc, self._secrets),
            )
        finally:
            if self._tracker is not None:
                self._tracker.end()

    def list_tools(self) -> list[dict[str, Any]]:
        """Static tool metadata. 카탈로그 상태와 무관하게 항상 응답한다."""
        return self.service.list_tools()

    def call_tool(self, name: str, arguments: Mapping[str, Any] | None) -> dict[str, Any]:
        """Dispatch one tool call, always returning a payload (never raising).

        ToolError 는 ``{"error": {...}}`` 로 직렬화된다. 종료 중이면 NO_ACCESS.
        """
        if self._tracker is not None and not self._tracker.begin():
            return {
                "error": {
                    "code": "NO_ACCESS",
                    "message": "server is stopping",
                    "hint": "retry after reload",
                }
            }
        try:
            result = self.service.dispatch(name, arguments)
            if isinstance(result, dict):
                return result
            return {"value": result}
        finally:
            if self._tracker is not None:
                self._tracker.end()

    def test_connection(self, name: str) -> dict[str, Any]:
        """UI 의 커넥션 테스트 버튼용 — 왕복 1회와 지연 시간만 확인한다."""
        names = set(self.database.connection_names())
        if name not in names:
            return {"ok": False, "latency_ms": None, "message": "unknown connection"}
        if self._tracker is not None and not self._tracker.begin():
            return {"ok": False, "latency_ms": None, "message": "stopping"}
        started = time.perf_counter()
        try:
            self.database.execute(
                name,
                self.probe_sql,
                max_rows=1,
                timeout_s=self._query_timeout,
            )
            latency = max(0, int((time.perf_counter() - started) * 1000))
            return {"ok": True, "latency_ms": latency, "message": ""}
        except Exception as exc:
            proxy_log.warning(
                "connection test %s failed: %s: %s",
                name,
                type(exc).__name__,
                _safe_message(exc, self._secrets),
            )
            return {
                "ok": False,
                "latency_ms": None,
                "message": user_message(exc),
            }
        finally:
            if self._tracker is not None:
                self._tracker.end()

    def health_snapshot(self) -> tuple[Any, ...]:
        if self._tracker is not None and not self._tracker.begin():
            return ()
        try:
            return tuple(self.database.snapshot())
        finally:
            if self._tracker is not None:
                self._tracker.end()

    def close(self) -> None:
        self.service.close()


def _query_timeout(config: Mapping[str, Any]) -> float | None:
    proxy = config.get("proxy")
    if isinstance(proxy, Mapping) and "query_timeout" in proxy:
        value = proxy["query_timeout"]
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return float(value)
    return None


def _safe_message(exc: BaseException, secrets: tuple[str, ...]) -> str:
    text = str(exc) or type(exc).__name__
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    line = text.splitlines()[0].strip() if text else "error"
    return line[:300]


def _negotiate_protocol(params: Any) -> str:
    requested = None
    if isinstance(params, dict):
        requested = params.get("protocolVersion")
    if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    if requested is not None:
        mcp_log.info("unsupported protocolVersion %r; using %s", requested, PROTOCOL_VERSION)
    else:
        mcp_log.info("protocolVersion missing; using %s", PROTOCOL_VERSION)
    return PROTOCOL_VERSION


def handle_message(
    message: Any,
    session: Any,
    gateway: ToolGateway,
) -> dict[str, Any] | None:
    if not isinstance(message, dict):
        return _rpc_error(None, INVALID_REQUEST, "invalid request")
    req_id = message.get("id", _MISSING)
    notify = req_id is _MISSING
    jsonrpc = message.get("jsonrpc")
    method = message.get("method")
    if jsonrpc != "2.0" or not isinstance(method, str) or not method:
        if notify:
            return None
        return _rpc_error(
            None if req_id is _MISSING else req_id,
            INVALID_REQUEST,
            "invalid request",
        )
    try:
        result = _dispatch_method(method, message.get("params"), session, gateway)
    except Exception:
        mcp_log.exception("internal error in %s", method)
        if notify:
            return None
        return _rpc_error(req_id, INTERNAL_ERROR, "internal error")
    if notify:
        return None
    if isinstance(result, dict) and result.get("_rpc_error"):
        return _rpc_error(req_id, result["code"], result["message"])
    return _rpc_result(req_id, result)


_MISSING = object()


def _dispatch_method(
    method: str,
    params: Any,
    session: Any,
    gateway: ToolGateway,
) -> Any:
    if method == "initialize":
        version = _negotiate_protocol(params)
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": dict(SERVER_INFO),
            "instructions": SERVER_INSTRUCTIONS,
        }
    if method == "notifications/initialized":
        return {}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": tools_as_mcp(gateway.list_tools())}
    if method == "tools/call":
        return _call_tool(params, gateway)
    return {"_rpc_error": True, "code": METHOD_NOT_FOUND, "message": "method not found"}


def _call_tool(params: Any, gateway: ToolGateway) -> Any:
    if not isinstance(params, dict):
        return {"_rpc_error": True, "code": INVALID_PARAMS, "message": "invalid params"}
    name = params.get("name")
    if not isinstance(name, str) or not name:
        return {"_rpc_error": True, "code": INVALID_PARAMS, "message": "invalid params"}
    arguments = params.get("arguments", {})
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return {"_rpc_error": True, "code": INVALID_PARAMS, "message": "invalid params"}
    payload = gateway.call_tool(name, arguments)
    text = json.dumps(payload, ensure_ascii=False, default=str)
    result: dict[str, Any] = {
        "content": [{"type": "text", "text": text}],
    }
    if isinstance(payload, dict) and "error" in payload:
        result["isError"] = True
    return result


def _batch_has_initialize(payload: list[Any]) -> bool:
    for item in payload:
        if isinstance(item, dict) and item.get("method") == "initialize":
            return True
    return False


def handle_payload(
    payload: Any,
    session: Any,
    gateway: ToolGateway,
) -> tuple[Any, bool]:
    if isinstance(payload, list):
        if not payload:
            return _rpc_error(None, INVALID_REQUEST, "invalid request"), False
        if len(payload) > MAX_BATCH:
            return _rpc_error(None, INVALID_REQUEST, "invalid request"), False
        if _batch_has_initialize(payload):
            return _rpc_error(None, INVALID_REQUEST, "invalid request"), False
        responses: list[dict[str, Any]] = []
        for item in payload:
            try:
                reply = handle_message(item, session, gateway)
            except Exception:
                mcp_log.exception("batch item failed")
                req_id = item.get("id") if isinstance(item, dict) else None
                reply = _rpc_error(req_id, INTERNAL_ERROR, "internal error")
            if reply is not None:
                responses.append(reply)
        if not responses:
            return None, True
        return responses, False
    if not isinstance(payload, dict):
        return _rpc_error(None, INVALID_REQUEST, "invalid request"), False
    reply = handle_message(payload, session, gateway)
    if reply is None:
        return None, True
    return reply, False


__all__ = [
    "INTERNAL_ERROR",
    "INVALID_PARAMS",
    "INVALID_REQUEST",
    "MAX_BATCH",
    "METHOD_NOT_FOUND",
    "PARSE_ERROR",
    "PROTOCOL_VERSION",
    "SERVER_INFO",
    "SERVER_INSTRUCTIONS",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "TOOLS",
    "ToolGateway",
    "claim_tools_owner",
    "handle_message",
    "handle_payload",
    "is_single_initialize",
    "parse_message",
    "publish_tools",
    "tools_as_mcp",
]
