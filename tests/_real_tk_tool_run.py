#!/usr/bin/env python3
"""실Tk로 App을 띄워 도구 화면의 '실행' 기능을 재현한다.

테스트 모듈이 아니라 tests/test_app_wiring.py가 서브프로세스로 실행하는 스크립트다.
(macOS Tk는 한 프로세스에서 root 재생성 시 segfault로 죽으므로 격리한다.)

인프로세스 스탙 서버로 ToolsPage에서 list_connections / query 를 실제로 호출하고,
결과 렌더와 잘못된 JSON 거부(서버 미호출)를 검증한다. 설명/파라미터가 같은 행에
나란히 배치되고(가로 패널), 인자/결과가 카드 전체 폭을 가져가는지(폭 비교) 레이아웃
회귀도 함께 검증한다. Oracle·외부 프로세스 불필요.
성공하면 SCENARIO_OK 를 출력하고 0으로 종료한다.
"""
from __future__ import annotations

import json
import queue
import sys
import tempfile
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ui


class StubServer:
    """ProxyServer 대역: UI가 쓰는 표면만 구현하고 호출을 기록한다."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def snapshot(self) -> dict:
        return {
            "host": "127.0.0.1", "port": 8811, "listening": True, "lifecycle": "running",
            "uptime_s": 1, "requests": 0, "sessions": 0, "tools": 2,
            "readiness": {"catalog": "ready", "lifecycle": "running"},
            "connections": [{"name": "local", "dsn": "127.0.0.1:1521/XEPDB1", "state": "ok",
                             "latency_ms": 2, "pool_used": 0, "pool_max": 5}],
        }

    def tools(self) -> list:
        return [
            {"name": "list_connections", "description": "커넥션 목록", "params": [],
             "example": {"name": "list_connections", "arguments": {}}},
            {"name": "query", "description": "SELECT 실행",
             "params": [{"name": "connection", "type": "string", "required": True},
                        {"name": "sql", "type": "string", "required": True}],
             "example": {"name": "query", "arguments": {"connection": "local", "sql": "SELECT 1 FROM DUAL"}}},
        ]

    def call_tool(self, name: str, arguments: dict) -> dict:
        self.calls.append((name, dict(arguments)))
        if name == "list_connections":
            return {"connections": [{"name": "local", "state": "ok"}]}
        if name == "query":
            return {"columns": ["N"], "rows": [[1]], "row_count": 1}
        return {"error": {"code": "BAD_PARAM", "message": f"unknown tool: {name}"}}

    def test_connection(self, name: str) -> dict:
        return {"ok": True, "latency_ms": 2, "message": ""}

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

    server = StubServer()
    try:
        app = ui.App(server, config, config_path, queue.Queue())
    except tk.TclError as exc:
        print("TK_UNAVAILABLE:", exc)
        temp.cleanup()
        return 0

    app.withdraw()
    app.geometry("1000x640")
    page = app.pages["tools"]
    seen: dict[str, str] = {}

    def select(name: str) -> None:
        for i, tool in enumerate(page.tools):
            if tool["name"] == name:
                page.listbox.selection_clear(0, "end")
                page.listbox.selection_set(i)
                page._on_select()
                return
        raise AssertionError(f"tool not found: {name}")

    def run_list() -> None:
        select("list_connections")
        seen["args_default"] = page.args.get("1.0", "end").strip()
        page._run_tool()
        seen["running"] = page.result.get("1.0", "end").strip()

    def capture_list() -> None:
        seen["list_result"] = page.result.get("1.0", "end").strip()

    def run_bad() -> None:
        select("query")
        ui.set_text(page.args, "{ not valid json")
        seen["calls_before_bad"] = str(len(server.calls))
        page._run_tool()                 # 서버 호출 없이 즉시 오류 문구

    def capture_bad() -> None:
        seen["bad_result"] = page.result.get("1.0", "end").strip()
        seen["calls_after_bad"] = str(len(server.calls))

    def check_layout() -> None:
        # 설명/파라미터는 같은 행(가로 패널)에 놓여 높이가 거의 같아야 한다.
        desc_top = page.desc.winfo_rooty()
        params_top = page.params.winfo_rooty()
        seen["desc_params_same_row"] = str(abs(desc_top - params_top) <= 4)
        # 인자/결과는 카드(body) 폭을 거의 그대로 가져가야 한다 —
        # 설명/파라미터 앞에 가로 패널이 있어도 자를엮야 한다.
        body_width = page.args.master.winfo_width()
        args_width = page.args.winfo_width()
        seen["args_width"] = str(args_width)
        seen["body_width"] = str(body_width)
        seen["args_uses_full_width"] = str(body_width > 0 and args_width >= body_width * 0.9)
        app.quit()

    app.after(400, run_list)
    app.after(1200, capture_list)
    app.after(1400, run_bad)
    app.after(2000, capture_bad)
    app.after(2200, check_layout)
    app.mainloop()
    try:
        app.destroy()
    except tk.TclError:
        pass
    temp.cleanup()

    for key in ("args_default", "running", "list_result", "bad_result",
                "calls_before_bad", "calls_after_bad", "desc_params_same_row",
                "args_width", "body_width", "args_uses_full_width"):
        print(f"{key}: {seen.get(key)!r}")

    failures = []
    if "실행 중" not in seen.get("running", ""):
        failures.append(f"실행 진행 문구가 없다: {seen.get('running')!r}")
    result = seen.get("list_result", "")
    if "실행 중" in result:
        failures.append(f"결과가 갱신되지 않았다: {result!r}")
    try:
        parsed = json.loads(result)
    except Exception:
        parsed = None
    if not isinstance(parsed, dict) or parsed.get("connections") != [{"name": "local", "state": "ok"}]:
        failures.append(f"list_connections 결과가 아니다: {result!r}")
    if "JSON" not in seen.get("bad_result", ""):
        failures.append(f"잘못된 JSON을 거부하지 않았다: {seen.get('bad_result')!r}")
    if seen.get("calls_before_bad") != seen.get("calls_after_bad"):
        failures.append("잘못된 JSON인데 서버를 호출했다")
    if seen.get("desc_params_same_row") != "True":
        failures.append(f"설명/파라미터가 같은 행에 없다: {seen.get('desc_params_same_row')!r}")
    if seen.get("args_uses_full_width") != "True":
        failures.append(
            f"인자/결과가 카드 전체 폭을 가져가지 않는다: "
            f"args={seen.get('args_width')!r} body={seen.get('body_width')!r}"
        )

    if failures:
        for item in failures:
            print("FAIL:", item)
        return 1
    print("SCENARIO_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
