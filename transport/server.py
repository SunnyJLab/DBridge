"""Streamable HTTP MCP server. Importing this module never binds a socket."""

from __future__ import annotations

import copy
import queue
import json
import logging
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping
from urllib.parse import urlsplit

from .config import collect_secrets, validate_config
from .dispatch import (
    ToolGateway,
    claim_tools_owner,
    handle_payload,
    is_single_initialize,
    parse_message,
)
from .session import STREAM_SENTINEL, Session, SessionStore

MAX_BODY_BYTES = 1_048_576
SSE_KEEPALIVE_S = 15.0
HEALTH_INTERVAL_S = 10.0
DRAIN_TIMEOUT_S = 5.0
JOIN_TIMEOUT_S = 5.0
SOCKET_TIMEOUT_S = 5.0

proxy_log = logging.getLogger("proxy")
http_log = logging.getLogger("http")
pool_log = logging.getLogger("pool")


class DbWorkTracker:
    """진행 중인 DB 작업 수를 세어 안전한 종료 시점을 잡는다.

    ``mark_stopping()`` 이후 ``begin()`` 은 False 를 돌려 새 작업을 거절하고,
    ``wait_idle()`` 이 남은 작업을 기다린다. ``try_claim_close()`` 는 풀 종료를
    정확히 한 번만 수행하도록 소유권을 주장한다 (중복 close 방지).
    """
    def __init__(self, closer: Any) -> None:
        self._lock = threading.Lock()
        self._count = 0
        self._zero = threading.Event()
        self._zero.set()
        self._stopping = False
        self._close_claimed = False
        self._closer = closer

    def begin(self) -> bool:
        with self._lock:
            if self._stopping:
                return False
            self._count += 1
            self._zero.clear()
            return True

    def end(self) -> None:
        claim = False
        with self._lock:
            if self._count > 0:
                self._count -= 1
            if self._count == 0:
                self._zero.set()
                if self._stopping and not self._close_claimed:
                    self._close_claimed = True
                    claim = True
        if claim:
            self._closer()

    def mark_stopping(self) -> None:
        with self._lock:
            self._stopping = True
            if self._count == 0:
                self._zero.set()

    def wait_idle(self, timeout: float) -> bool:
        return self._zero.wait(timeout)

    def try_claim_close(self) -> bool:
        with self._lock:
            if self._count != 0 or self._close_claimed:
                return False
            self._close_claimed = True
            return True

    @property
    def pending(self) -> int:
        with self._lock:
            return self._count

    @property
    def close_claimed(self) -> bool:
        with self._lock:
            return self._close_claimed


