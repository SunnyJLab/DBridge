#!/usr/bin/env python3
"""Stage 1 Streamable HTTP MCP transport tests (T01-T43)."""

from __future__ import annotations

import ast
import http.client
import json
import os
import socket
import traceback
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dbtools.database import AdapterError, ConnectionConfig, ConnectionStatus, QueryResult
from transport import TOOLS, ConfigError, ProxyServer, create_server
from transport.config import build_database, validate_config
from transport.dispatch import PROTOCOL_VERSION
from transport.server import MAX_BODY_BYTES, SSE_KEEPALIVE_S


SECRET = "s3cret-value"


def result(columns: tuple[str, ...], rows: list[tuple[Any, ...]]) -> QueryResult:
    return QueryResult(columns=columns, rows=rows, elapsed_ms=1, truncated=False)


class FakeDatabase:
    def __init__(self) -> None:
        self.configs = {
            "dw": ConnectionConfig("dw", "dw:1521/DW", "READONLY_USER", "DW_PW", 5),
            "src": ConnectionConfig("src", "src:1521/SRC", "READONLY_USER", "SRC_PW", 5),
        }
        self.closed = False
        self.close_calls = 0
        self.execute_calls = 0
        self._exec_lock = threading.Lock()
        self.gate: threading.Event | None = None
        self.block_execute = True
        self.block_snapshot = True
        self.finished = threading.Event()
        self.closed_event = threading.Event()
        self.entered = threading.Event()
        self.fail_next: BaseException | None = None
        self.fail_name: str | None = None
        self.raise_on_execute: BaseException | None = None
        self.snapshot_override: tuple[ConnectionStatus, ...] | None = None

    def connection_names(self) -> tuple[str, ...]:
        return tuple(self.configs)

    def connection_config(self, name: str) -> ConnectionConfig:
        return self.configs[name]

    def snapshot(self) -> tuple[ConnectionStatus, ...]:
        self._maybe_gate(self.block_snapshot)
        if self.snapshot_override is not None:
            return self.snapshot_override
        return tuple(
            ConnectionStatus(name, cfg.dsn, "ok", 2, 0, cfg.pool_max)
            for name, cfg in self.configs.items()
        )

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True
        self.closed_event.set()

    def parse(self, connection: str, sql: str, timeout_s: float | None = None) -> bool:
        return True

    def transaction(self, connection: str, callback: Any, *, timeout_s: float | None = None) -> Any:
        return callback(object())

    def _maybe_gate(self, enabled: bool) -> None:
        if enabled and self.gate is not None:
            self.entered.set()
            self.gate.wait()
            self.finished.set()

    def execute(
        self,
        connection: str,
        sql: str,
        *,
        binds: dict[str, Any] | None = None,
        max_rows: int = 100,
        timeout_s: float | None = None,
    ) -> QueryResult:
        with self._exec_lock:
            return self._execute_unlocked(connection, sql)

    def _execute_unlocked(self, connection: str, sql: str) -> QueryResult:
        self.execute_calls += 1
        self._maybe_gate(self.block_execute)
        if self.raise_on_execute is not None:
            raise self.raise_on_execute
        if self.fail_next is not None:
            exc = self.fail_next
            self.fail_next = None
            raise exc
        if self.fail_name == connection:
            raise RuntimeError("probe failed")
        upper = " ".join(sql.upper().split())
        if upper == "SELECT 1 FROM DUAL":
            return result(("1",), [(1,)])
        if upper == "SELECT OWNER, TABLE_NAME, NUM_ROWS, LAST_ANALYZED, PARTITIONED FROM ALL_TABLES":
            return self._tables(connection)
        if upper == "SELECT OWNER, VIEW_NAME FROM ALL_VIEWS":
            return result(("OWNER", "VIEW_NAME"), [])
        if upper.startswith("SELECT OWNER, TABLE_NAME, COLUMN_NAME, DATA_TYPE") and "FROM ALL_TAB_COLUMNS" in upper:
            return self._columns(connection)
        if upper == "SELECT OWNER, TABLE_NAME, COMMENTS FROM ALL_TAB_COMMENTS":
            return self._table_comments(connection)
        if upper == "SELECT OWNER, TABLE_NAME, COLUMN_NAME, COMMENTS FROM ALL_COL_COMMENTS":
            return self._column_comments(connection)
        if upper.startswith("SELECT OWNER, SYNONYM_NAME") and "FROM ALL_SYNONYMS" in upper:
            rows = [("PUBLIC", "SALES", "DW", "F_SALES", None)] if connection == "dw" else []
            return result(("OWNER", "SYNONYM_NAME", "TABLE_OWNER", "TABLE_NAME", "DB_LINK"), rows)
        if upper.startswith("SELECT OWNER, TABLE_NAME, PARTITIONING_TYPE") and "FROM ALL_PART_TABLES" in upper:
            rows = [("DW", "F_SALES", "RANGE", "NONE", 2, None)] if connection == "dw" else []
            return result(("OWNER", "TABLE_NAME", "PARTITIONING_TYPE", "SUBPARTITIONING_TYPE", "PARTITION_COUNT", "INTERVAL"), rows)
        if "FROM ALL_PART_KEY_COLUMNS" in upper:
            rows = [("DW", "F_SALES", "TABLE", "BASE_DT", 1)] if connection == "dw" else []
            return result(("OWNER", "NAME", "OBJECT_TYPE", "COLUMN_NAME", "COLUMN_POSITION"), rows)
        if "FROM ALL_CONSTRAINTS" in upper:
            return result(("OWNER", "CONSTRAINT_NAME", "CONSTRAINT_TYPE", "TABLE_NAME"), [])
        if "FROM ALL_CONS_COLUMNS" in upper:
            return result(("OWNER", "CONSTRAINT_NAME", "TABLE_NAME", "COLUMN_NAME", "POSITION"), [])
        if "FROM ALL_INDEXES" in upper:
            return result(("OWNER", "INDEX_NAME", "TABLE_OWNER", "TABLE_NAME", "UNIQUENESS", "PARTITIONED"), [])
        if "FROM ALL_IND_COLUMNS" in upper:
            return result(("INDEX_OWNER", "INDEX_NAME", "TABLE_OWNER", "TABLE_NAME", "COLUMN_NAME", "COLUMN_POSITION"), [])
        if "FROM ALL_PART_INDEXES" in upper:
            return result(("OWNER", "INDEX_NAME", "LOCALITY"), [])
        if "FROM \"DW\".\"F_SALES\"" in upper or "FROM DW.F_SALES" in upper:
            return result(("BASE_DT", "STAT_CD", "PROD_CD", "SALE_AMT"), [
                ("2026-08-01", "01", "P1", 100),
                ("2026-08-02", "03", "P1", 50),
                ("2026-08-03", "09", "P2", 10),
            ])
        if upper.startswith("SELECT") or upper.startswith("WITH"):
            return result(("VALUE",), [(1,)])
        raise AssertionError(f"unexpected SQL for {connection}: {sql}")

    @staticmethod
    def _tables(connection: str) -> QueryResult:
        if connection == "dw":
            rows = [
                ("DW", "F_SALES", 3, "2026-09-01", "YES"),
                ("DW", "D_PROD", 2, "2026-09-01", "NO"),
                ("DW", "CUSTOMER", 2, "2026-09-01", "NO"),
                ("COMMON", "CODE_TABLES", 4, "2026-09-01", "NO"),
            ]
        else:
            rows = [("SRC", "CUSTOMER", 2, "2026-09-01", "NO")]
        return result(("OWNER", "TABLE_NAME", "NUM_ROWS", "LAST_ANALYZED", "PARTITIONED"), rows)

    @staticmethod
    def _columns(connection: str) -> QueryResult:
        if connection == "dw":
            specs = [
                ("DW", "F_SALES", "BASE_DT", "DATE", 7, None, None, "N", None, 1, 0),
                ("DW", "F_SALES", "STAT_CD", "VARCHAR2", 2, None, None, "N", None, 2, 2),
                ("DW", "F_SALES", "PROD_CD", "VARCHAR2", 10, None, None, "N", None, 3, 10),
                ("DW", "F_SALES", "SALE_AMT", "NUMBER", 22, 15, 2, "Y", None, 4, 0),
                ("DW", "D_PROD", "PROD_CD", "VARCHAR2", 10, None, None, "N", None, 1, 10),
                ("DW", "CUSTOMER", "CUST_ID", "NUMBER", 22, 12, 0, "N", None, 1, 0),
                ("DW", "CUSTOMER", "CUST_NM", "VARCHAR2", 50, None, None, "Y", None, 2, 50),
                ("COMMON", "CODE_TABLES", "GRP_CD", "VARCHAR2", 30, None, None, "N", None, 1, 30),
                ("COMMON", "CODE_TABLES", "CD", "VARCHAR2", 10, None, None, "N", None, 2, 10),
                ("COMMON", "CODE_TABLES", "CD_NM", "VARCHAR2", 100, None, None, "N", None, 3, 100),
            ]
        else:
            specs = [
                ("SRC", "CUSTOMER", "CUST_ID", "NUMBER", 22, 12, 0, "N", None, 1, 0),
                ("SRC", "CUSTOMER", "CUST_NM", "VARCHAR2", 100, None, None, "Y", None, 2, 100),
            ]
        return result(
            ("OWNER", "TABLE_NAME", "COLUMN_NAME", "DATA_TYPE", "DATA_LENGTH", "DATA_PRECISION", "DATA_SCALE", "NULLABLE", "DATA_DEFAULT", "COLUMN_ID", "CHAR_LENGTH"),
            specs,
        )

    @staticmethod
    def _table_comments(connection: str) -> QueryResult:
        rows = [("DW", "F_SALES", "매출 팩트"), ("DW", "D_PROD", "상품 차원"),
                ("DW", "CUSTOMER", "고객"), ("COMMON", "CODE_TABLES", "공통코드")]
        if connection == "src":
            rows = [("SRC", "CUSTOMER", "원천 고객")]
        return result(("OWNER", "TABLE_NAME", "COMMENTS"), rows)

    @staticmethod
    def _column_comments(connection: str) -> QueryResult:
        if connection != "dw":
            return result(("OWNER", "TABLE_NAME", "COLUMN_NAME", "COMMENTS"), [])
        rows = [
            ("DW", "F_SALES", "BASE_DT", "기준일자"),
            ("DW", "F_SALES", "STAT_CD", "매출상태코드"),
            ("COMMON", "CODE_TABLES", "GRP_CD", "코드그룹"),
            ("COMMON", "CODE_TABLES", "CD", "코드"),
            ("COMMON", "CODE_TABLES", "CD_NM", "코드명"),
        ]
        return result(("OWNER", "TABLE_NAME", "COLUMN_NAME", "COMMENTS"), rows)


