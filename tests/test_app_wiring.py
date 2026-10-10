#!/usr/bin/env python3
"""Headless Stage 2 wiring tests. Tk widgets are never created."""

from __future__ import annotations

import inspect
import json
import logging
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config_store
from app import UiQueueHandler
from ui import (
    App,
    ServerLifecycle,
    apply_status_rows,
    build_status_rows,
    build_tool_catalog,
    claude_cli_snippet,
    claude_json_snippet,
    format_uptime,
    load_tool_catalog,
    parse_tool_arguments,
    status_listening_label,
)


def _tk_available() -> bool:
    """디스플레이가 없는 머싱(CI 등)에서는 tk.Tk() 생성이 실패한다; 그럴 때 실Tk 테스트는 건너넌다.

    프로넌스를 분리해서 확인한다. macOS Tk는 한 프로세스에서 root를 만들고 파기한 뒤
    다시 만들면 segfault로 죽으므로, 이 프로세스에서는 실Tk 테스트가 만들 root 하나만 허용한다.
    """
    probe = subprocess.run(
        [sys.executable, "-c", "import tkinter; tkinter.Tk()"],
        capture_output=True,
        timeout=60,
    )
    return probe.returncode == 0
class FakeTreeview:
    def __init__(self) -> None:
        self._order: list[str] = []
        self._items: dict[str, dict[str, Any]] = {}
        self._selected: list[str] = []
        self._focus = ""

    def get_children(self):
        return tuple(self._order)

    def exists(self, iid):
        return iid in self._items

    def item(self, iid, option=None, **kwargs):
        entry = self._items[iid]
        if option == "values":
            return entry["values"]
        if "values" in kwargs:
            entry["values"] = kwargs["values"]
        if "tags" in kwargs:
            entry["tags"] = kwargs["tags"]
        return {"values": entry["values"], "tags": entry["tags"]}

    def insert(self, _parent, _index, iid="", values=(), tags=()):
        self._items[iid] = {"values": values, "tags": tags}
        if iid not in self._order:
            self._order.append(iid)
        return iid

    def delete(self, iid):
        self._items.pop(iid, None)
        self._order = [x for x in self._order if x != iid]
        self._selected = [x for x in self._selected if x != iid]
        if self._focus == iid:
            self._focus = ""

    def set_children(self, _parent, *iids):
        self._order = list(iids)

    def selection(self):
        return tuple(self._selected)

    def selection_set(self, iids):
        self._selected = list(iids)

    def focus(self, iid=None):
        if iid is None:
            return self._focus
        self._focus = iid
        return self._focus


class _Var:
    def __init__(self) -> None:
        self.value = ""

    def set(self, value):
        self.value = value


class FakeLabel:
    def __init__(self) -> None:
        self.fg = None

    def config(self, **kwargs):
        if "fg" in kwargs:
            self.fg = kwargs["fg"]


def _bind_status_after(page):
    scheduled: list[tuple[int, int, Any]] = []
    cancelled: set[int] = set()
    seq = [0]

    def after(delay, fn, *args):
        seq[0] += 1
        scheduled.append((seq[0], delay, fn))
        return seq[0]

    def after_cancel(aid):
        import tkinter as tk
        if aid in cancelled:
            raise tk.TclError("invalid command name")
        cancelled.add(aid)

    page.after = after
    page.after_cancel = after_cancel
    return scheduled, cancelled


class FakeButton:
    def __init__(self) -> None:
        self.text = ""

    def config(self, **kwargs):
        if "text" in kwargs:
            self.text = kwargs["text"]


class FakeApp:
    def __init__(self, server) -> None:
        self._server = server
        self.running = None
        self.done_calls: list[tuple[Any, tuple]] = []

    @property
    def server(self):
        return self._server

    def set_running(self, text, kind):
        self.running = (text, kind)

    def enqueue_ui_done(self, callback, *args):
        self.done_calls.append((callback, args))


class FakeProxyServer:
    def __init__(self, snapshot: dict[str, Any] | None = None, tools: list | None = None) -> None:
        self._snapshot = snapshot or {
            "listening": True,
            "host": "127.0.0.1",
            "port": 8811,
            "started_at": 1_700_000_000.0,
            "request_count": 3,
            "session_count": 1,
            "connections": [
                {
                    "name": "prod-erp",
                    "dsn": "db1.example.internal:1521/ORCLPDB1",
                    "state": "ok",
                    "latency_ms": 12,
                    "pool_used": 3,
                    "pool_max": 10,
                },
                {
                    "name": "legacy-hr",
                    "dsn": "db2.example.internal:1521/ORCLPDB2",
                    "state": "error",
                    "latency_ms": None,
                    "pool_used": 0,
                    "pool_max": 5,
                },
            ],
        }
        self._tools = tools if tools is not None else [
            {
                "name": "query",
                "description": "SELECT 전용 쿼리",
                "params": [
                    {"name": "connection", "type": "string", "required": True},
                    {"name": "sql", "type": "string", "required": True},
                    {"name": "max_rows", "type": "int", "required": False, "default": 100},
                ],
                "example": {"name": "query", "arguments": {"connection": "prod-erp"}},
            }
        ]
        self.start_calls = 0
        self.stop_calls = 0
        self.test_calls: list[str] = []

    def snapshot(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._snapshot))

    def tools(self) -> list[dict[str, Any]]:
        return json.loads(json.dumps(self._tools))

    def test_connection(self, name: str) -> dict[str, Any]:
        self.test_calls.append(name)
        return {"ok": True, "latency_ms": 7, "message": ""}

    def readiness(self) -> dict[str, Any]:
        return {
            "lifecycle": "running" if self._snapshot.get("listening") else "stopped",
            "db_work_pending": 0,
            "catalog": "ready",
            "extensions": {"tools": 0, "relationships": 0, "codes": 0, "errors": []},
        }

    def start(self) -> None:
        self.start_calls += 1
        self._snapshot["listening"] = True

    def stop(self) -> None:
        self.stop_calls += 1
        self._snapshot["listening"] = False