class _McpHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, server_address: tuple[str, int], RequestHandlerClass: Any, proxy: "ProxyServer") -> None:
        self.proxy = proxy
        super().__init__(server_address, RequestHandlerClass)

    def process_request(self, request: Any, client_address: Any) -> None:
        try:
            request.settimeout(SOCKET_TIMEOUT_S)
        except OSError:
            pass
        super().process_request(request, client_address)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = SOCKET_TIMEOUT_S

    @property
    def proxy(self) -> "ProxyServer":
        return self.server.proxy  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        http_log.info("%s - " + fmt, self.address_string(), *args)

    def do_POST(self) -> None:
        """JSON-RPC 요청 본문 1건 처리 — MCP 의 주 경로."""
        if not self._check_path():
            return
        self.proxy._bump_requests()
        if not self._check_post_headers():
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        payload, parse_error = parse_message(body)
        if parse_error is not None:
            header = self.headers.get("Mcp-Session-Id")
            sid = None
            if header:
                existing = self.proxy.sessions.get(header)
                if existing is not None:
                    sid = existing.id
            self._send_json(200, parse_error, session_id=sid)
            return
        session, _marker, created = self._resolve_session(payload)
        if session is None:
            return
        if created:
            self.proxy.sessions.mark_initialized(session.id)
        reply, notify_only = handle_payload(payload, session, self.proxy.gateway)
        if notify_only:
            self._send_json(202, None, session_id=session.id)
            return
        self._send_json(200, reply, session_id=session.id)

    def do_GET(self) -> None:
        """SSE 스트림 개설(서버→클라이언트 알림용). 세션에 소켓을 등록해 둔다."""
        if not self._check_path():
            return
        self.proxy._bump_requests()
        accept = (self.headers.get("Accept") or "").lower()
        if not _accepts(accept, ("text/event-stream", "*/*")):
            self._reject(406)
            return
        session = self._require_session()
        if session is None:
            return
        token = self.proxy.sessions.attach_stream(session.id)
        if token is None:
            self._send_json(409, {"error": "sse already active"}, session_id=session.id, close=True)
            return
        self._send_sse(session, token)

    def do_DELETE(self) -> None:
        """세션 종료 요청."""
        if not self._check_path():
            return
        self.proxy._bump_requests()
        session = self._require_session()
        if session is None:
            return
        self.proxy.sessions.close(session.id)
        self._send_json(200, {"ok": True}, session_id=session.id)

    def do_PUT(self) -> None:
        self._reject(404)

    def do_PATCH(self) -> None:
        self._reject(404)

    def do_OPTIONS(self) -> None:
        self._reject(404)

    def _check_path(self) -> bool:
        path = urlsplit(self.path).path
        if path != "/mcp":
            self._reject(404)
            return False
        return True

    def _check_post_headers(self) -> bool:
        encoding = (self.headers.get("Transfer-Encoding") or "").strip().lower()
        if encoding and encoding != "identity":
            http_log.info("unsupported Transfer-Encoding: %s", encoding)
            self._reject(501)
            return False
        length_values = self.headers.get_all("Content-Length")
        if not length_values:
            self._reject(411)
            return False
        # Leading OWS is stripped by the stdlib (email/http) parser before values
        # reach this check, so arrivals here are already normalized (RFC 7230 OWS
        # allowed). Differing/conflicting Content-Length duplicates are rejected;
        # identical-value duplicates are allowed. Other smuggling vectors we
        # reject are non-ASCII/non-digit, trailing space/tab, and negatives.
        # The later session gate validates the session, not Content-Length;
        # a leading-OWS request that becomes 400 here is that sessionless request.
        parsed_lengths: list[int] = []
        for raw in length_values:
            text = raw if isinstance(raw, str) else str(raw or "")
            if not text or not text.isascii() or not text.isdigit():
                self._reject(400)
                return False
            try:
                parsed_lengths.append(int(text))
            except ValueError:
                self._reject(400)
                return False
        if any(value != parsed_lengths[0] for value in parsed_lengths):
            self._reject(400)
            return False
        length = parsed_lengths[0]
        if length < 0:
            self._reject(400)
            return False
        if length > MAX_BODY_BYTES:
            self._reject(413)
            return False
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            self._reject(415)
            return False
        accept = (self.headers.get("Accept") or "").lower()
        if not _accepts(accept, ("application/json", "text/event-stream", "*/*")):
            self._reject(406)
            return False
        return True

    def _resolve_session(self, payload: Any) -> tuple[Session | None, Any, bool]:
        header = self.headers.get("Mcp-Session-Id")
        if is_single_initialize(payload) and not header:
            session = self.proxy.sessions.create()
            return session, session.id, True
        if not header:
            self._send_json(400, {"error": "missing session"}, close=True)
            return None, False, False
        session = self.proxy.sessions.get(header)
        if session is None:
            self._send_json(404, {"error": "unknown session"}, close=True)
            return None, False, False
        return session, header, False

    def _require_session(self) -> Session | None:
        header = self.headers.get("Mcp-Session-Id")
        if not header:
            self._send_json(400, {"error": "missing session"}, close=True)
            return None
        session = self.proxy.sessions.get(header)
        if session is None:
            self._send_json(404, {"error": "unknown session"}, close=True)
            return None
        return session

    def _reject(self, status: int) -> None:
        self._send_json(status, None, close=True)

    def _send_json(
        self,
        status: int,
        payload: Any = None,
        *,
        session_id: str | None = None,
        close: bool = False,
    ) -> None:
        if payload is None:
            body = b""
        else:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        if body:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if session_id:
            self.send_header("Mcp-Session-Id", session_id)
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        if body:
            self.wfile.write(body)
            self.wfile.flush()

    def _send_sse(self, session: Session, token: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Mcp-Session-Id", session.id)
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
        except OSError:
            self.proxy.sessions.detach_stream(session.id, token)
            return
        sock = getattr(self, "connection", None) or getattr(self, "request", None)
        self.proxy.sessions.set_stream_socket(session.id, token, sock)
        try:
            while not session.close_event.is_set():
                try:
                    item = session.queue.get(timeout=self.proxy.sse_keepalive_s)
                except queue.Empty:
                    if session.close_event.is_set():
                        break
                    try:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                    except OSError:
                        break
                    continue
                if item is STREAM_SENTINEL or session.close_event.is_set():
                    break
                try:
                    data = json.dumps(item, ensure_ascii=False, default=str)
                    frame = f"event: message\ndata: {data}\n\n".encode("utf-8")
                    self.wfile.write(frame)
                    self.wfile.flush()
                except OSError:
                    break
        finally:
            self.proxy.sessions.detach_stream(session.id, token)


def _accepts(header: str, allowed: tuple[str, ...]) -> bool:
    if not header.strip():
        return True
    parts = [item.strip().split(";")[0].strip() for item in header.split(",")]
    return any(part in allowed for part in parts)


class ProxyServer:
    """127.0.0.1 전용 Streamable HTTP MCP 서버 + 관리 UI 가 읽는 상태 소스.

    ``start()`` 는 소켓을 열고 카탈로그 워밍업을 백그라운드로 띄운다.
    ``stop()`` 은 새 요청을 막고 → in-flight DB 작업을 기다리고 → 세션 스트림을
    깨우고 → 풀을 닫는다. 설정 변경은 이 객체를 버리고 새로 만드는 방식이다
    (부분 재설정 없음).
    """
    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        database: Any = None,
        base_dir: Any = None,
        drain_timeout_s: float | None = None,
    ) -> None:
        self.config = validate_config(config)
        self._secrets = collect_secrets(self.config)
        self._lock = threading.Lock()
        self._lifecycle = "idle"
        self._listening = False
        self._started_at: float | None = None
        self._request_count = 0
        self._host = self.config["proxy"]["host"]
        self._port = int(self.config["proxy"]["port"])
        self._drain_timeout_s = (
            DRAIN_TIMEOUT_S if drain_timeout_s is None else float(drain_timeout_s)
        )
        self.sse_keepalive_s = SSE_KEEPALIVE_S
        self._owner_token = f"srv-{time.time_ns()}-{id(self)}"
        claim_tools_owner(self._owner_token)
        self.sessions = SessionStore()
        self._tracker = DbWorkTracker(self._close_gateway)
        self.gateway = ToolGateway(
            self.config,
            database=database,
            base_dir=base_dir,
            owner_token=self._owner_token,
            work_tracker=self._tracker,
        )
        self._httpd: _McpHTTPServer | None = None
        self._serve_thread: threading.Thread | None = None
        self._health_thread: threading.Thread | None = None
        self._warmup_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._health_cache: list[dict[str, Any]] = self._placeholder_connections()
        self._closed = False

    def start(self) -> None:
        with self._lock:
            if self._lifecycle not in ("idle",):
                raise RuntimeError("server already started")
            self._lifecycle = "running"
            self._stop_event.clear()
        httpd = _McpHTTPServer((self._host, self._port), _Handler, self)
        self._httpd = httpd
        self._port = int(httpd.server_address[1])
        self._started_at = time.time()
        self._listening = True
        serve_thread = threading.Thread(
            target=httpd.serve_forever,
            name="mcp-http",
            daemon=True,
        )
        health_thread = threading.Thread(
            target=self._health_loop,
            name="mcp-health",
            daemon=True,
        )
        warmup_thread = threading.Thread(
            target=self._warmup_loop,
            name="mcp-warmup",
            daemon=True,
        )
        self._serve_thread = serve_thread
        self._health_thread = health_thread
        self._warmup_thread = warmup_thread
        serve_thread.start()
        warmup_thread.start()
        health_thread.start()
        proxy_log.info("listening on http://%s:%s/mcp", self._host, self._port)

    def stop(self) -> None:
        with self._lock:
            if self._lifecycle == "idle":
                self._lifecycle = "stopped"
                return
            if self._lifecycle == "stopped":
                return
            first = self._lifecycle == "running"
            self._lifecycle = "stopping"
            self._listening = False
        if first:
            self._stop_event.set()
            stream_sockets = self.sessions.active_stream_sockets()
            self.sessions.close_all()
            httpd = self._httpd
            if httpd is not None:
                closer = threading.Thread(target=httpd.shutdown, name="mcp-shutdown", daemon=True)
                closer.start()
                closer.join(JOIN_TIMEOUT_S)
                for sock in stream_sockets:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    try:
                        sock.close()
                    except OSError:
                        pass
                try:
                    httpd.server_close()
                except OSError:
                    pass
            self._tracker.mark_stopping()
            self._join_owned()
        drained = self._tracker.wait_idle(self._drain_timeout_s)
        if drained and self._tracker.try_claim_close():
            self._close_gateway()
            with self._lock:
                self._lifecycle = "stopped"
            return
        if not drained:
            proxy_log.warning(
                "drain pending: %s remaining DB op(s), shutdown incomplete",
                self._tracker.pending,
            )

    def snapshot(self) -> dict[str, Any]:
        """Listening state, address, uptime, and request counters for the UI."""
        with self._lock:
            connections = copy.deepcopy(self._health_cache)
            payload = {
                "listening": bool(self._listening),
                "host": self._host,
                "port": self._port,
                "started_at": self._started_at,
                "request_count": self._request_count,
                "session_count": self.sessions.count(),
                "connections": connections,
            }
        return payload

    def readiness(self) -> dict[str, Any]:
        """Catalog/extension warm-up state for the UI (기동 직후엔 ``pending``)."""
        with self._lock:
            lifecycle = self._lifecycle
        return {
            "lifecycle": lifecycle,
            "db_work_pending": self._tracker.pending,
            "catalog": self.gateway.catalog_state,
            "extensions": copy.deepcopy(self.gateway.extensions_state),
        }

    def test_connection(self, name: str) -> dict[str, Any]:
        """UI 의 커넥션 테스트용 — 왕복 1회와 지연 시간만 확인한다."""
        with self._lock:
            lifecycle = self._lifecycle
        if lifecycle != "running":
            return {"ok": False, "latency_ms": None, "message": "stopping"}
        return self.gateway.test_connection(name)

    def tools(self) -> list[dict[str, Any]]:
        return self.gateway.list_tools()

    def call_tool(self, name: str, arguments: Mapping[str, Any] | None) -> dict[str, Any]:
        with self._lock:
            lifecycle = self._lifecycle
        if lifecycle != "running":
            return {"error": {"code": "NO_ACCESS", "message": "server is stopping",
                              "hint": "retry after reload"}}
        return self.gateway.call_tool(name, arguments)

    def _bump_requests(self) -> None:
        with self._lock:
            self._request_count += 1

    def _placeholder_connections(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in self.config.get("connections") or []:
            if not isinstance(item, Mapping):
                continue
            row: dict[str, Any] = {
                "name": item.get("name"),
                "dsn": item.get("dsn"),
                "state": "error",
                "latency_ms": None,
                "pool_used": 0,
                "pool_max": item.get("pool_max", 5),
            }
            if self.gateway.catalog_error:
                row["error"] = self.gateway.catalog_error
            rows.append(row)
        return rows

    def _health_loop(self) -> None:
        while not self._stop_event.is_set() and self.gateway.catalog_state == "pending":
            if self._stop_event.wait(0.05):
                return
        if self._stop_event.is_set():
            return
        self._sample_health()
        while not self._stop_event.wait(HEALTH_INTERVAL_S):
            self.sessions.sweep()
            self._sample_health()

    def _sample_health(self) -> None:
        try:
            statuses = self.gateway.health_snapshot()
        except Exception:
            pool_log.exception("health snapshot failed")
            return
        if not statuses:
            return
        rows: list[dict[str, Any]] = []
        for status in statuses:
            if hasattr(status, "as_dict"):
                payload = status.as_dict()
            elif isinstance(status, Mapping):
                payload = dict(status)
            else:
                continue
            row: dict[str, Any] = {
                "name": payload.get("name"),
                "dsn": payload.get("dsn"),
                "state": payload.get("state"),
                "latency_ms": payload.get("latency_ms"),
                "pool_used": payload.get("pool_used", 0),
                "pool_max": payload.get("pool_max", 0),
            }
            error = payload.get("error")
            if not error and row["state"] != "ok":
                error = self.gateway.catalog_error
            if error:
                row["error"] = error
            rows.append(row)
        if rows:
            with self._lock:
                self._health_cache = rows

    def _warmup_loop(self) -> None:
        self.gateway.warm_up(self._stop_event)

    def _join_owned(self) -> None:
        for thread in (self._serve_thread, self._health_thread, self._warmup_thread):
            if thread is not None and thread.is_alive() and thread is not threading.current_thread():
                thread.join(JOIN_TIMEOUT_S)

    def _close_gateway(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.gateway.close()
        except Exception:
            proxy_log.exception("gateway close failed")
        with self._lock:
            if self._lifecycle == "stopping" and self._tracker.pending == 0:
                self._lifecycle = "stopped"


def create_server(
    config: Mapping[str, Any],
    *,
    database: Any = None,
    base_dir: Any = None,
    drain_timeout_s: float | None = None,
) -> ProxyServer:
    return ProxyServer(
        config,
        database=database,
        base_dir=base_dir,
        drain_timeout_s=drain_timeout_s,
    )


__all__ = [
    "DRAIN_TIMEOUT_S",
    "HEALTH_INTERVAL_S",
    "MAX_BODY_BYTES",
    "ProxyServer",
    "SSE_KEEPALIVE_S",
    "create_server",
]
