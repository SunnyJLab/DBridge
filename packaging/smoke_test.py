"""Smoke-test a built DBridge.exe: start it, speak MCP over HTTP, check the driver.

    python packaging/smoke_test.py dist/DBridge

Copies the build to a scratch folder (so logs/ and config.json never end up in the
release), points it at an Oracle address where nothing listens, then checks:

1. MCP initialize + tools/list answer on 127.0.0.1 with the expected tool count.
2. The catalog warm-up reached the bundled oracledb thin driver: the failure in
   logs/proxy.log is a DPY network error, not "oracledb driver is not installed".
3. cryptography (needed by thin-mode authentication) is in the bundle.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

PORT = 18811
EXPECTED_TOOLS = 18
TIMEOUT_S = 60


def _post(body: dict, session_id: str | None = None) -> tuple[dict, str | None]:
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    request = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/mcp",
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read() or b"null"), response.headers.get("Mcp-Session-Id")


def _wait_for(predicate, what: str):
    deadline = time.monotonic() + TIMEOUT_S
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            result = predicate()
            if result:
                return result
        except Exception as exc:  # server not up yet
            last_error = exc
        time.sleep(1)
    raise SystemExit(f"FAIL: timed out waiting for {what} (last error: {last_error})")


def main() -> None:
    build = Path(sys.argv[1]).resolve()
    work = Path(tempfile.mkdtemp(prefix="dbridge-smoke-")) / "DBridge"
    shutil.copytree(build, work)

    internal = work / "_internal"
    if not (internal / "cryptography").is_dir():
        raise SystemExit("FAIL: cryptography is missing from the bundle")
    if not (internal / "oracledb").is_dir():
        raise SystemExit("FAIL: oracledb is missing from the bundle")
    if not (internal / "theme_sv" / "sv.tcl").is_file():
        raise SystemExit("FAIL: theme_sv is missing from the bundle")

    config = {
        "proxy": {"host": "127.0.0.1", "port": PORT, "log_level": "INFO"},
        "connections": [
            {
                "name": "smoke",
                "dsn": "127.0.0.1:1/SMOKE",
                "user": "smoke",
                "password": "smoke-not-a-secret",
                "pool_max": 1,
            }
        ],
        "ui": {"palette": "sun"},
        "require_process": [],
    }
    config_path = work / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    process = subprocess.Popen([str(work / "DBridge.exe"), "--config", str(config_path)])
    try:
        def initialize():
            reply, sid = _post(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "smoke", "version": "0"},
                    },
                }
            )
            return (reply, sid) if sid else None

        reply, sid = _wait_for(initialize, "MCP initialize")
        server = reply["result"]["serverInfo"]
        print(f"initialize ok: {server['name']} {server['version']}")
        _post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
        listed, _ = _post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, sid)
        names = [tool["name"] for tool in listed["result"]["tools"]]
        if len(names) != EXPECTED_TOOLS:
            raise SystemExit(f"FAIL: expected {EXPECTED_TOOLS} tools, got {len(names)}: {names}")
        print(f"tools/list ok: {len(names)} tools")

        log_path = work / "logs" / "proxy.log"

        def warmup_line():
            if not log_path.exists():
                return None
            for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if "warmup failed" in line:
                    return line
            return None

        line = _wait_for(warmup_line, "catalog warm-up result in logs/proxy.log")
        print(f"warm-up: {line}")
        if "not installed" in line or "DPY-" not in line:
            raise SystemExit("FAIL: warm-up did not reach the oracledb thin driver")
        if process.poll() is not None:
            raise SystemExit(f"FAIL: DBridge.exe exited early with code {process.returncode}")
        print("PASS")
    finally:
        process.kill()
        process.wait(timeout=30)


if __name__ == "__main__":
    main()