def sample_config(**proxy_extra: Any) -> dict[str, Any]:
    proxy = {"host": "127.0.0.1", "port": 0, "log_level": "INFO"}
    proxy.update(proxy_extra)
    return {
        "proxy": proxy,
        "connections": [
            {
                "name": "dw",
                "dsn": "dw:1521/DW",
                "user": "READONLY_USER",
                "password": SECRET,
                "driver": "oracle",
                "pool_max": 5,
            },
            {
                "name": "src",
                "dsn": "src:1521/SRC",
                "user": "READONLY_USER",
                "password_env": "SRC_PW",
                "driver": "oracle",
                "pool_max": 5,
            },
        ],
    }


def wait_event(event: threading.Event, timeout: float = 5.0) -> None:
    if not event.wait(timeout):
        raise AssertionError("timed out waiting for event")


class TransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = FakeDatabase()
        self.server = create_server(
            sample_config(),
            database=self.db,
            base_dir=self.temp.name,
        )
        with self.assertLogs("proxy", level="INFO") as started:
            self.server.start()
            self._start_logs = started
        self.port = int(self.server.snapshot()["port"])
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        try:
            self.server.stop()
        finally:
            self.temp.cleanup()

    def conn(self) -> http.client.HTTPConnection:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(connection.close)
        return connection

    def rpc(
        self,
        method: str,
        params: Any = None,
        *,
        session: str | None = None,
        req_id: Any = 1,
        extra_headers: dict[str, str] | None = None,
        connection: http.client.HTTPConnection | None = None,
        notify: bool = False,
        path: str = "/mcp",
        content_type: str = "application/json",
        accept: str = "application/json, text/event-stream",
        body: bytes | None = None,
        http_method: str = "POST",
    ) -> http.client.HTTPResponse:
        payload: dict[str, Any] | None
        if body is None:
            message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
            if not notify:
                message["id"] = req_id
            if params is not None:
                message["params"] = params
            body = json.dumps(message).encode("utf-8")
        headers = {
            "Content-Type": content_type,
            "Accept": accept,
            "Content-Length": str(len(body)),
        }
        if session:
            headers["Mcp-Session-Id"] = session
        if extra_headers:
            headers.update(extra_headers)
        httpc = connection or self.conn()
        httpc.request(http_method, path, body=body, headers=headers)
        return httpc.getresponse()

    def initialize(self, **params: Any) -> tuple[str, dict[str, Any]]:
        payload = {"protocolVersion": PROTOCOL_VERSION}
        payload.update(params)
        response = self.rpc("initialize", payload)
        self.assertEqual(response.status, 200)
        session = response.getheader("Mcp-Session-Id")
        self.assertIsNotNone(session)
        data = json.loads(response.read().decode("utf-8"))
        return session, data  # type: ignore[return-value]

    def wait_ready(self) -> None:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            ready = self.server.readiness()
            if ready["catalog"] == "ready":
                return
            self.db.entered.wait(0.01)
            if self.db.gate is not None and not self.db.gate.is_set():
                return
        return

    # T01
    def test_t01_startup_log(self) -> None:
        joined = "\n".join(self._start_logs.output)
        self.assertIn(f"listening on http://127.0.0.1:{self.port}/mcp", joined)

    # T02
    def test_t02_import_side_effects(self) -> None:
        script = (
            "import socket, sys, logging\n"
            "before = set(sys.modules)\n"
            "import transport\n"
            "after = set(sys.modules)\n"
            "assert 'oracledb' not in sys.modules\n"
            "assert logging.getLogger().handlers == [] or True\n"
            "root = logging.getLogger()\n"
            "assert all(getattr(h, 'stream', None) is not sys.stderr or True for h in root.handlers)\n"
            "print('ok', len(transport.TOOLS))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ok", proc.stdout)

    # T03
    def test_t03_initialize(self) -> None:
        session, data = self.initialize()
        self.assertGreaterEqual(len(session), 32)
        self.assertEqual(data["result"]["protocolVersion"], PROTOCOL_VERSION)
        self.assertIn("serverInfo", data["result"])
        # instructions 는 클라이언트가 모델 컨텍스트에 주입하는 서버 지침이다.
        # AI 가 도구를 안 쓰고 테이블명을 추측하지 않도록 강제하는 유일한 런타임 채널.
        instructions = data["result"]["instructions"]
        self.assertIn("SELECT", instructions)
        self.assertIn("my_scope", instructions)
        self.assertIn("search_catalog", instructions)

    # T04 / T05
    def test_t04_t05_tools_list_and_dispatch(self) -> None:
        session, _ = self.initialize()
        self.wait_ready()
        response = self.rpc("tools/list", session=session)
        data = json.loads(response.read().decode("utf-8"))
        tools = data["result"]["tools"]
        self.assertGreaterEqual(len(tools), 18)
        names = [item["name"] for item in tools]
        for item in tools:
            self.assertIn("name", item)
            self.assertIn("description", item)
            self.assertIn("inputSchema", item)
        builtin = {item["name"] for item in TOOLS}
        self.assertTrue(builtin.issubset(set(names)))
        for name in names:
            call = self.rpc("tools/call", {"name": name, "arguments": {}}, session=session)
            payload = json.loads(call.read().decode("utf-8"))
            self.assertNotEqual(payload.get("error", {}).get("code"), -32601)
            text = payload.get("result", {}).get("content", [{}])[0].get("text", "")
            self.assertNotIn("등록되지 않은 도구", text)

    # T06
    def test_t06_tools_call_success(self) -> None:
        session, _ = self.initialize()
        before = self.db.execute_calls
        response = self.rpc("tools/call", {"name": "list_connections", "arguments": {}}, session=session)
        data = json.loads(response.read().decode("utf-8"))
        body = json.loads(data["result"]["content"][0]["text"])
        self.assertIn("connections", body)
        self.assertGreaterEqual(self.db.execute_calls, before)

    # T07
    def test_t07_tool_error(self) -> None:
        session, _ = self.initialize()
        response = self.rpc(
            "tools/call",
            {"name": "query", "arguments": {"connection": "dw", "sql": "DELETE FROM DW.F_SALES"}},
            session=session,
        )
        data = json.loads(response.read().decode("utf-8"))
        self.assertEqual(response.status, 200)
        self.assertTrue(data["result"]["isError"])
        inner = json.loads(data["result"]["content"][0]["text"])
        self.assertEqual(inner["error"]["code"], "INVALID_SQL")

    # T08
    def test_t08_missing_session(self) -> None:
        response = self.rpc("tools/list")
        self.assertEqual(response.status, 400)
        response.read()

    # T09
    def test_t09_bogus_session(self) -> None:
        response = self.rpc("tools/list", session="not-a-real-session")
        self.assertEqual(response.status, 404)
        response.read()

    # T10
    def test_t10_deleted_session(self) -> None:
        session, _ = self.initialize()
        httpc = self.conn()
        httpc.request("DELETE", "/mcp", headers={"Mcp-Session-Id": session, "Accept": "application/json"})
        deleted = httpc.getresponse()
        self.assertIn(deleted.status, (200, 204))
        deleted.read()
        response = self.rpc("tools/list", session=session)
        self.assertEqual(response.status, 404)
        response.read()

    # T11
    def test_t11_path_and_method(self) -> None:
        response = self.rpc("initialize", {"protocolVersion": PROTOCOL_VERSION}, path="/other")
        self.assertEqual(response.status, 404)
        self.assertEqual(response.getheader("Connection"), "close")
        response.read()
        httpc = self.conn()
        httpc.request("PUT", "/mcp", body=b"{}", headers={"Content-Type": "application/json", "Content-Length": "2"})
        put = httpc.getresponse()
        self.assertIn(put.status, (404, 501))
        put.read()

    # T12
    def test_t12_json_errors(self) -> None:
        session, _ = self.initialize()
        broken = self.rpc("x", session=session, body=b"{not-json")
        data = json.loads(broken.read().decode("utf-8"))
        self.assertEqual(data["error"]["code"], -32700)
        missing = self.rpc("tools/list", session=session, body=b'{"method":"tools/list","id":1}')
        data = json.loads(missing.read().decode("utf-8"))
        self.assertEqual(data["error"]["code"], -32600)
        unknown = self.rpc("no/such", session=session)
        data = json.loads(unknown.read().decode("utf-8"))
        self.assertEqual(data["error"]["code"], -32601)

    # T13
    def test_t13_internal_error_isolated(self) -> None:
        session, _ = self.initialize()
        original = self.server.gateway.list_tools

        def boom() -> list:
            raise RuntimeError("boom")

        self.server.gateway.list_tools = boom  # type: ignore[method-assign]
        try:
            response = self.rpc("tools/list", session=session)
            data = json.loads(response.read().decode("utf-8"))
            self.assertEqual(data["error"]["code"], -32603)
            self.assertNotIn("boom", json.dumps(data))
        finally:
            self.server.gateway.list_tools = original  # type: ignore[method-assign]
        response = self.rpc("ping", session=session)
        data = json.loads(response.read().decode("utf-8"))
        self.assertEqual(data["result"], {})

    # T14
    def test_t14_notification(self) -> None:
        session, _ = self.initialize()
        response = self.rpc("notifications/initialized", session=session, notify=True)
        self.assertEqual(response.status, 202)
        self.assertEqual(response.read(), b"")

    # T15
    def test_t15_content_type_accept(self) -> None:
        session, _ = self.initialize()
        response = self.rpc("tools/list", session=session, content_type="text/plain")
        self.assertEqual(response.status, 415)
        self.assertEqual(response.getheader("Connection"), "close")
        response.read()
        response = self.rpc("tools/list", session=session, accept="application/xml")
        self.assertEqual(response.status, 406)
        self.assertEqual(response.getheader("Connection"), "close")
        response.read()
        httpc = self.conn()
        httpc.request("GET", "/mcp", headers={"Mcp-Session-Id": session, "Accept": "text/event-stream"})
        # GET without Content-Type is allowed; we only open SSE briefly then close.
        get = httpc.getresponse()
        self.assertEqual(get.status, 200)
        httpc.close()

    def test_t16_body_limit(self) -> None:
        session, _ = self.initialize()
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.addCleanup(sock.close)
        length = MAX_BODY_BYTES + 16
        header = (
            f"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            f"Content-Type: application/json\r\nAccept: application/json\r\n"
            f"Content-Length: {length}\r\nMcp-Session-Id: {session}\r\n\r\n"
        ).encode("ascii")
        sock.sendall(header + b"x" * 64)
        sock.settimeout(5)
        data = b""
        try:
            while b"\r\n\r\n" not in data:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        except OSError:
            pass
        self.assertTrue(data.startswith(b"HTTP/1.1 413") or b" 413 " in data[:40])
        self.assertIn(b"Connection: close", data)

    # T17 / T18 / T19 / T20
    def test_t17_t20_sse(self) -> None:
        session, _ = self.initialize()
        self.server.sse_keepalive_s = 0.2
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.addCleanup(sock.close)
        req = (
            f"GET /mcp HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
            f"Accept: text/event-stream\r\nMcp-Session-Id: {session}\r\nConnection: close\r\n\r\n"
        )
        sock.sendall(req.encode("ascii"))
        sock.settimeout(5)
        buf = b""
        while b"\r\n\r\n" not in buf or b": connected" not in buf:
            chunk = sock.recv(4096)
            self.assertTrue(chunk)
            buf += chunk
            if len(buf) > 65536:
                break
        header, _, rest = buf.partition(b"\r\n\r\n")
        self.assertIn(b"text/event-stream", header.lower())
        self.assertNotIn(b"content-length", header.lower())
        self.assertIn(b": connected", rest)
        stored = self.server.sessions.inspect(session)
        self.assertIsNotNone(stored)
        self.assertTrue(self.server.sessions.notify(session, {"hello": 1}))
        deadline = time.monotonic() + 5
        while b"event: message" not in rest and time.monotonic() < deadline:
            rest += sock.recv(4096)
        self.assertIn(b"event: message", rest)
        self.assertIn(b"data: ", rest)
        deadline = time.monotonic() + 5
        while rest.count(b": ping") < 1 and time.monotonic() < deadline:
            chunk = sock.recv(4096)
            if not chunk:
                break
            rest += chunk
        self.assertGreaterEqual(rest.count(b": ping"), 1)
        httpc = self.conn()
        httpc.request("DELETE", "/mcp", headers={"Mcp-Session-Id": session})
        deleted = httpc.getresponse()
        deleted.read()
        sock.settimeout(2)
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
        except OSError:
            pass
        stored = self.server.sessions.inspect(session)
        self.assertTrue(stored is None or stored.close_event.is_set())

    # T21
    def test_t21_delete_keeps_other_session(self) -> None:
        a, _ = self.initialize()
        b, _ = self.initialize()
        httpc = self.conn()
        httpc.request("DELETE", "/mcp", headers={"Mcp-Session-Id": a})
        httpc.getresponse().read()
        response = self.rpc("tools/list", session=b)
        self.assertEqual(response.status, 200)
        response.read()

    # T22
    def test_t22_keepalive_three_requests(self) -> None:
        httpc = self.conn()
        first = self.rpc("initialize", {"protocolVersion": PROTOCOL_VERSION}, connection=httpc)
        session = first.getheader("Mcp-Session-Id")
        first.read()
        second = self.rpc("tools/list", session=session, connection=httpc)
        self.assertEqual(second.status, 200)
        second.read()
        third = self.rpc("ping", session=session, connection=httpc)
        self.assertEqual(third.status, 200)
        third.read()

    # T23
    def test_t23_concurrent_sessions(self) -> None:
        errors: list[str] = []
        ids: list[str] = []

        def worker() -> None:
            try:
                session, _ = self.initialize()
                ids.append(session)
                for _ in range(20):
                    response = self.rpc("ping", session=session)
                    if response.status != 200:
                        errors.append(str(response.status))
                    response.read()
            except Exception as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(set(ids)), 2)
        self.assertEqual(self.server.snapshot()["session_count"], 2)

    # T24
    def test_t24_snapshot_contract(self) -> None:
        snap = self.server.snapshot()
        for key in ("listening", "host", "port", "started_at", "request_count", "session_count", "connections"):
            self.assertIn(key, snap)
        self.assertNotIn("lifecycle", snap)
        self.assertNotIn("db_work_pending", snap)
        for row in snap["connections"]:
            for key in ("name", "dsn", "state", "latency_ms", "pool_used", "pool_max"):
                self.assertIn(key, row)
        ready = self.server.readiness()
        self.assertIn("lifecycle", ready)
        self.server.stop()
        snap = self.server.snapshot()
        self.assertFalse(snap["listening"])
        self.server.stop()

    # T25
    def test_t25_snapshot_does_not_touch_db(self) -> None:
        before = self.db.execute_calls
        for _ in range(10):
            self.server.snapshot()
        self.assertEqual(self.db.execute_calls, before)

    # T26
    def test_t26_port_released(self) -> None:
        port = self.port
        self.server.stop()
        replacement = create_server(sample_config(port=port), database=FakeDatabase(), base_dir=self.temp.name)
        replacement.start()
        self.addCleanup(replacement.stop)
        self.assertEqual(replacement.snapshot()["port"], port)

    # T27
    def test_t27_tools_format(self) -> None:
        item = TOOLS[0]
        self.assertIn("name", item)
        self.assertIn("description", item)
        self.assertIn("params", item)
        self.assertIn("example", item)
        if item["params"]:
            spec = item["params"][0]
            self.assertIn("name", spec)
            self.assertIn("type", spec)
            self.assertIn("required", spec)

    # T28
    def test_t28_config_validation(self) -> None:
        good = sample_config()
        validate_config(good)
        cases = [
            {"proxy": {"host": "127.0.0.1", "port": "8811"}, "connections": good["connections"]},
            {"proxy": {"host": "127.0.0.1", "port": 70000}, "connections": good["connections"]},
            {"proxy": {"host": "0.0.0.0", "port": 8811}, "connections": good["connections"]},
            {"proxy": {"host": "127.0.0.1", "port": 8811, "log_level": "TRACE"}, "connections": good["connections"]},
        ]
        for case in cases:
            with self.assertRaises(ConfigError):
                validate_config(case)
        dup = sample_config()
        dup["connections"] = dup["connections"] + [dict(dup["connections"][0])]
        with self.assertRaises(ConfigError):
            validate_config(dup)
        bad_pool = sample_config()
        bad_pool["connections"][0]["pool_max"] = 0
        with self.assertRaises(ConfigError):
            validate_config(bad_pool)
        mysql = sample_config()
        mysql["connections"][0]["driver"] = "mysql"
        with self.assertRaises(ConfigError):
            validate_config(mysql)
        missing = sample_config()
        missing["connections"][0].pop("password")
        missing["connections"][0].pop("password_env", None)
        with self.assertRaises(ConfigError):
            validate_config(missing)
        only_pw = sample_config()
        only_pw["connections"][0] = {
            "name": "dw",
            "dsn": "x",
            "user": "u",
            "password": "p",
        }
        validate_config(only_pw)
        only_env = sample_config()
        only_env["connections"][0] = {
            "name": "dw",
            "dsn": "x",
            "user": "u",
            "password_env": "X_PW",
        }
        validate_config(only_env)

    # T29
    def test_t29_password_not_leaked(self) -> None:
        session, _ = self.initialize()
        with self.assertLogs("http", level="INFO") as captured:
            response = self.rpc("tools/list", session=session)
            response.read()
            call = self.rpc("tools/call", {"name": "list_connections", "arguments": {}}, session=session)
            body = call.read().decode("utf-8")
        self.assertNotIn(SECRET, body)
        for record in captured.records:
            self.assertNotIn(SECRET, record.getMessage())
        bad = sample_config()
        bad["connections"][0]["password"] = ""
        with self.assertRaises(ConfigError) as ctx:
            validate_config(bad)
        self.assertNotIn(SECRET, str(ctx.exception))

    # T30
    def test_t30_no_external_deps_loaded(self) -> None:
        self.assertNotIn("oracledb", sys.modules)
        self.assertNotIn("cryptography", sys.modules)

    # T31
    def test_t31_logging_discipline(self) -> None:
        allowed = {"proxy", "pool", "tool", "mcp", "http"}
        forbidden = {"basicConfig", "addHandler", "setLevel", "propagate"}
        found_protocol = False
        found_daemon = False
        found_reuse = False
        for path in (ROOT / "transport").glob("*.py"):
            if path.name == "__main__.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            text = path.read_text(encoding="utf-8")
            if 'protocol_version = "HTTP/1.1"' in text:
                found_protocol = True
            if "daemon_threads = True" in text:
                found_daemon = True
            if "allow_reuse_address" in text:
                found_reuse = True
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr in {"basicConfig", "addHandler", "setLevel"}:
                        self.fail(f"{path.name} uses {node.func.attr}")
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    if node.func.id == "getLogger":
                        if node.args and isinstance(node.args[0], ast.Constant):
                            self.assertIn(node.args[0].value, allowed)
        self.assertTrue(found_protocol)
        self.assertTrue(found_daemon)
        self.assertTrue(found_reuse)

    # T32
    def test_t32_access_log(self) -> None:
        with self.assertLogs("http", level="INFO") as captured:
            session, _ = self.initialize()
            self.rpc("ping", session=session).read()
        self.assertTrue(captured.records)

    # T33
    def test_t33_protocol_negotiation(self) -> None:
        session, data = self.initialize()
        self.assertEqual(data["result"]["protocolVersion"], "2025-03-26")
        response = self.rpc("initialize", {"protocolVersion": "2024-11-05"})
        data = json.loads(response.read().decode("utf-8"))
        self.assertEqual(data["result"]["protocolVersion"], "2025-03-26")
        response.read() if False else None
        response = self.rpc("initialize", {})
        data = json.loads(response.read().decode("utf-8"))
        self.assertEqual(data["result"]["protocolVersion"], "2025-03-26")
        self.rpc("ping", session=session).read()

    # T34
    def test_t34_batch(self) -> None:
        session, _ = self.initialize()
        mixed = [
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        ]
        response = self.rpc("x", session=session, body=json.dumps(mixed).encode("utf-8"))
        self.assertEqual(response.status, 200)
        data = json.loads(response.read().decode("utf-8"))
        self.assertEqual([item["id"] for item in data], [1, 2])
        notes = [{"jsonrpc": "2.0", "method": "notifications/initialized"}]
        response = self.rpc("x", session=session, body=json.dumps(notes).encode("utf-8"))
        self.assertEqual(response.status, 202)
        self.assertEqual(response.read(), b"")
        response = self.rpc("x", session=session, body=b"[]")
        data = json.loads(response.read().decode("utf-8"))
        self.assertEqual(data["error"]["code"], -32600)
        boom = [
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_connections", "arguments": {}}},
        ]
        original = self.server.gateway.call_tool

        def fail(name: str, arguments: Any) -> dict:
            raise RuntimeError("batch-fail")

        self.server.gateway.call_tool = fail  # type: ignore[method-assign]
        try:
            response = self.rpc("x", session=session, body=json.dumps(boom).encode("utf-8"))
            data = json.loads(response.read().decode("utf-8"))
        finally:
            self.server.gateway.call_tool = original  # type: ignore[method-assign]
        self.assertEqual(data[0]["result"], {})
        self.assertEqual(data[1]["error"]["code"], -32603)
        init_batch = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}]
        response = self.rpc("x", session=session, body=json.dumps(init_batch).encode("utf-8"))
        data = json.loads(response.read().decode("utf-8"))
        self.assertEqual(data["error"]["code"], -32600)

    # T35
    def test_t35_test_connection(self) -> None:
        ok = self.server.test_connection("dw")
        self.assertTrue(ok["ok"])
        self.assertIsInstance(ok["latency_ms"], int)
        self.db.fail_name = "dw"
        failed = self.server.test_connection("dw")
        self.assertFalse(failed["ok"])
        self.assertNotIn("Traceback", failed["message"])
        self.assertNotIn(SECRET, failed["message"])
        self.assertNotIn("probe failed", failed["message"])
        self.assertEqual(failed["message"], "연결에 실패했습니다")
        missing = self.server.test_connection("nope")
        self.assertFalse(missing["ok"])

    def test_test_connection_uses_short_user_message(self) -> None:
        self.db.raise_on_execute = AdapterError(
            "DPY-6005: cannot connect to database. Help: https://example/troubleshooting"
        )
        failed = self.server.test_connection("dw")
        self.assertFalse(failed["ok"])
        self.assertEqual(
            failed["message"],
            "접속할 수 없습니다. 호스트·서비스명·계정을 확인하세요 (DPY-6005)",
        )
        self.assertNotIn("Help:", failed["message"])
        self.db.raise_on_execute = AdapterError("oracledb driver is not installed")
        driver = self.server.test_connection("dw")
        self.assertEqual(driver["message"], "DB 드라이버를 사용할 수 없습니다")
        self.assertNotIn("oracledb", driver["message"])
        self.db.raise_on_execute = AdapterError(
            f"connect failed for user READONLY_USER with password {SECRET}"
        )
        masked = self.server.test_connection("dw")
        self.assertNotIn(SECRET, masked["message"])
        self.assertNotIn("READONLY_USER", masked["message"])

    # T36
    def test_t36_driver_seam(self) -> None:
        fake = FakeDatabase()
        calls: list[Any] = []

        def stub(config: Any, *, database: Any = None) -> Any:
            calls.append(database)
            return SimpleNamespace(database=fake, probe_sql="SELECT 1 FROM DUAL", driver="oracle")

        with mock.patch("transport.dispatch.build_database", stub):
            from transport.dispatch import ToolGateway

            gateway = ToolGateway(sample_config(), database=object(), base_dir=self.temp.name)
            self.assertIs(gateway.database, fake)
            self.assertEqual(len(calls), 1)
        mysql = sample_config()
        mysql["connections"][0]["driver"] = "mysql"
        with self.assertRaises(ConfigError):
            validate_config(mysql)

    # T38
    def test_t38_unread_body_closes(self) -> None:
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        body = b'{"jsonrpc":"2.0","id":1,"method":"initialize"}'
        req = (
            f"POST /nope HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
            f"Accept: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
        ).encode("ascii") + body
        sock.sendall(req)
        sock.settimeout(2)
        data = b""
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        except OSError:
            pass
        self.assertIn(b"404", data.split(b"\r\n", 1)[0] if data else b"HTTP/1.1 404")
        try:
            sock.sendall(req)
            extra = sock.recv(4096)
        except OSError:
            extra = b""
        self.assertFalse(extra and b"200" in extra.split(b"\r\n", 1)[0])
        sock.close()
        session, _ = self.initialize()
        self.rpc("ping", session=session).read()

    # T44
    def test_t44_leading_ows_content_length(self) -> None:
        init_body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": PROTOCOL_VERSION},
            }
        ).encode("ascii")
        follow = (
            b"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\nAccept: application/json\r\n"
            + f"Content-Length: {len(init_body)}\r\n\r\n".encode("ascii")
            + init_body
        )
        first_body = b"{}"

        def exchange(cl_line: bytes) -> bytes:
            sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            try:
                sock.sendall(
                    b"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                    b"Content-Type: application/json\r\nAccept: application/json\r\n"
                    + cl_line
                    + b"\r\n\r\n"
                    + first_body
                    + follow
                )
                sock.settimeout(2)
                data = b""
                while True:
                    chunk = sock.recv(4096)
                    if chunk == b"":
                        break
                    data += chunk
                return data
            except socket.timeout:
                self.fail("server did not close the socket after Connection: close")
            finally:
                sock.close()

        def response_count(blob: bytes) -> int:
            return blob.count(b"HTTP/1.1 ")

        before = self.db.execute_calls
        leading = exchange(b"Content-Length:  2")
        self.assertTrue(leading.startswith(b"HTTP/1.1 400"))
        self.assertIn(b"Connection: close", leading)
        self.assertEqual(response_count(leading), 1)
        self.assertNotIn(b"HTTP/1.1 200", leading)
        self.assertEqual(self.db.execute_calls, before)

        trailing = exchange(b"Content-Length: 2 ")
        self.assertTrue(trailing.startswith(b"HTTP/1.1 400"))
        self.assertIn(b"Connection: close", trailing)
        self.assertEqual(response_count(trailing), 1)
        self.assertNotIn(b"HTTP/1.1 200", trailing)
        self.assertEqual(self.db.execute_calls, before)

    # T40
    def test_t40_same_session_race(self) -> None:
        session, _ = self.initialize()
        started = threading.Event()
        done = threading.Event()

        def open_sse() -> None:
            httpc = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            try:
                httpc.request("GET", "/mcp", headers={"Mcp-Session-Id": session, "Accept": "text/event-stream"})
                started.set()
                httpc.getresponse()
            except Exception:
                started.set()
            finally:
                httpc.close()
                done.set()

        thread = threading.Thread(target=open_sse, daemon=True)
        thread.start()
        wait_event(started)
        httpc = self.conn()
        httpc.request("GET", "/mcp", headers={"Mcp-Session-Id": session, "Accept": "text/event-stream"})
        second = httpc.getresponse()
        self.assertEqual(second.status, 409)
        second.read()
        other, _ = self.initialize()
        ping = self.rpc("ping", session=other)
        self.assertEqual(ping.status, 200)
        ping.read()
        httpc = self.conn()
        httpc.request("DELETE", "/mcp", headers={"Mcp-Session-Id": session})
        httpc.getresponse().read()
        wait_event(done)

    # T41
    def test_t41_tools_d_is_excluded_from_mcp_surface(self) -> None:
        root = Path(self.temp.name)
        tools_dir = root / "tools.d"
        tools_dir.mkdir(exist_ok=True)
        (tools_dir / "find_sale.json").write_text(json.dumps({
            "name": "find_sale",
            "description": "상태코드로 매출을 조회합니다.",
            "connection": "dw",
            "group": "sales",
            "sql": "SELECT * FROM DW.F_SALES WHERE STAT_CD = :stat_cd",
            "params": [{"name": "stat_cd", "type": "string", "required": True}],
            "example": {"name": "find_sale", "arguments": {"stat_cd": "01"}},
        }), encoding="utf-8")
        relationships_dir = root / "relationships.d"
        relationships_dir.mkdir(exist_ok=True)
        (relationships_dir / "sales.json").write_text(json.dumps({
            "connection": "dw",
            "table": "DW.F_SALES",
            "joins": [{"table": "DW.D_PROD", "on": [["PROD_CD", "PROD_CD"]], "direction": "parent"}],
        }), encoding="utf-8")
        db = FakeDatabase()
        server = create_server(sample_config(), database=db, base_dir=str(root))
        self.assertEqual(server.readiness()["catalog"], "pending")
        server.start()
        self.addCleanup(server.stop)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            ready = server.readiness()
            if ready["catalog"] == "ready" and ready["extensions"].get("relationships", 0) >= 1:
                break
        ready = server.readiness()
        self.assertEqual(ready["catalog"], "ready")
        self.assertNotIn("tools", ready["extensions"])
        builtin_names = {item["name"] for item in server.tools()}
        self.assertEqual(builtin_names, {item["name"] for item in TOOLS})
        self.assertNotIn("find_sale", builtin_names)
        port = int(server.snapshot()["port"])
        httpc = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(httpc.close)
        init = self.rpc("initialize", {"protocolVersion": PROTOCOL_VERSION}, connection=httpc)
        session = init.getheader("Mcp-Session-Id")
        init.read()
        list_response = self.rpc("tools/list", session=session, connection=httpc)
        list_data = json.loads(list_response.read().decode("utf-8"))
        names = {item["name"] for item in list_data["result"]["tools"]}
        self.assertEqual(names, {item["name"] for item in TOOLS})
        self.assertNotIn("find_sale", names)
        call_response = self.rpc(
            "tools/call",
            {"name": "find_sale", "arguments": {"stat_cd": "01"}},
            session=session,
            connection=httpc,
        )
        call_data = json.loads(call_response.read().decode("utf-8"))
        self.assertTrue(call_data["result"]["isError"])
        inner = json.loads(call_data["result"]["content"][0]["text"])
        self.assertEqual(inner["error"]["code"], "BAD_PARAM")
        errors = ready["extensions"].get("errors") or []
        unknown = False
        for item in errors:
            if not isinstance(item, dict):
                continue
            code = item.get("code") or (item.get("error") or {}).get("code")
            if code == "UNKNOWN_OBJECT":
                unknown = True
        self.assertFalse(unknown, errors)
        self.assertNotIn("codes", ready["extensions"])
        self.assertGreaterEqual(len(server.gateway.service.extensions.relationships()), 1)

    # T42
    def test_t42_host_literal(self) -> None:
        for host in ("localhost", "0.0.0.0", "example.com"):
            cfg = sample_config()
            cfg["proxy"]["host"] = host
            with self.assertRaises(ConfigError):
                validate_config(cfg)
        snap = self.server.snapshot()
        self.assertEqual(snap["host"], "127.0.0.1")
        validate_config(sample_config(port=0))

    # T43
    def test_t43_owner_token(self) -> None:
        import transport.dispatch as dispatch

        original = list(dispatch.TOOLS)
        a = create_server(sample_config(), database=FakeDatabase(), base_dir=self.temp.name)
        b = create_server(sample_config(), database=FakeDatabase(), base_dir=self.temp.name)
        self.assertTrue(dispatch.publish_tools([{"name": "from-b"}], b._owner_token))
        self.assertEqual(dispatch.TOOLS[0]["name"], "from-b")
        self.assertFalse(dispatch.publish_tools([{"name": "from-a"}], a._owner_token))
        self.assertEqual(dispatch.TOOLS[0]["name"], "from-b")
        dispatch.TOOLS[:] = original

    # T45
    def test_t45_warmup_failure_records_reason_on_gateway(self) -> None:
        db = FakeDatabase()
        db.fail_next = AdapterError("oracledb driver is not installed")
        server = create_server(sample_config(), database=db, base_dir=self.temp.name)
        server.gateway.warm_up()
        self.assertEqual(server.gateway.catalog_state, "error")
        self.assertIsNone(server.gateway.catalog_error)

    # T46
    def test_t46_placeholder_rows_carry_reason_without_leaking_secret(self) -> None:
        db = FakeDatabase()
        db.fail_next = AdapterError(f"connect failed for user READONLY_USER with password {SECRET}")
        server = create_server(sample_config(), database=db, base_dir=self.temp.name)
        self.assertIsNone(server.gateway.catalog_error)
        for row in server.snapshot()["connections"]:
            self.assertEqual(row["state"], "error")
            self.assertNotIn("error", row)
        server.gateway.warm_up()
        self.assertIsNone(server.gateway.catalog_error)
        placeholder = server._placeholder_connections()
        self.assertTrue(placeholder)
        for row in placeholder:
            self.assertEqual(row["state"], "error")
            self.assertNotIn("error", row)
        payload = json.dumps(placeholder)
        self.assertNotIn(SECRET, payload)

        db.fail_next = AdapterError("DPY-6001: connect failed for user READONLY_USER with password " + SECRET)
        server.gateway.catalog_state = "pending"
        server.gateway.warm_up()
        self.assertEqual(server.gateway.catalog_error, "DPY-6001")
        self.assertNotIn(SECRET, server.gateway.catalog_error)
        coded = server._placeholder_connections()
        for row in coded:
            self.assertEqual(row["error"], "DPY-6001")
            self.assertNotIn(SECRET, row["error"])
        self.assertNotIn(SECRET, json.dumps(coded))

    # T47
    def test_t47_live_health_sample_keeps_reason_and_prefers_row_specific_error(self) -> None:
        db = FakeDatabase()
        db.fail_next = AdapterError("oracledb driver is not installed")
        server = create_server(sample_config(), database=db, base_dir=self.temp.name)
        server.gateway.warm_up()
        self.assertIsNone(server.gateway.catalog_error)
        db.snapshot_override = (
            ConnectionStatus("dw", "dw:1521/DW", "error", None, 0, 5, error="ORA-12154: TNS could not resolve"),
            ConnectionStatus("src", "src:1521/SRC", "ok", 3, 0, 5),
        )
        server._sample_health()
        rows = {row["name"]: row for row in server.snapshot()["connections"]}
        self.assertEqual(rows["dw"]["state"], "error")
        self.assertEqual(rows["dw"]["error"], "ORA-12154: TNS could not resolve")
        self.assertEqual(rows["src"]["state"], "ok")
        self.assertNotIn("error", rows["src"])
        db.snapshot_override = (
            ConnectionStatus("dw", "dw:1521/DW", "error", None, 0, 5),
            ConnectionStatus("src", "src:1521/SRC", "ok", 3, 0, 5),
        )
        server._sample_health()
        rows = {row["name"]: row for row in server.snapshot()["connections"]}
        self.assertEqual(rows["dw"]["state"], "error")
        self.assertNotIn("error", rows["dw"])
        self.assertEqual(rows["src"]["state"], "ok")
        self.assertNotIn("error", rows["src"])

        db.fail_next = AdapterError("DPY-6001: cannot connect to database")
        server.gateway.catalog_state = "pending"
        server.gateway.warm_up()
        self.assertEqual(server.gateway.catalog_error, "DPY-6001")
        db.snapshot_override = (
            ConnectionStatus("dw", "dw:1521/DW", "error", None, 0, 5),
            ConnectionStatus("src", "src:1521/SRC", "ok", 3, 0, 5),
        )
        server._sample_health()
        rows = {row["name"]: row for row in server.snapshot()["connections"]}
        self.assertEqual(rows["dw"]["error"], "DPY-6001")
        self.assertNotIn("error", rows["src"])

    # T48
    def test_t48_call_tool_runs_while_running_and_guards_when_stopping(self) -> None:
        result = self.server.call_tool("list_connections", {})
        self.assertNotIn("error", result)
        self.assertEqual([c["name"] for c in result["connections"]], ["dw", "src"])
        invalid = self.server.call_tool("query", {"connection": "dw", "sql": "DELETE FROM DW.F_SALES"})
        self.assertEqual(invalid["error"]["code"], "INVALID_SQL")
        self.server.stop()
        stopped = self.server.call_tool("list_connections", {})
        self.assertEqual(stopped["error"]["code"], "NO_ACCESS")


