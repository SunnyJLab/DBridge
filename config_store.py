"""Load / validate / save config.json. File I/O lives here; core stays unaware."""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from transport.config import ConfigError, validate_config as _core_validate

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
DEFAULT_CONFIG = {
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
    "ui": {"palette": "slate"},
    "require_process": [],
}


def validate(data: Any) -> None:
    """Raise ValueError with a Korean message on schema violation; else None."""
    if not isinstance(data, Mapping):
        raise ValueError("최상위는 JSON 객체여야 합니다")
    for key in ("proxy", "connections"):
        if key not in data:
            raise ValueError(f"필수 키 누락: {key}")
    proxy = data["proxy"]
    if not isinstance(proxy, Mapping):
        raise ValueError("proxy는 객체여야 합니다")
    for key in ("host", "port", "log_level"):
        if key not in proxy:
            raise ValueError(f"필수 키 누락: proxy.{key}")
    if proxy["host"] != "127.0.0.1":
        raise ValueError("proxy.host는 127.0.0.1만 허용됩니다")
    port = proxy["port"]
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("proxy.port는 1~65535 정수여야 합니다")
    if proxy["log_level"] not in LOG_LEVELS:
        raise ValueError(f"proxy.log_level은 {', '.join(LOG_LEVELS)} 중 하나여야 합니다")
    connections = data["connections"]
    if not isinstance(connections, list):
        raise ValueError("connections는 배열이어야 합니다")
    names: set[str] = set()
    for i, conn in enumerate(connections):
        if not isinstance(conn, Mapping):
            raise ValueError(f"connections[{i}]는 객체여야 합니다")
        for key in ("name", "dsn", "user"):
            if key not in conn:
                raise ValueError(f"필수 키 누락: connections[{i}].{key}")
        if "pool_max" not in conn:
            raise ValueError(f"필수 키 누락: connections[{i}].pool_max")
        pool_max = conn["pool_max"]
        if not isinstance(pool_max, int) or isinstance(pool_max, bool) or pool_max < 1:
            raise ValueError(f"connections[{i}].pool_max는 1 이상 정수여야 합니다")
        name = conn["name"]
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"connections[{i}].name이 비어 있습니다")
        if name in names:
            raise ValueError(f"커넥션 이름 중복: {name}")
        names.add(name)
        has_password = isinstance(conn.get("password"), str) and conn.get("password") != ""
        env = conn.get("password_env")
        has_env = isinstance(env, str) and bool(env.strip())
        if not has_password and not has_env:
            raise ValueError(
                f"connections[{i}]는 password 또는 password_env가 필요합니다"
            )
    try:
        _core_validate(data)
    except ConfigError as exc:
        raise ValueError(str(exc)) from exc


def save(path: Path, data: dict, backup: bool = True) -> Path | None:
    """Validate → backup/YYYYMMDDHHMMSS/config.json → tmp → os.replace.

    Returns the backup directory, or None when no backup was taken.
    """
    validate(data)
    path = Path(path)
    backup_dir = None
    if backup and path.exists():
        backup_dir = path.parent / "backup" / datetime.now().strftime("%Y%m%d%H%M%S")
        backup_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup_dir / path.name)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)
    return backup_dir


def load(path: Path) -> dict:
    """Create DEFAULT_CONFIG on first run; otherwise read and validate."""
    path = Path(path)
    if not path.exists():
        save(path, json.loads(json.dumps(DEFAULT_CONFIG)), backup=False)
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    validate(data)
    return data


def reload_server(create_server, current_server, new_config: Mapping[str, Any]):
    """Stop the current server, then create+start a replacement.

    Headless tests pass a fake factory and count the calls. Returns the new
    server instance. UI threads must not call this; wrap in a worker.
    """
    snapshot = json.loads(json.dumps(new_config))
    if current_server is not None:
        current_server.stop()
    server = create_server(snapshot)
    server.start()
    return server
