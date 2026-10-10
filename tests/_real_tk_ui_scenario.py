#!/usr/bin/env python3
"""실Tk로 App 전체를 띄워 사용자 보고 시나리오를 재현하는 헬퍼.

테스트 모듈이 아니라 `tests/test_app_wiring.py`가 서브프로세스로 실행하는 스크립트다.
macOS Tk는 한 프로세스에서 root를 만들고 파기한 뒤 다시 만들면 segfault로 죽으므로,
전체 App을 띄우는 이 시나리오는 별도 프로세스로 격리한다.

검증 대상(모두 사용자가 보고한 증상):
  1) 커넥션 실패 시 상태 표가 코드만 보여준다 (`● 실패 (DPY-6001)`)
  2) 커넥션 테스트의 `테스트 중…`이 결과로 교체된다
  3) 설정 저장의 `저장 중 · 프록시 재기동…`이 결과로 교체된다
  4) 공통코드 페이지의 테이블 입력은 수동 ttk.Entry이고, 페이지 진입/커넥션
     변경만으로는 list_tables 호출이 발생하지 않는다 (자동 테이블 탐색 제거).

성공하면 `SCENARIO_OK`를 출력하고 0으로 종료한다.
"""
from __future__ import annotations

import json
import queue
import sys
import tempfile
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ui

REASON = "oracledb driver is not installed"
STATUS_CODE = "DPY-6001"


class FailingServer:
    """DB에 닿지 못하는 ProxyServer 대역. 실패 사유를 스냅샷으로 보고한다."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.tool_calls: list[str] = []

    def snapshot(self) -> dict:
        return {
            "host": "127.0.0.1", "port": 8811, "listening": True, "lifecycle": "running",
            "uptime_s": 1, "requests": 0, "sessions": 0, "tools": 20,
            "readiness": {"catalog": "error", "lifecycle": "running"},
            "connections": [{
                "name": "local", "dsn": "127.0.0.1:1521/XEPDB1", "state": "error",
                "error": STATUS_CODE, "latency_ms": None, "pool_used": 0, "pool_max": 5,
            }],
        }

    def tools(self) -> list:
        return []

    def test_connection(self, name: str) -> dict:
        self.entered.set()
        time.sleep(0.1)          # 워커 스레드에서 실제로 시간이 걸리는 경로를 흥내낸다
        raise RuntimeError(REASON)

    def call_tool(self, name: str, arguments: dict) -> dict:
        self.tool_calls.append(name)
        return {"error": {"code": "UNAVAILABLE", "message": "stub"}}

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None


def main() -> int:
    temp = tempfile.TemporaryDirectory()
    config_path = Path(temp.name) / "config.json"
    config = {
        "proxy": {"host": "127.0.0.1", "port": 8811, "log_level": "INFO"},
        "connections": [{"name": "local", "dsn": "127.0.0.1:1521/XEPDB1", "user": "readonly",
                         "password_env": "MCP_DB_PW", "pool_max": 5}],
        "ui": {"palette": "slate"},
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")

    server = FailingServer()
    try:
        # create 팩토리도 대역으로 주입해 저장→리로드가 실 Oracle에 의존하지 않게 한다.
        app = ui.App(server, config, config_path, queue.Queue(),
                            create=lambda cfg: FailingServer())
    except tk.TclError as exc:
        print("TK_UNAVAILABLE:", exc)
        temp.cleanup()
        return 0

    app.withdraw()
    status = app.pages["status"]
    config_page = app.pages["config"]
    extensions_page = app.pages["extensions"]
    seen: dict[str, str] = {}

    def start_test() -> None:
        status.tree.selection_set("local")
        status._test_selected()
        seen["test_pending"] = status.test_var.get()

    def capture_test() -> None:
        seen["test_done"] = status.test_var.get()

    def start_save() -> None:
        config_page._save()
        seen["save_pending"] = config_page.status_var.get()

    def check_extensions_page() -> None:
        # 페이지 진입(on_show 없음) + 테이블 입력은 수동 Entry이므로 list_tables를 호출하지 않는다.
        seen["extensions_is_entry"] = str(isinstance(extensions_page.cc_table_entry, ttk.Entry))
        app._show_page("extensions")
        extensions_page.cc_connection.set("local")
        extensions_page.cc_table.set("SCOTT.EMP")
        seen["tool_calls_after_extensions_nav"] = ",".join(server.tool_calls)

    def finish() -> None:
        seen["save_done"] = config_page.status_var.get()
        seen["row"] = " ".join(
            " ".join(map(str, status.tree.item(iid, "values")))
            for iid in status.tree.get_children()
        )
        app.quit()

    app.after(300, start_test)
    app.after(1000, capture_test)     # 2.5초 자동 지움 이전에 결과 문구를 붙잡는다
    app.after(1300, start_save)
    app.after(1600, check_extensions_page)
    app.after(3000, finish)
    app.mainloop()
    try:
        app.destroy()
    except tk.TclError:
        pass
    temp.cleanup()

    failures = []
    if "테스트 중" not in seen.get("test_pending", ""):
        failures.append(f"테스트 진행 문구가 없다: {seen.get('test_pending')!r}")
    if "테스트 중" in seen.get("test_done", ""):
        failures.append(f"테스트 문구가 멈췄다: {seen.get('test_done')!r}")
    if REASON not in seen.get("test_done", ""):
        failures.append(f"테스트 실패 사유가 없다: {seen.get('test_done')!r}")
    if "저장 중" not in seen.get("save_pending", ""):
        failures.append(f"저장 진행 문구가 없다: {seen.get('save_pending')!r}")
    if "저장 중" in seen.get("save_done", ""):
        failures.append(f"저장 문구가 멈췄다: {seen.get('save_done')!r}")
    if "● 실패 (DPY-6001)" not in seen.get("row", ""):
        failures.append(f"상태 행에 코드가 없다: {seen.get('row')!r}")
    if REASON in seen.get("row", "") or "oracledb" in seen.get("row", ""):
        failures.append(f"상태 행에 안내문이 남았다: {seen.get('row')!r}")
    if seen.get("extensions_is_entry") != "True":
        failures.append(f"공통코드 테이블 입력이 ttk.Entry가 아니다: {seen.get('extensions_is_entry')!r}")
    if seen.get("tool_calls_after_extensions_nav"):
        failures.append(
            f"공통코드 페이지 진입/입력만으로 도구가 호출되었다: {seen.get('tool_calls_after_extensions_nav')!r}"
        )

    for key in ("test_pending", "test_done", "save_pending", "save_done", "row",
                "extensions_is_entry", "tool_calls_after_extensions_nav"):
        print(f"{key}: {seen.get(key)!r}")
    if failures:
        for item in failures:
            print("FAIL:", item)
        return 1
    print("SCENARIO_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