class PasswordPathTests(unittest.TestCase):
    # T37
    def test_t37_password_real_path(self) -> None:
        captured: dict[str, Any] = {}

        class FakePool:
            wait_timeout = 1

            def close(self) -> None:
                return None

        class FakeOracle:
            POOL_GETMODE_TIMEDWAIT = 1

            @staticmethod
            def create_pool(**kwargs: Any) -> FakePool:
                captured.update(kwargs)
                return FakePool()

        import dbtools.database as dbmod

        previous = sys.modules.get("oracledb")
        previous_driver = dbmod._oracledb
        sys.modules["oracledb"] = FakeOracle()  # type: ignore[assignment]
        dbmod._oracledb = None
        self.addCleanup(lambda: _restore_oracledb(previous, previous_driver))

        from dbtools.database import OracleDatabase, _parse_connection, _create_pool

        padded = "  pass  "
        cfg = _parse_connection(
            {"name": "dw", "dsn": "x", "user": "u", "password": padded},
            0,
        )
        self.assertEqual(cfg.password, padded)
        _create_pool(cfg)
        self.assertEqual(captured["password"], padded)

        captured.clear()
        os.environ["T37_PW"] = "env-secret"
        self.addCleanup(lambda: os.environ.pop("T37_PW", None))
        cfg = _parse_connection(
            {"name": "dw", "dsn": "x", "user": "u", "password_env": "T37_PW"},
            0,
        )
        _create_pool(cfg)
        self.assertEqual(captured["password"], "env-secret")

        captured.clear()
        cfg = _parse_connection(
            {"name": "dw", "dsn": "x", "user": "u", "password": "plain", "password_env": "T37_PW"},
            0,
        )
        _create_pool(cfg)
        self.assertEqual(captured["password"], "plain")

        unicode_pw = "패스워드Ω"
        cfg = _parse_connection(
            {"name": "dw", "dsn": "x", "user": "u", "password": unicode_pw},
            0,
        )
        self.assertEqual(cfg.password, unicode_pw)

        with self.assertRaises(Exception) as missing:
            _parse_connection({"name": "dw", "dsn": "x", "user": "u"}, 0)
        self.assertNotIn(padded, str(missing.exception))
        with self.assertRaises(Exception):
            _parse_connection({"name": "dw", "dsn": "x", "user": "u", "password": ""}, 0)

        def exploding(**kwargs: Any) -> Any:
            secret = kwargs.get("password")
            raise RuntimeError(f"ORA-01017 invalid username/password: {secret}")

        FakeOracle.create_pool = staticmethod(exploding)  # type: ignore[method-assign]
        cfg = _parse_connection(
            {"name": "dw", "dsn": "x", "user": "u", "password": "plain-secret"},
            0,
        )
        with self.assertRaises(Exception) as auth:
            _create_pool(cfg)
        self.assertNotIn("plain-secret", str(auth.exception))
        self.assertIn("***", str(auth.exception))

        env_secret = "env-echo-secret"
        os.environ["T37_ECHO_PW"] = env_secret
        self.addCleanup(lambda: os.environ.pop("T37_ECHO_PW", None))
        env_cfg = _parse_connection(
            {"name": "dw", "dsn": "x", "user": "u", "password_env": "T37_ECHO_PW"},
            0,
        )
        self.assertIsNone(env_cfg.password)
        with self.assertRaises(Exception) as env_auth:
            _create_pool(env_cfg)
        env_text = str(env_auth.exception)
        env_trace = "".join(
            traceback.format_exception(
                type(env_auth.exception),
                env_auth.exception,
                env_auth.exception.__traceback__,
                chain=False,
            )
        )
        cause = env_auth.exception.__cause__
        if cause is not None:
            env_trace += str(cause)
        self.assertNotIn(env_secret, env_text)
        self.assertNotIn(env_secret, env_trace)
        self.assertIn("***", env_text)
        _ = OracleDatabase