VALID_CONFIG = {
    "proxy": {"host": "127.0.0.1", "port": 8811, "log_level": "INFO"},
    "connections": [
        {
            "name": "local",
            "dsn": "127.0.0.1:1521/XEPDB1",
            "user": "readonly",
            "password_env": "MCP_DB_PW",
            "pool_max": 5,
        }
    ],
}


class StatusRowsTests(unittest.TestCase):
    def test_build_status_rows_maps_ok_and_err(self) -> None:
        server = FakeProxyServer()
        rows = build_status_rows(server.snapshot())
        self.assertEqual(len(rows), 2)
        iid0, values0, tags0 = rows[0]
        self.assertEqual(iid0, "prod-erp")
        self.assertEqual(values0[0], "prod-erp")
        self.assertEqual(values0[1], "db1.example.internal:1521/ORCLPDB1")
        self.assertEqual(values0[2], "● 정상")
        self.assertEqual(values0[3], "12ms")
        self.assertEqual(values0[4], "3/10")
        self.assertIn("ok", tags0)
        iid1, values1, tags1 = rows[1]
        self.assertEqual(iid1, "legacy-hr")
        self.assertEqual(values1[0], "legacy-hr")
        self.assertEqual(values1[2], "● 실패")
        self.assertEqual(values1[3], "-")
        self.assertIn("err", tags1)
        self.assertIn("stripe", tags1)

    def test_status_row_iid_is_stable_for_same_name(self) -> None:
        server = FakeProxyServer()
        first = build_status_rows(server.snapshot())
        snap = server.snapshot()
        snap["connections"][0]["latency_ms"] = 99
        second = build_status_rows(snap)
        self.assertEqual([row[0] for row in first], [row[0] for row in second])
        self.assertEqual(first[0][0], "prod-erp")

    def test_build_status_rows_shows_code_not_guidance(self) -> None:
        server = FakeProxyServer()
        snap = server.snapshot()
        snap["connections"][1]["error"] = "DPY-6001"
        rows = build_status_rows(snap)
        mark = rows[1][1][2]
        self.assertEqual(mark, "● 실패 (DPY-6001)")
        self.assertNotIn("접속할 수 없습니다", mark)
        self.assertIn("err", rows[1][2])

        snap["connections"][1]["error"] = (
            "접속할 수 없습니다. 호스트·서비스명·계정을 확인하세요 (DPY-6001)"
        )
        mark = build_status_rows(snap)[1][1][2]
        self.assertEqual(mark, "● 실패 (DPY-6001)")
        self.assertNotIn("접속할 수 없습니다", mark)

        snap["connections"][1]["error"] = "접속할 수 없습니다. 호스트·서비스명·계정을 확인하세요"
        mark = build_status_rows(snap)[1][1][2]
        self.assertEqual(mark, "● 실패")

    def test_build_status_rows_unchanged_without_error(self) -> None:
        server = FakeProxyServer()
        snap = server.snapshot()
        self.assertNotIn("error", snap["connections"][1])
        rows = build_status_rows(snap)
        _iid1, values1, _tags1 = rows[1]
        self.assertEqual(values1[2], "● 실패")

    def test_build_status_rows_pending_catalog_boundaries(self) -> None:
        server = FakeProxyServer()
        snap = server.snapshot()
        snap["connections"][1]["error"] = "DPY-6001"
        snap["connections"].append({
            "name": "staging",
            "dsn": "db3.example.internal:1521/ORCLPDB3",
            "state": "error",
            "latency_ms": None,
            "pool_used": 0,
            "pool_max": 5,
        })
        rows = build_status_rows(snap, catalog="pending")
        self.assertEqual(rows[0][1][2], "● 정상")
        self.assertIn("ok", rows[0][2])
        self.assertEqual(rows[1][1][2], "● 실패 (DPY-6001)")
        self.assertIn("err", rows[1][2])
        self.assertEqual(rows[2][1][2], "● 연결 중…")
        self.assertIn("pending", rows[2][2])
        self.assertNotIn("err", rows[2][2])
        ready_rows = build_status_rows(snap, catalog="ready")
        self.assertEqual(ready_rows[2][1][2], "● 실패")
        self.assertIn("err", ready_rows[2][2])

    def test_render_snapshot_pending_catalog_without_error(self) -> None:
        from ui import StatusPage

        class PendingServer(FakeProxyServer):
            def readiness(self):
                payload = super().readiness()
                payload["catalog"] = "pending"
                return payload

        server = PendingServer()
        page = StatusPage.__new__(StatusPage)
        page.app = FakeApp(server)
        page.tree = FakeTreeview()
        page.stat_vars = {
            "state": _Var(), "uptime": _Var(), "requests": _Var(), "addr": _Var(),
        }
        page.state_label = FakeLabel()
        page.stop_btn = FakeButton()
        StatusPage._render_snapshot(page, server.snapshot())
        self.assertEqual(page.tree.item("prod-erp", "values")[2], "● 정상")
        self.assertIn("ok", page.tree.item("prod-erp")["tags"])
        self.assertEqual(page.tree.item("legacy-hr", "values")[2], "● 연결 중…")
        self.assertIn("pending", page.tree.item("legacy-hr")["tags"])
        self.assertNotIn("err", page.tree.item("legacy-hr")["tags"])

    def test_render_snapshot_keeps_selection_and_test_uses_it(self) -> None:
        from ui import StatusPage
        from unittest.mock import patch

        server = FakeProxyServer()
        page = StatusPage.__new__(StatusPage)
        page.app = FakeApp(server)
        page.tree = FakeTreeview()
        page.stat_vars = {
            "state": _Var(),
            "uptime": _Var(),
            "requests": _Var(),
            "addr": _Var(),
        }
        page.state_label = FakeLabel()
        page.stop_btn = FakeButton()
        page.test_var = _Var()
        page.test_label = FakeLabel()
        clears: list[tuple[Any, ...]] = []

        def capture_after(_delay, fn, *args):
            clears.append((fn, args))
            return None

        page.after = capture_after
        StatusPage._render_snapshot(page, server.snapshot())
        page.tree.selection_set(["legacy-hr"])
        page.tree.focus("legacy-hr")
        snap = server.snapshot()
        snap["connections"][0]["latency_ms"] = 40
        StatusPage._render_snapshot(page, snap)
        self.assertEqual(list(page.tree.selection()), ["legacy-hr"])
        self.assertEqual(page.tree.focus(), "legacy-hr")

        started: list[threading.Thread] = []
        RealThread = threading.Thread

        class JoinableThread(RealThread):
            def start(self):
                started.append(self)
                super().start()

        with patch("ui.threading.Thread", JoinableThread):
            StatusPage._test_selected(page)
        self.assertEqual(len(started), 1)
        started[0].join(2)
        self.assertFalse(started[0].is_alive())
        self.assertEqual(server.test_calls, ["legacy-hr"])
        self.assertEqual(len(page.app.done_calls), 1)
        fn, args = page.app.done_calls[0]
        self.assertIs(fn.__func__, StatusPage._on_test_done)
        self.assertEqual(args[0], "legacy-hr")
        self.assertEqual(args[1], {"ok": True, "latency_ms": 7, "message": ""})
        fn(*args)
        self.assertEqual(page.test_var.value, "legacy-hr: 연결 정상 (7ms)")

    def test_blur_tree_if_outside_keeps_selection_for_test(self) -> None:
        from ui import StatusPage
        from unittest.mock import patch

        server = FakeProxyServer()
        page = StatusPage.__new__(StatusPage)
        page.app = FakeApp(server)
        page.tree = FakeTreeview()
        page.tree.master = object()
        page.test_var = _Var()
        page.test_label = FakeLabel()
        page.after = lambda *a, **k: None
        page._focus_widget = page.tree
        page.focus_get = lambda: page._focus_widget
        page.focus_set = lambda: setattr(page, "_focus_widget", page)
        apply_status_rows(page.tree, build_status_rows(server.snapshot()))
        page.tree.selection_set(["legacy-hr"])
        StatusPage._blur_tree_if_outside(
            page, type("E", (), {"widget": object()})()
        )
        self.assertEqual(list(page.tree.selection()), ["legacy-hr"])
        self.assertIs(page._focus_widget, page)

        class JoinableThread(threading.Thread):
            def start(self):
                super().start()
                self.join(2)

        with patch("ui.threading.Thread", JoinableThread):
            StatusPage._test_selected(page)
        self.assertEqual(server.test_calls, ["legacy-hr"])
        self.assertIn("테스트 중", page.test_var.value)
        self.assertNotIn("먼저 선택", page.test_var.value)

    def test_blur_tree_if_outside_ignores_tree_click(self) -> None:
        from ui import StatusPage

        page = StatusPage.__new__(StatusPage)
        page.tree = FakeTreeview()
        page.tree.master = object()
        page._focus_widget = page.tree
        page.focus_get = lambda: page._focus_widget
        page.focus_set = lambda: setattr(page, "_focus_widget", page)
        page.tree.selection_set(["legacy-hr"])
        StatusPage._blur_tree_if_outside(
            page, type("E", (), {"widget": page.tree})()
        )
        self.assertIs(page._focus_widget, page.tree)
        self.assertEqual(list(page.tree.selection()), ["legacy-hr"])

    def test_status_listening_label(self) -> None:
        text, kind, pill = status_listening_label({"listening": True})
        self.assertEqual((text, kind, pill), ("실행 중", "ok", "● 실행 중"))
        text, kind, pill = status_listening_label(
            {"listening": False}, {"lifecycle": "stopping"}
        )
        self.assertEqual(text, "중지 중")
        self.assertEqual(pill, "● 중지 중")
        text, kind, pill = status_listening_label({"listening": False})
        self.assertEqual((text, kind, pill), ("중지됨", "err", "● 중지됨"))

    def test_status_listening_label_uses_catalog_when_listening(self) -> None:
        text, kind, pill = status_listening_label(
            {"listening": True}, {"catalog": "error"}
        )
        self.assertEqual(kind, "warn")
        self.assertNotEqual(text, "실행 중")
        self.assertTrue(pill.startswith("● "))
        text, kind, pill = status_listening_label(
            {"listening": True}, {"catalog": "pending"}
        )
        self.assertEqual(kind, "warn")
        self.assertNotEqual(text, "실행 중")
        text, kind, pill = status_listening_label(
            {"listening": True}, {"catalog": "ready"}
        )
        self.assertEqual((text, kind, pill), ("실행 중", "ok", "● 실행 중"))
        text, kind, pill = status_listening_label({"listening": True}, None)
        self.assertEqual((text, kind, pill), ("실행 중", "ok", "● 실행 중"))
        text, kind, pill = status_listening_label({"listening": False})
        self.assertEqual((text, kind, pill), ("중지됨", "err", "● 중지됨"))

    def test_format_uptime(self) -> None:
        self.assertEqual(format_uptime(None), "00:00:00")
        self.assertEqual(format_uptime(100.0, now=3761.0), "01:01:01")

    def test_render_snapshot_shows_zero_uptime_when_stopped_even_with_stale_started_at(self) -> None:
        """중지된 서버의 snapshot()은 과거 started_at을 남길 수 있다 — UI는 listening=False면
        그값을 무시하고 00:00:00을 보여야 한다(증가를 멈췄다)."""
        from ui import StatusPage

        server = FakeProxyServer()
        page = StatusPage.__new__(StatusPage)
        page.app = FakeApp(server)
        page.tree = FakeTreeview()
        page.stat_vars = {
            "state": _Var(), "uptime": _Var(), "requests": _Var(), "addr": _Var(),
        }
        page.state_label = FakeLabel()
        page.stop_btn = FakeButton()

        stale_snap = {
            "listening": False, "started_at": time.time() - 500,
            "host": "h", "port": 1, "request_count": 0, "connections": [],
        }
        StatusPage._render_snapshot(page, stale_snap)
        self.assertEqual(page.stat_vars["uptime"].value, "00:00:00")

    def test_render_snapshot_restart_counts_uptime_from_new_started_at(self) -> None:
        """재시작하면 새 started_at이 과거보다 최근이라 가동 시간이 0부터 다시 증가해야 한다."""
        from ui import StatusPage

        server = FakeProxyServer()
        page = StatusPage.__new__(StatusPage)
        page.app = FakeApp(server)
        page.tree = FakeTreeview()
        page.stat_vars = {
            "state": _Var(), "uptime": _Var(), "requests": _Var(), "addr": _Var(),
        }
        page.state_label = FakeLabel()
        page.stop_btn = FakeButton()

        stopped = {
            "listening": False, "started_at": time.time() - 500,
            "host": "h", "port": 1, "request_count": 0, "connections": [],
        }
        StatusPage._render_snapshot(page, stopped)
        self.assertEqual(page.stat_vars["uptime"].value, "00:00:00")

        restarted = {
            "listening": True, "started_at": time.time() - 5,
            "host": "h", "port": 1, "request_count": 0, "connections": [],
        }
        StatusPage._render_snapshot(page, restarted)
        self.assertEqual(page.stat_vars["uptime"].value, "00:00:05")


