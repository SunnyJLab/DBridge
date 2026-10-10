"""Typed runtime context shared by every MCP DB tool handler."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .database import DEFAULT_MAX_ROWS, DEFAULT_QUERY_TIMEOUT_S
from .errors import BadParamError

_REQUIRED_CONN_KEYS = ("name", "dsn")


def _as_path(value: Any) -> Path:
    if isinstance(value, Path):
        path = value
    elif isinstance(value, str) and value.strip():
        path = Path(value)
    else:
        raise BadParamError(
            "base_dir는 경로여야 합니다",
            hint="프록시 데이터 디렉터리의 절대 경로를 전달하세요",
        )
    return path


def _positive_timeout(value: Any, *, source: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise BadParamError(
            f"{source}은(는) 0보다 큰 숫자(초)여야 합니다",
            hint="config proxy.query_timeout을 초 단위 양수로 설정하세요",
        )
    return float(value)


def _positive_max_rows(value: Any, *, source: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise BadParamError(
            f"{source}은(는) 1 이상의 정수여야 합니다",
            hint="행 수 상한은 양의 정수여야 합니다",
        )
    return value


def _connection_entries(config: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    raw = config.get("connections")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise BadParamError(
            "config.connections는 객체 배열이어야 합니다",
            hint="list_connections로 커넥션 이름을 확인하세요",
        )
    entries: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise BadParamError(f"connections[{index}]는 객체여야 합니다")
        for key in _REQUIRED_CONN_KEYS:
            value = item.get(key)
            if not isinstance(value, str) or not value.strip():
                raise BadParamError(
                    f"connections[{index}].{key}가 없습니다",
                    hint="config.json의 name/dsn을 확인하세요",
                )
        name = item["name"]
        if name in seen:
            raise BadParamError(f"커넥션 이름이 중복됩니다: {name}")
        seen.add(name)
        entries.append(item)
    return tuple(entries)


def _timeout_from_config(config: Mapping[str, Any]) -> float | None:
    proxy = config.get("proxy")
    if not isinstance(proxy, Mapping) or "query_timeout" not in proxy:
        return None
    return _positive_timeout(proxy["query_timeout"], source="proxy.query_timeout")


@dataclass
class ToolContext:
    """Collaborators and limits for ``handler(context, **validated_args)``.

    ``database.execute`` runs SQL by named connection. ``catalog`` supplies
    accessible object metadata. ``extensions`` is optional.
    """

    config: Mapping[str, Any]
    database: Any
    catalog: Any
    base_dir: Path
    extensions: Any | None = None
    query_timeout: float | None = None
    max_rows: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.config, Mapping):
            raise BadParamError(
                "config는 객체여야 합니다",
                hint="config.json 형태의 mapping을 전달하세요",
            )
        _connection_entries(self.config)
        if self.database is None:
            raise BadParamError("database는 필수입니다")
        if self.catalog is None:
            raise BadParamError("catalog는 필수입니다")
        if not hasattr(self.database, "execute"):
            raise BadParamError("database.execute가 필요합니다")
        self.base_dir = _as_path(self.base_dir)
        if self.query_timeout is None:
            derived = _timeout_from_config(self.config)
            self.query_timeout = (
                derived if derived is not None else float(DEFAULT_QUERY_TIMEOUT_S)
            )
        else:
            self.query_timeout = _positive_timeout(
                self.query_timeout, source="query_timeout"
            )
        if self.max_rows is None:
            self.max_rows = int(DEFAULT_MAX_ROWS)
        else:
            self.max_rows = _positive_max_rows(self.max_rows, source="max_rows")

    def get_connection(self, name: str) -> Any:
        """Fail-closed lookup of a registered connection by name."""
        if not isinstance(name, str) or not name.strip():
            raise BadParamError(
                "커넥션 이름이 비어 있습니다",
                hint="list_connections로 커넥션 이름을 확인하세요",
            )
        getter = getattr(self.database, "connection_config", None)
        if callable(getter):
            return getter(name)
        names_fn = getattr(self.database, "connection_names", None)
        if callable(names_fn):
            names = names_fn()
            if name in names:
                for item in _connection_entries(self.config):
                    if item.get("name") == name:
                        return item
                return name
            raise BadParamError(
                f"등록되지 않은 커넥션: {name}",
                hint="list_connections로 커넥션 이름을 확인하세요",
            )
        for item in _connection_entries(self.config):
            if item.get("name") == name:
                return item
        raise BadParamError(
            f"등록되지 않은 커넥션: {name}",
            hint="list_connections로 커넥션 이름을 확인하세요",
        )


__all__ = ["ToolContext"]