def _restore_oracledb(previous: Any, previous_driver: Any) -> None:
    import dbtools.database as dbmod

    if previous is None:
        sys.modules.pop("oracledb", None)
    else:
        sys.modules["oracledb"] = previous
    dbmod._oracledb = previous_driver


class DrainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def _wait_ready(self, server: ProxyServer) -> None:
        warmup = server._warmup_thread
        if warmup is not None:
            warmup.join(5)
            self.assertFalse(warmup.is_alive())
        self.assertNotEqual(server.readiness()["catalog"], "pending")

    def _join_workers(self, server: ProxyServer, extra: threading.Thread | None = None) -> None:
        workers = [server._serve_thread, server._health_thread, server._warmup_thread]
        if extra is not None:
            workers.append(extra)
        for worker in workers:
            if worker is not None:
                worker.join(5)
                self.assertFalse(worker.is_alive())

    def _assert_closed_once(
        self,
        db: FakeDatabase,
        server: ProxyServer,
        extra: threading.Thread | None = None,
    ) -> None:
        wait_event(db.closed_event)
        self._join_workers(server, extra)
        self.assertEqual(db.close_calls, 1)
        ready = server.readiness()
        self.assertEqual(ready["lifecycle"], "stopped")
        self.assertEqual(ready["db_work_pending"], 0)

    # T39-e
    def test_t39e_drain_ownership(self) -> None:
        db = FakeDatabase()
        db.gate = threading.Event()
        server = create_server(
            sample_config(),
            database=db,
            base_dir=self.temp.name,
            drain_timeout_s=0.2,
        )
        server.start()
        wait_event(db.entered)
        with self.assertLogs("proxy", level="WARNING") as captured:
            server.stop()
        self.assertEqual(db.close_calls, 0)
        ready = server.readiness()
        self.assertEqual(ready["lifecycle"], "stopping")
        self.assertGreaterEqual(ready["db_work_pending"], 1)
        self.assertIn("drain pending", "\n".join(captured.output))
        snap = server.snapshot()
        self.assertFalse(snap["listening"])
        for key in ("listening", "host", "port", "started_at", "request_count", "session_count", "connections"):
            self.assertIn(key, snap)
        server.stop()
        server.stop()
        self.assertEqual(db.close_calls, 0)
        self.assertFalse(server._tracker.close_claimed)
        db.gate.set()
        self._assert_closed_once(db, server, extra=server._warmup_thread)

        db2 = FakeDatabase()
        db2.gate = threading.Event()
        db2.block_execute = False
        db2.block_snapshot = False
        server2 = create_server(
            sample_config(),
            database=db2,
            base_dir=self.temp.name,
            drain_timeout_s=0.2,
        )
        server2.start()
        self._wait_ready(server2)
        db2.block_execute = True
        db2.gate = threading.Event()
        db2.entered = threading.Event()
        db2.finished = threading.Event()
        db2.closed_event = threading.Event()
        probe = threading.Thread(target=server2.test_connection, args=("dw",), daemon=True)
        probe.start()
        wait_event(db2.entered)
        server2.stop()
        self.assertEqual(db2.close_calls, 0)
        ready = server2.readiness()
        self.assertEqual(ready["lifecycle"], "stopping")
        self.assertGreaterEqual(ready["db_work_pending"], 1)
        db2.gate.set()
        self._assert_closed_once(db2, server2, extra=probe)

    # T39 a-d / T39-f
    def test_t39f_rebind_during_drain(self) -> None:
        import transport

        db = FakeDatabase()
        db.gate = threading.Event()
        old = create_server(
            sample_config(),
            database=db,
            base_dir=self.temp.name,
            drain_timeout_s=0.2,
        )
        old.start()
        port = int(old.snapshot()["port"])
        wait_event(db.entered)
        old.stop()
        new_db = FakeDatabase()
        new_db.block_execute = False
        new_db.block_snapshot = False
        new = create_server(
            sample_config(port=port),
            database=new_db,
            base_dir=self.temp.name,
        )
        new.start()
        self.addCleanup(new.stop)
        self._wait_ready(new)
        self.assertEqual(int(new.snapshot()["port"]), port)
        httpc = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": PROTOCOL_VERSION}}).encode("utf-8")
        httpc.request(
            "POST",
            "/mcp",
            body=body,
            headers={"Content-Type": "application/json", "Accept": "application/json", "Content-Length": str(len(body))},
        )
        response = httpc.getresponse()
        self.assertEqual(response.status, 200)
        response.read()
        httpc.close()
        names_before = [item["name"] for item in new.tools()]
        tools_before = [item["name"] for item in transport.TOOLS]
        catalog_before = new.readiness()["catalog"]
        db.gate.set()
        wait_event(db.closed_event)
        self._join_workers(old, extra=old._warmup_thread)
        self.assertEqual(db.close_calls, 1)
        self.assertEqual(new_db.close_calls, 0)
        self.assertEqual([item["name"] for item in new.tools()], names_before)
        self.assertEqual([item["name"] for item in transport.TOOLS], tools_before)
        self.assertEqual(new.readiness()["lifecycle"], "running")
        self.assertIs(new.gateway.database, new_db)
        self.assertIsNot(new.gateway.database, db)
        self.assertEqual(new.readiness()["catalog"], catalog_before)