class UiDonePumpTests(unittest.TestCase):
    """App._pump_ui_done: 메인 쓰레드 드레인, 콜백 예외 생존성, 워커당 1회 enqueue."""

    def test_pump_drains_all_pending_on_one_pass(self) -> None:
        app = App.__new__(App)
        app._ui_done_queue = queue.Queue()
        calls: list[tuple[str, int]] = []
        rescheduled: list[tuple[Any, ...]] = []

        def fake_after(_delay, fn, *args):
            rescheduled.append((fn, args))
            return None

        app.after = fake_after
        app.enqueue_ui_done(lambda n: calls.append(("a", n)), 1)
        app.enqueue_ui_done(lambda n: calls.append(("b", n)), 2)
        app.enqueue_ui_done(lambda n: calls.append(("c", n)), 3)
        App._pump_ui_done(app)
        self.assertEqual(calls, [("a", 1), ("b", 2), ("c", 3)])
        self.assertEqual(app._ui_done_queue.qsize(), 0)
        self.assertEqual(len(rescheduled), 1)
        self.assertEqual(rescheduled[0][0].__func__, App._pump_ui_done)

    def test_pump_survives_callback_exception_and_keeps_pumping(self) -> None:
        app = App.__new__(App)
        app._ui_done_queue = queue.Queue()
        calls: list[str] = []
        reschedule_count = {"n": 0}

        def fake_after(_delay, fn, *args):
            reschedule_count["n"] += 1
            return None

        app.after = fake_after

        def boom():
            raise RuntimeError("bad completion callback")

        app.enqueue_ui_done(boom)
        app.enqueue_ui_done(lambda: calls.append("survived"))
        with self.assertLogs("ui", level="ERROR"):
            App._pump_ui_done(app)
        self.assertEqual(calls, ["survived"])
        self.assertEqual(reschedule_count["n"], 1)

        # 예외를 낸 다음 회차도 계속 펌핑된다 (첫 실행이 죽지 않았음을 재확인).
        app.enqueue_ui_done(lambda: calls.append("still-alive"))
        App._pump_ui_done(app)
        self.assertEqual(calls, ["survived", "still-alive"])
        self.assertEqual(reschedule_count["n"], 2)

    def test_no_worker_thread_calls_after_directly(self) -> None:
        """_test_selected/_toggle_server/save_config 워커 본문에 self.after(0, ...) 호출이 남아있으면 실패."""
        from ui import StatusPage

        for source in (
            inspect.getsource(StatusPage._test_selected),
            inspect.getsource(StatusPage._toggle_server),
            inspect.getsource(App.save_config),
        ):
            self.assertNotIn("self.after(0,", source)
            self.assertNotIn("app.after(0,", source)

    def test_test_selected_enqueues_exactly_once_on_success(self) -> None:
        from ui import StatusPage
        from unittest.mock import patch

        server = FakeProxyServer()
        page = StatusPage.__new__(StatusPage)
        page.app = FakeApp(server)
        page.tree = FakeTreeview()
        page.test_var = _Var()
        page.test_label = FakeLabel()
        page.after = lambda *a, **k: None
        apply_status_rows(page.tree, build_status_rows(server.snapshot()))
        page.tree.selection_set(["legacy-hr"])

        class JoinableThread(threading.Thread):
            def start(self):
                super().start()
                self.join(2)

        with patch("ui.threading.Thread", JoinableThread):
            StatusPage._test_selected(page)
        self.assertEqual(len(page.app.done_calls), 1)

    def test_test_selected_enqueues_exactly_once_on_failure(self) -> None:
        from ui import StatusPage
        from unittest.mock import patch

        class BoomServer(FakeProxyServer):
            def test_connection(self, name: str):
                raise RuntimeError("AdapterError: driver missing")

        server = BoomServer()
        page = StatusPage.__new__(StatusPage)
        page.app = FakeApp(server)
        page.tree = FakeTreeview()
        page.test_var = _Var()
        page.test_label = FakeLabel()
        page.after = lambda *a, **k: None
        apply_status_rows(page.tree, build_status_rows(server.snapshot()))
        page.tree.selection_set(["legacy-hr"])

        class JoinableThread(threading.Thread):
            def start(self):
                super().start()
                self.join(2)

        with patch("ui.threading.Thread", JoinableThread):
            StatusPage._test_selected(page)
        self.assertEqual(len(page.app.done_calls), 1)
        fn, args = page.app.done_calls[0]
        self.assertIs(fn.__func__, StatusPage._on_test_done)
        self.assertFalse(args[1]["ok"])


    def test_on_test_done_failure_auto_clears_slower_than_success(self) -> None:
        from ui import StatusPage, P

        page = StatusPage.__new__(StatusPage)
        page.test_var = _Var()
        page.test_label = FakeLabel()
        scheduled, cancelled = _bind_status_after(page)

        StatusPage._on_test_done(
            page, "legacy-hr", {"ok": False, "message": "oracledb driver is not installed"}
        )
        self.assertEqual(page.test_label.fg, P["err"])
        self.assertEqual(len(scheduled), 1)
        fail_delay = scheduled[0][1]

        StatusPage._on_test_done(
            page, "legacy-hr", {"ok": True, "latency_ms": 7, "message": ""}
        )
        self.assertEqual(page.test_label.fg, P["ok"])
        self.assertEqual(len(scheduled), 2)
        ok_delay = scheduled[1][1]
        self.assertEqual(ok_delay, 2500)
        self.assertGreater(fail_delay, ok_delay)
        self.assertEqual(fail_delay, 8000)
        self.assertIn(scheduled[0][0], cancelled)

    def test_status_clear_cancel_does_not_wipe_newer_message(self) -> None:
        from ui import StatusPage, P

        page = StatusPage.__new__(StatusPage)
        page.test_var = _Var()
        page.test_label = FakeLabel()
        scheduled, cancelled = _bind_status_after(page)

        StatusPage._on_test_done(
            page, "legacy-hr", {"ok": False, "message": "접속할 수 없습니다"}
        )
        fail_id = scheduled[0][0]
        StatusPage._set_status(page, "legacy-hr: 테스트 중…", "warn")
        self.assertIn(fail_id, cancelled)
        self.assertEqual(len(scheduled), 1)
        for aid, _delay, fn in scheduled:
            if aid not in cancelled:
                fn()
        self.assertIn("테스트 중", page.test_var.value)
        self.assertEqual(page.test_label.fg, P["warn"])

        StatusPage._on_test_done(
            page, "legacy-hr", {"ok": True, "latency_ms": 7, "message": ""}
        )
        self.assertEqual(page.test_var.value, "legacy-hr: 연결 정상 (7ms)")
        self.assertIn(fail_id, cancelled)

    def test_on_toggle_done_failure_schedules_auto_clear(self) -> None:
        from ui import StatusPage, P

        page = StatusPage.__new__(StatusPage)
        page.test_var = _Var()
        page.test_label = FakeLabel()
        page.stop_btn = FakeButton()
        page._busy = True
        page.refresh = lambda: None
        scheduled, _cancelled = _bind_status_after(page)

        StatusPage._on_toggle_done(page, False, "stop failed")
        self.assertFalse(page._busy)
        self.assertEqual(page.test_label.fg, P["err"])
        self.assertIn("stop failed", page.test_var.value)
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(scheduled[0][1], 8000)

    def test_toggle_server_enqueues_exactly_once_on_success_and_failure(self) -> None:
        from ui import StatusPage
        from unittest.mock import patch

        class JoinableThread(threading.Thread):
            def start(self):
                super().start()
                self.join(2)

        # 성공 경로
        server = FakeProxyServer()
        page = StatusPage.__new__(StatusPage)
        page.app = FakeApp(server)
        page.app.config_data = VALID_CONFIG
        page.app.lifecycle = ServerLifecycle(server, lambda cfg: FakeProxyServer())
        page.stop_btn = FakeButton()
        page._busy = False
        with patch("ui.threading.Thread", JoinableThread):
            StatusPage._toggle_server(page)
        self.assertEqual(len(page.app.done_calls), 1)
        fn, args = page.app.done_calls[0]
        self.assertIs(fn.__func__, StatusPage._on_toggle_done)
        self.assertTrue(args[0])

        # 실패 경로: lifecycle.stop이 예외를 던진다.
        class BoomLifecycle(ServerLifecycle):
            def stop(self, seq=None):
                raise RuntimeError("stop failed")

        server2 = FakeProxyServer()
        page2 = StatusPage.__new__(StatusPage)
        page2.app = FakeApp(server2)
        page2.app.config_data = VALID_CONFIG
        page2.app.lifecycle = BoomLifecycle(server2, lambda cfg: FakeProxyServer())
        page2.stop_btn = FakeButton()
        page2._busy = False
        with patch("ui.threading.Thread", JoinableThread):
            StatusPage._toggle_server(page2)
        self.assertEqual(len(page2.app.done_calls), 1)
        fn2, args2 = page2.app.done_calls[0]
        self.assertIs(fn2.__func__, StatusPage._on_toggle_done)
        self.assertFalse(args2[0])

    def test_save_config_enqueues_exactly_once_on_success_and_failure(self) -> None:
        from unittest.mock import patch

        class JoinableThread(threading.Thread):
            def start(self):
                super().start()
                self.join(2)

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            server = FakeProxyServer()
            app = App.__new__(App)
            app.config_path = config_path
            app.config_data = json.loads(json.dumps(VALID_CONFIG))
            app.lifecycle = ServerLifecycle(server, lambda cfg: FakeProxyServer())
            app._ui_done_queue = queue.Queue()
            done: list[tuple[bool, str]] = []

            def on_done(ok, message):
                done.append((ok, message))

            with patch("ui.threading.Thread", JoinableThread):
                App.save_config(app, json.loads(json.dumps(VALID_CONFIG)), on_done)
            self.assertEqual(app._ui_done_queue.qsize(), 1)
            fn, args = app._ui_done_queue.get_nowait()
            self.assertIs(fn, on_done)
            fn(*args)
            self.assertEqual(len(done), 1)
            self.assertTrue(done[0][0])

            class BoomLifecycle(ServerLifecycle):
                def reload(self, config, seq=None):
                    raise RuntimeError("reload failed")

            server2 = FakeProxyServer()
            app2 = App.__new__(App)
            app2.config_path = config_path
            app2.config_data = json.loads(json.dumps(VALID_CONFIG))
            app2.lifecycle = BoomLifecycle(server2, lambda cfg: FakeProxyServer())
            app2._ui_done_queue = queue.Queue()
            done2: list[tuple[bool, str]] = []

            def on_done2(ok, message):
                done2.append((ok, message))

            with patch("ui.threading.Thread", JoinableThread):
                App.save_config(app2, json.loads(json.dumps(VALID_CONFIG)), on_done2)
            self.assertEqual(app2._ui_done_queue.qsize(), 1)
            fn2, args2 = app2._ui_done_queue.get_nowait()
            self.assertIs(fn2, on_done2)
            fn2(*args2)
            self.assertEqual(len(done2), 1)
            self.assertFalse(done2[0][0])


