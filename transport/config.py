"""Load and validate proxy config. Importing this module never opens a DB."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8811
DEFAULT_POOL_MAX = 5
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
SUPPORTED_DRIVERS = ("oracle",)
DRIVER_PROBE_SQL = {"oracle": "SELECT 1 FROM DUAL"}


class ConfigError(ValueError):
    """Invalid proxy configuration. Messages never include secret values."""


@dataclass(frozen=True)
class DatabaseBinding:
    database: Any
    probe_sql: str
    driver: str


def load_config(path: str | Path) -> dict[str, Any]:
    raw = Path(path).read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError("config is not valid UTF-8") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError("config is not valid JSON") from exc
    return validate_config(data)


def validate_config(data: Any) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise ConfigError("config must be an object")
    config = copy.deepcopy(dict(data))
    proxy = config.get("proxy")
    if proxy is None:
        proxy = {}
        config["proxy"] = proxy
    if not isinstance(proxy, dict):
        raise ConfigError("proxy must be an object")
    host = proxy.get("host", DEFAULT_HOST)
    if host != DEFAULT_HOST:
        raise ConfigError("host must be the literal 127.0.0.1")
    proxy["host"] = DEFAULT_HOST
    port = proxy.get("port", DEFAULT_PORT)
    if isinstance(port, bool) or not isinstance(port, int) or port < 0 or port > 65535:
        raise ConfigError("port must be an integer 0-65535")
    proxy["port"] = port
    log_level = proxy.get("log_level", "INFO")
    if not isinstance(log_level, str) or log_level not in LOG_LEVELS:
        raise ConfigError("log_level must be DEBUG, INFO, WARNING, or ERROR")
    proxy["log_level"] = log_level
    if "query_timeout" in proxy:
        timeout = proxy["query_timeout"]
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or timeout <= 0
        ):
            raise ConfigError("proxy.query_timeout must be a positive number")
    connections = config.get("connections")
    if not isinstance(connections, list) or not connections:
        raise ConfigError("connections must be a non-empty array")
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(connections):
        normalized.append(_validate_connection(item, index, seen))
    config["connections"] = normalized
    if "common_code" in config:
        config["common_code"] = _validate_common_code(config["common_code"], seen)
    if "tools" in config:
        config["tools"] = _validate_tools(config["tools"])
    if "require_process" in config:
        config["require_process"] = _validate_require_process(config["require_process"])
    return config


# 운영자가 사이트별로 조정하는 도구 정책값. 전부 선택이며 생략 시 아래 기본값을 쓴다.
TOOL_DEFAULTS: dict[str, Any] = {
    # cross_query 동시 실행 스레드 수. pool_max 가 작은 커넥션이 섞이면 낮춰야 한다.
    "cross_query_workers": 4,
    # table_profile: 통계 수집 후 변경 비율이 이 값을 넘으면 stale 로 표시.
    "stale_change_ratio": 0.10,
    # explain: 계획 추정 행수가 이 값을 넘으면 HIGH_ROWS 경고. 대형 DW 는 올려야 한다.
    "high_rows_threshold": 1_000_000,
}


def _validate_tools(raw: Any) -> dict[str, Any]:
    """Optional per-site tool tuning. Unknown keys and bad ranges fail closed."""
    if not isinstance(raw, Mapping):
        raise ConfigError("tools must be an object")
    entry = dict(raw)
    unknown = set(entry) - set(TOOL_DEFAULTS)
    if unknown:
        raise ConfigError(f"tools has unknown keys: {sorted(unknown)}")
    workers = entry.get("cross_query_workers")
    if workers is not None:
        if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 32:
            raise ConfigError("tools.cross_query_workers must be an integer 1-32")
    ratio = entry.get("stale_change_ratio")
    if ratio is not None:
        if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0 < ratio <= 1:
            raise ConfigError("tools.stale_change_ratio must be a number in (0, 1]")
        entry["stale_change_ratio"] = float(ratio)
    threshold = entry.get("high_rows_threshold")
    if threshold is not None:
        if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 1:
            raise ConfigError("tools.high_rows_threshold must be a positive integer")
    return entry


def _validate_require_process(raw: Any) -> list[str]:
    """지정된 프로그램이 떠 있을 때만 실행된다. 키가 없으면 process_guard.DEFAULT_REQUIRE_PROCESS(기본 빈 목록)가 적용되고, 빈 배열이면 게이트 비활성."""
    items = [raw] if isinstance(raw, str) else raw
    if not isinstance(items, list):
        raise ConfigError("require_process must be a string or an array of strings")
    normalized: list[str] = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError("require_process entries must be non-empty strings")
        normalized.append(item.strip())
    return normalized


def _validate_common_code(raw: Any, connection_names: set[str]) -> dict[str, Any]:
    """공통코드 테이블의 위치. 프록시 전체가 하나를 공유한다.

    이름 규칙으로는 찾을 수 없어(코멘트·명명 관례가 환경마다 다름) 위치만 설정으로
    받는다. 컬럼 역할은 에이전트가 describe_table 코멘트에서 읽으므로 설정하지 않는다.
    """
    if not isinstance(raw, Mapping):
        raise ConfigError("common_code must be an object")
    entry = dict(raw)
    unknown = set(entry) - {"table", "connection"}
    if unknown:
        raise ConfigError(f"common_code has unknown keys: {sorted(unknown)}")
    table = entry.get("table")
    if not isinstance(table, str) or not table.strip():
        raise ConfigError("common_code.table is missing")
    entry["table"] = table.strip().upper()
    if entry["table"].count(".") != 1:
        raise ConfigError("common_code.table must be OWNER.TABLE")
    if "connection" in entry:
        value = entry["connection"]
        if not isinstance(value, str) or not value.strip():
            raise ConfigError("common_code.connection must be a non-empty string")
        entry["connection"] = value.strip()
        if entry["connection"] not in connection_names:
            raise ConfigError(
                f"common_code.connection is not a registered connection: {entry['connection']}"
            )
    return entry


def _validate_connection(item: Any, index: int, seen: set[str]) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise ConfigError(f"connections[{index}] must be an object")
    entry = dict(item)
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f"connections[{index}].name is missing")
    name = name.strip()
    if name in seen:
        raise ConfigError(f"duplicate connection name: {name}")
    seen.add(name)
    entry["name"] = name
    dsn = entry.get("dsn")
    if not isinstance(dsn, str) or not dsn.strip():
        raise ConfigError(f"connection {name!r} dsn is missing")
    entry["dsn"] = dsn.strip()
    user = entry.get("user")
    if not isinstance(user, str) or not user.strip():
        raise ConfigError(f"connection {name!r} user is missing")
    entry["user"] = user.strip()
    pool_max = entry.get("pool_max", DEFAULT_POOL_MAX)
    if (
        not isinstance(pool_max, int)
        or isinstance(pool_max, bool)
        or pool_max < 1
    ):
        raise ConfigError(f"connection {name!r} pool_max must be an integer >= 1")
    entry["pool_max"] = pool_max
    driver = entry.get("driver", "oracle")
    if not isinstance(driver, str) or driver not in SUPPORTED_DRIVERS:
        raise ConfigError(f"connection {name!r} driver is not supported")
    entry["driver"] = driver
    password = entry.get("password")
    password_env = entry.get("password_env")
    has_password = False
    if password is not None:
        if not isinstance(password, str):
            raise ConfigError(f"connection {name!r} password must be a string")
        if password == "":
            raise ConfigError(f"connection {name!r} password is empty")
        has_password = True
    has_env = False
    if password_env is not None:
        if not isinstance(password_env, str):
            raise ConfigError(f"connection {name!r} password_env must be a string")
        stripped_env = password_env.strip()
        if stripped_env:
            entry["password_env"] = stripped_env
            has_env = True
        else:
            entry.pop("password_env", None)
    if not has_password and not has_env:
        raise ConfigError(
            f"connection {name!r} requires password or password_env"
        )
    return entry


def _driver_from_config(config: Mapping[str, Any]) -> str:
    connections = config.get("connections") or []
    if connections and isinstance(connections[0], Mapping):
        driver = connections[0].get("driver", "oracle")
        if isinstance(driver, str) and driver in DRIVER_PROBE_SQL:
            return driver
    return "oracle"


def scrub_secrets(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return a deepcopy with password/password_env keys removed."""
    data = copy.deepcopy(dict(config))
    connections = data.get("connections")
    if isinstance(connections, list):
        cleaned: list[Any] = []
        for item in connections:
            if isinstance(item, dict):
                entry = dict(item)
                entry.pop("password", None)
                entry.pop("password_env", None)
                cleaned.append(entry)
            else:
                cleaned.append(item)
        data["connections"] = cleaned
    return data


def collect_secrets(config: Mapping[str, Any]) -> tuple[str, ...]:
    secrets: list[str] = []
    connections = config.get("connections")
    if isinstance(connections, list):
        for item in connections:
            if isinstance(item, Mapping):
                password = item.get("password")
                if isinstance(password, str) and password:
                    secrets.append(password)
    return tuple(secrets)


def build_database(
    config: Mapping[str, Any],
    *,
    database: Any = None,
) -> DatabaseBinding:
    driver = _driver_from_config(config)
    probe_sql = DRIVER_PROBE_SQL[driver]
    if database is not None:
        return DatabaseBinding(database=database, probe_sql=probe_sql, driver=driver)
    if driver == "oracle":
        from dbtools.database import OracleDatabase

        return DatabaseBinding(
            database=OracleDatabase(config),
            probe_sql=probe_sql,
            driver=driver,
        )
    raise ConfigError(f"unsupported driver: {driver}")


__all__ = [
    "ConfigError",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DRIVER_PROBE_SQL",
    "DatabaseBinding",
    "LOG_LEVELS",
    "SUPPORTED_DRIVERS",
    "build_database",
    "collect_secrets",
    "load_config",
    "scrub_secrets",
    "validate_config",
]