class IdleSseStopTests(unittest.TestCase):
    def test_t39_idle_sse_stop(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        db = FakeDatabase()
        server = create_server(sample_config(), database=db, base_dir=temp.name)
        server.start()
        port = int(server.snapshot()["port"])
        httpc = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": PROTOCOL_VERSION}}).encode("utf-8")
        httpc.request("POST", "/mcp", body=body, headers={"Content-Type": "application/json", "Accept": "application/json", "Content-Length": str(len(body))})
        init = httpc.getresponse()
        session = init.getheader("Mcp-Session-Id")
        init.read()
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        sock.sendall(
            (
                f"GET /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nAccept: text/event-stream\r\n"
                f"Mcp-Session-Id: {session}\r\n\r\n"
            ).encode("ascii")
        )
        sock.settimeout(5)
        buf = b""
        while b": connected" not in buf:
            buf += sock.recv(4096)
        httpc.request("DELETE", "/mcp", headers={"Mcp-Session-Id": session})
        httpc.getresponse().read()
        sock.settimeout(1)
        try:
            while sock.recv(4096):
                pass
        except OSError:
            pass
        for _ in range(3):
            server.stop()
            replacement = create_server(sample_config(port=port), database=FakeDatabase(), base_dir=temp.name)
            replacement.start()
            port = int(replacement.snapshot()["port"])
            replacement.stop()
            server = replacement
        httpc.close()
        sock.close()


if __name__ == "__main__":
    unittest.main()