@unittest.skipUnless(_tk_available(), "디스플레이가 없어 실Tk를 생성할 수 없다")
class RealTkPumpTests(unittest.TestCase):
    """가짜 after()로는 잡아낼 수 없는 실Tk 쓰레드 드랍 버그를 mainloop로 재현한다."""

    def test_worker_completion_reaches_label_through_real_mainloop(self) -> None:
        """실Tk mainloop 하에서 App._pump_ui_done이 워커 완료을 실제로 라벨까지 전달하는지 검증한다.

        구방식(self.after(0, ...))은 워커 스레드에서 호출된 after가 실Tk에서 유실되어 이 테스트를 통과하지 못했다;
        가짜 after()를 기록만 하는 헤드리스 테스트는 이 드랍을 잡아낼 수 없었다.
        """
        import tkinter as tk

        root = tk.Tk()
        try:
            root.withdraw()
            app = App.__new__(App)
            app.after = root.after
            app._ui_done_queue = queue.Queue()
            label_var = tk.StringVar(value="테스트 중…")
            done_event = threading.Event()

            def on_done(text):
                label_var.set(text)
                done_event.set()

            def worker():
                # 실제 코드와 동일한 패턴: 워커가 App.enqueue_ui_done으로 넣고 메인 스레드 펌프가 소비.
                App.enqueue_ui_done(app, on_done, "legacy-hr: SELECT 1 → OK")

            App._pump_ui_done(app)
            threading.Thread(target=worker, daemon=True).start()

            deadline = time.monotonic() + 5
            while not done_event.is_set() and time.monotonic() < deadline:
                root.update()
                time.sleep(0.01)

            self.assertTrue(done_event.is_set(), "완료 콜백이 지정된 시간 내에 mainloop를 통해 도달하지 않았다")
            self.assertEqual(label_var.get(), "legacy-hr: SELECT 1 → OK")
        finally:
            root.destroy()


@unittest.skipUnless(_tk_available(), "디스플레이가 없어 실Tk를 생성할 수 없다")
class RealTkUiScenarioTests(unittest.TestCase):
    """App 전잴를 실Tk로 띄우는 사용자 보고 시나리오.

    RealTkPumpTests는 전달 메커니즘만 보기 때문에, 실제 StatusPage/ConfigPage가
    연결된 상태에서 라벨이 해소되는지는 이 시나리오가 담담한다.
    macOS Tk는 한 프로세스에서 root 재생성 시 segfault로 죽으므로 별도 프로세스로 실행한다.
    """

    def test_transient_labels_resolve_and_row_shows_reason(self) -> None:
        script = Path(__file__).resolve().parent / "_real_tk_ui_scenario.py"
        completed = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            timeout=180,
        )
        output = completed.stdout + completed.stderr
        if "TK_UNAVAILABLE" in output:
            self.skipTest("실Tk를 생성할 수 없는 환경이다")
        self.assertEqual(completed.returncode, 0, output)
        self.assertIn("SCENARIO_OK", output)


class ParseToolArgumentsTests(unittest.TestCase):
    def test_blank_means_no_arguments(self) -> None:
        self.assertEqual(parse_tool_arguments(""), {})
        self.assertEqual(parse_tool_arguments("   \n"), {})

    def test_object_is_parsed(self) -> None:
        self.assertEqual(parse_tool_arguments('{"connection": "dw"}'), {"connection": "dw"})

    def test_non_object_is_rejected(self) -> None:
        for raw in ("[1, 2]", '"x"', "5", "true"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_tool_arguments(raw)

    def test_malformed_json_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_tool_arguments("{ not json")


@unittest.skipUnless(_tk_available(), "디스플레이가 없어 실Tk를 생성할 수 없다")
class RealTkToolRunTests(unittest.TestCase):
    """도구 화면의 '실행' 기능을 App 전체 + fake DB로 검증 (별도 프로세스)."""

    def test_tool_runs_and_renders_result(self) -> None:
        script = Path(__file__).resolve().parent / "_real_tk_tool_run.py"
        completed = subprocess.run(
            [sys.executable, str(script)], capture_output=True, text=True, timeout=180,
        )
        output = completed.stdout + completed.stderr
        if "TK_UNAVAILABLE" in output:
            self.skipTest("실Tk를 생성할 수 없는 환경이다")
        self.assertEqual(completed.returncode, 0, output)
        self.assertIn("SCENARIO_OK", output)


class ToolCatalogTests(unittest.TestCase):
    def test_build_tool_catalog_from_server_tools(self) -> None:
        server = FakeProxyServer()
        catalog = build_tool_catalog(server.tools())
        self.assertEqual(catalog[0]["name"], "query")
        self.assertEqual(catalog[0]["desc"], "SELECT 전용 쿼리")
        params = catalog[0]["params"]
        self.assertEqual(params[0], ("connection", "string", "필수"))
        self.assertEqual(params[1], ("sql", "string", "필수"))
        self.assertEqual(params[2], ("max_rows", "int", "기본 100"))
        self.assertEqual(catalog[0]["example"]["name"], "query")

    def test_load_tool_catalog_empty_success_vs_exception(self) -> None:
        empty = FakeProxyServer(tools=[])
        catalog, error = load_tool_catalog(empty)
        self.assertEqual(catalog, [])
        self.assertIsNone(error)

        class Boom:
            def tools(self):
                raise RuntimeError("boom")

        with self.assertLogs("ui", level="ERROR"):
            catalog, error = load_tool_catalog(Boom())
        self.assertEqual(catalog, [])
        self.assertEqual(error, "도구 목록 조회 실패")

    def test_claude_snippets_use_config_host_port(self) -> None:
        cfg = {"proxy": {"host": "127.0.0.1", "port": 8812}}
        self.assertIn("http://127.0.0.1:8812/mcp", claude_cli_snippet(cfg))
        self.assertIn('"url": "http://127.0.0.1:8812/mcp"', claude_json_snippet(cfg))


class UiQueueHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.q: queue.Queue = queue.Queue()
        self.handler = UiQueueHandler(self.q)
        self.handler.setFormatter(
            logging.Formatter("%(asctime)s  %(levelname)-7s %(name)-6s %(message)s",
                              datefmt="%H:%M:%S")
        )
        self.root = logging.getLogger()
        self.previous = list(self.root.handlers)
        self.previous_level = self.root.level
        for handler in self.previous:
            self.root.removeHandler(handler)
        self.root.addHandler(self.handler)
        self.root.setLevel(logging.DEBUG)

    def tearDown(self) -> None:
        self.root.removeHandler(self.handler)
        for handler in self.previous:
            self.root.addHandler(handler)
        self.root.setLevel(self.previous_level)

    def test_prepare_info_tuple(self) -> None:
        logging.getLogger("tool").info("hello")
        item = self.q.get_nowait()
        self.assertIsInstance(item, tuple)
        self.assertEqual(len(item), 2)
        self.assertEqual(item[0], "INFO")
        self.assertIsInstance(item[1], str)
        self.assertIn("INFO", item[1])
        self.assertIn("tool", item[1])
        self.assertIn("hello", item[1])

    def test_prepare_maps_critical_to_error(self) -> None:
        record = logging.LogRecord(
            "x", logging.CRITICAL, __file__, 1, "boom", None, None
        )
        level, line = self.handler.prepare(record)
        self.assertEqual(level, "ERROR")
        self.assertIn("boom", line)
        logging.getLogger("x").critical("boom")
        queued = self.q.get_nowait()
        self.assertEqual(queued[0], "ERROR")


class ConfigStoreTests(unittest.TestCase):
    def test_validate_default_and_rejects(self) -> None:
        config_store.validate(json.loads(json.dumps(VALID_CONFIG)))
        with self.assertRaises(ValueError):
            config_store.validate({**VALID_CONFIG, "proxy": {**VALID_CONFIG["proxy"], "port": "8811"}})
        with self.assertRaises(ValueError):
            config_store.validate({**VALID_CONFIG, "proxy": {**VALID_CONFIG["proxy"], "port": 70000}})
        with self.assertRaises(ValueError):
            config_store.validate({**VALID_CONFIG, "proxy": {**VALID_CONFIG["proxy"], "host": "0.0.0.0"}})
        with self.assertRaises(ValueError):
            config_store.validate({**VALID_CONFIG, "proxy": {**VALID_CONFIG["proxy"], "log_level": "TRACE"}})
        dup = json.loads(json.dumps(VALID_CONFIG))
        dup["connections"].append(dict(dup["connections"][0]))
        with self.assertRaises(ValueError):
            config_store.validate(dup)
        bad_pool = json.loads(json.dumps(VALID_CONFIG))
        bad_pool["connections"][0]["pool_max"] = 0
        with self.assertRaises(ValueError):
            config_store.validate(bad_pool)

    def test_save_backup_atomic_and_invalid_leaves_original(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            original = json.loads(json.dumps(VALID_CONFIG))
            config_store.save(path, original, backup=False)
            updated = json.loads(json.dumps(VALID_CONFIG))
            updated["proxy"]["port"] = 8812
            backup_dir = config_store.save(path, updated)
            self.assertIsNotNone(backup_dir)
            self.assertTrue(backup_dir.name.isdigit())
            self.assertEqual(len(backup_dir.name), 14)
            previous = json.loads((backup_dir / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(previous["proxy"]["port"], 8811)
            current = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(current["proxy"]["port"], 8812)
            self.assertFalse(path.with_name(path.name + ".tmp").exists())
            invalid = json.loads(json.dumps(updated))
            invalid["proxy"]["port"] = "abc"
            with self.assertRaises(ValueError):
                config_store.save(path, invalid)
            still = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(still["proxy"]["port"], 8812)

    def test_load_creates_default_and_accepts_bom(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            loaded = config_store.load(path)
            self.assertEqual(loaded, config_store.DEFAULT_CONFIG)
            self.assertTrue(path.exists())
            bom_path = Path(tmp) / "bom.json"
            payload = json.dumps(VALID_CONFIG, indent=2) + "\n"
            bom_path.write_bytes(b"\xef\xbb\xbf" + payload.encode("utf-8"))
            again = config_store.load(bom_path)
            self.assertEqual(again["proxy"]["port"], 8811)


class ReloadWiringTests(unittest.TestCase):
    def test_reload_server_stops_then_creates_and_starts(self) -> None:
        old = FakeProxyServer()
        created: list[FakeProxyServer] = []

        def factory(config):
            server = FakeProxyServer()
            server.config = config
            created.append(server)
            return server

        new_config = json.loads(json.dumps(VALID_CONFIG))
        new_config["proxy"]["port"] = 8812
        replacement = config_store.reload_server(factory, old, new_config)
        self.assertEqual(old.stop_calls, 1)
        self.assertEqual(len(created), 1)
        self.assertIs(replacement, created[0])
        self.assertEqual(replacement.start_calls, 1)
        self.assertEqual(created[0].config["proxy"]["port"], 8812)

    def test_save_config_worker_uses_reload_server(self) -> None:
        calls = {"stop": 0, "create": 0, "start": 0}
        old = FakeProxyServer()

        class NewServer(FakeProxyServer):
            def start(self) -> None:
                calls["start"] += 1
                super().start()

        def factory(config):
            calls["create"] += 1
            server = NewServer()
            server.config = config
            return server

        old_stop = old.stop

        def counted_stop():
            calls["stop"] += 1
            old_stop()

        old.stop = counted_stop
        replacement = config_store.reload_server(factory, old, VALID_CONFIG)
        self.assertEqual(calls, {"stop": 1, "create": 1, "start": 1})
        self.assertIsInstance(replacement, NewServer)
    def test_overlapping_reload_and_toggle_serialize(self) -> None:
        created: list[FakeProxyServer] = []

        def factory(config):
            server = FakeProxyServer()
            server.config = config
            created.append(server)
            return server

        old = FakeProxyServer()
        life = ServerLifecycle(old, factory)
        cfg_a = json.loads(json.dumps(VALID_CONFIG))
        cfg_a["proxy"]["port"] = 8812
        cfg_b = json.loads(json.dumps(VALID_CONFIG))
        cfg_b["proxy"]["port"] = 8813
        seq_a = life.next_seq()
        seq_b = life.next_seq()
        life.reload(cfg_b, seq=seq_b)
        discarded = life.reload(cfg_a, seq=seq_a)
        current = life.current()
        self.assertIs(current, discarded)
        self.assertEqual(current.config["proxy"]["port"], 8813)
        self.assertGreaterEqual(old.stop_calls, 1)
        self.assertTrue(current.snapshot()["listening"])
        listening = [s for s in created if s.snapshot()["listening"]]
        self.assertEqual(len(listening), 1)
        self.assertIs(listening[0], current)

    def test_stale_seq_does_not_overwrite_later_request(self) -> None:
        created: list[FakeProxyServer] = []

        def factory(config):
            server = FakeProxyServer()
            server.config = config
            created.append(server)
            return server

        old = FakeProxyServer()
        life = ServerLifecycle(old, factory)
        cfg_a = json.loads(json.dumps(VALID_CONFIG))
        cfg_a["proxy"]["port"] = 8812
        cfg_b = json.loads(json.dumps(VALID_CONFIG))
        cfg_b["proxy"]["port"] = 8813
        seq_a = life.next_seq()
        seq_b = life.next_seq()
        life.reload(cfg_b, seq=seq_b)
        life.reload(cfg_a, seq=seq_a)
        self.assertEqual(life.current().config["proxy"]["port"], 8813)

    def test_close_blocks_later_reload(self) -> None:
        created: list[FakeProxyServer] = []
        started = threading.Event()
        gate = threading.Event()

        class Gated(FakeProxyServer):
            def start(self) -> None:
                started.set()
                gate.wait(2)
                super().start()

        def factory(config):
            server = Gated()
            server.config = config
            created.append(server)
            return server

        old = FakeProxyServer()
        life = ServerLifecycle(old, factory)
        cfg = json.loads(json.dumps(VALID_CONFIG))
        seq = life.next_seq()
        errors: list[BaseException] = []

        def worker():
            try:
                life.reload(cfg, seq=seq)
            except BaseException as extra:
                errors.append(extra)

        t = threading.Thread(target=worker)
        t.start()
        self.assertTrue(started.wait(2))
        life.close()
        gate.set()
        t.join(3)
        self.assertFalse(t.is_alive())
        self.assertEqual(errors, [])
        self.assertFalse(life.current().snapshot()["listening"])
        for server in created:
            self.assertFalse(server.snapshot()["listening"])
        after = life.reload(cfg, seq=life.next_seq())
        self.assertIs(after, life.current())
        self.assertEqual(len(created), 1)
        self.assertFalse(after.snapshot()["listening"])


class ImportGuardTests(unittest.TestCase):
    def test_headless_imports_do_not_touch_tk(self) -> None:
        import ui as ui

        self.assertTrue(hasattr(ui, "build_status_rows"))
        self.assertTrue(hasattr(ui, "build_tool_catalog"))
        self.assertFalse(hasattr(ui, "MOCK_EXTERNAL_TOOLS"))
        self.assertFalse(hasattr(ui, "MOCK_CONFIG"))
        self.assertFalse(hasattr(ui, "MOCK_TOOLS"))
        self.assertFalse(hasattr(ui, "MOCK_LOG_LINES"))
        self.assertFalse(hasattr(ui, "_start_mock_log_feed"))
        source = inspect.getsource(App)
        self.assertNotIn("external_tools", source)


class PaletteTests(unittest.TestCase):
    """config.ui.palette → ui.set_palette (Tk 미기동)."""

    def setUp(self) -> None:
        import ui
        self.ui = ui
        self.addCleanup(lambda: ui.set_palette("slate"))

    def test_set_palette_applies_named_palette(self) -> None:
        self.ui.set_palette("nord")
        self.assertEqual(self.ui.P["bg"], self.ui.PALETTES["nord"]["bg"])

    def test_set_palette_clears_stale_keys(self) -> None:
        self.ui.set_palette("sun")
        self.assertIn("ttk_theme", self.ui.P)          # sun은 ttk_theme 보유
        self.ui.set_palette("slate")
        self.assertNotIn("ttk_theme", self.ui.P)       # slate로 전환 시 잔여 키 제거

    def test_set_palette_rejects_unknown(self) -> None:
        with self.assertRaises(ValueError):
            self.ui.set_palette("bogus-theme")

    def test_build_startup_default_palette_is_valid(self) -> None:
        # config.ui.palette 기본값(slate)이 유효한 팔레트인가
        self.assertIn(config_store.DEFAULT_CONFIG["ui"]["palette"], self.ui.PALETTES)


class ConfigDrivenStartupTests(unittest.TestCase):
    """app.py는 config의 driver로만 기반을 정한다 (Tk 미기동)."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def _write_config(self, driver: str = "oracle") -> Path:
        path = Path(self.temp.name) / f"config-{driver}.json"
        path.write_text(json.dumps({
            "proxy": {"host": "127.0.0.1", "port": 8811, "log_level": "INFO"},
            "require_process": [],
            "connections": [
                {"name": "dw", "dsn": "dw:1521/DW", "user": "READONLY_USER",
                 "password": "pw", "pool_max": 5, "driver": driver},
            ],
        }), encoding="utf-8")
        return path

    def test_production_entry_point_has_no_test_coupling(self) -> None:
        import app

        source = Path(app.__file__).read_text(encoding="utf-8")
        self.assertNotIn("test_http_transport", source)
        self.assertNotIn("FakeDatabase", source)
        self.assertNotIn("--fake", source)
        # 기반은 플래그가 아니라 config의 driver가 결정한다.
        with self.assertRaises(TypeError):
            app.build_startup(fake=True)

    def test_build_startup_builds_oracle_driver_from_config(self) -> None:
        import app

        path = self._write_config("oracle")
        config, server, create = app.build_startup(config_path=path)
        try:
            self.assertEqual(config["connections"][0]["driver"], "oracle")
            self.assertEqual(len(server.tools()), 18)
            self.assertEqual(type(server.gateway.database).__module__, "dbtools.database")
            self.assertTrue(callable(create))
        finally:
            server.stop()

    def test_unknown_driver_is_rejected(self) -> None:
        import app

        path = self._write_config("mysql")
        with self.assertRaises(Exception):
            app.build_startup(config_path=path)


if __name__ == "__main__":
    unittest.main()
