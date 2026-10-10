"""Assembly and lifecycle for all built-in and declarative DB tools."""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import tools
from .catalog import CatalogCache
from .context import ToolContext
from .database import Database, OracleDatabase
from .errors import BadParamError, ToolError, serialize_error
from .extensions import ExtensionLoader
from .registry import ToolDefinition, ToolRegistry


def builtin_definitions() -> tuple[ToolDefinition, ...]:
    """Return all 18 built-in tools in the documented phase/order contract."""
    return tools.definitions()


BUILTIN_TOOL_NAMES = tuple(item.name for item in builtin_definitions())


class ToolService:
    """Own database, catalog, extensions, and the tool registry."""

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        base_dir: str | Path,
        database: Database | None = None,
        auto_refresh_catalog: bool = True,
    ) -> None:
        if not isinstance(config, Mapping):
            raise BadParamError("config는 객체여야 합니다")
        self._lock = threading.RLock()
        self.config = json.loads(json.dumps(config))
        self.database: Database = database if database is not None else OracleDatabase(self.config)
        self.catalog = CatalogCache(self.database)
        if auto_refresh_catalog:
            self.catalog.refresh()
        self.extensions = ExtensionLoader(
            Path(base_dir),
            catalog=self.catalog,
        )
        self.extensions.reload()
        self.base_dir = self.extensions.base_dir
        self.context = ToolContext(
            config=self.config,
            database=self.database,
            catalog=self.catalog,
            base_dir=self.base_dir,
            extensions=self.extensions,
        )
        self.registry = self._build_registry()

    def _build_registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        for definition in builtin_definitions():
            registry.register(definition)
        return registry

    def list_tools(self) -> list[dict[str, Any]]:
        with self._lock:
            registry = self.registry
        return registry.list_metadata()

    def dispatch(self, name: str, arguments: Mapping[str, Any] | None) -> Any:
        with self._lock:
            registry = self.registry
        try:
            result = registry.dispatch(name, arguments, self.context)
        except ToolError as exc:
            return serialize_error(exc)
        return result

    def reload_extensions(self) -> dict[str, Any]:
        snapshot = self.extensions.reload()
        self.base_dir = self.extensions.base_dir
        self.context.base_dir = self.base_dir
        replacement = self._build_registry()
        with self._lock:
            self.registry = replacement
        return {
            "relationships": len(snapshot.relationships),
            "errors": [item.to_payload() for item in snapshot.errors],
        }

    def refresh_catalog(self, connection: str | None = None) -> dict[str, Any]:
        return self.catalog.refresh(connection)

    def close(self) -> None:
        self.database.close()


def create_tool_service(
    config: Mapping[str, Any],
    *,
    base_dir: str | Path | None = None,
    database: Database | None = None,
    auto_refresh_catalog: bool = True,
) -> ToolService:
    """Create a ready tool service; importing this module never opens a DB."""
    root = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parent.parent
    return ToolService(
        config,
        base_dir=root,
        database=database,
        auto_refresh_catalog=auto_refresh_catalog,
    )


__all__ = [
    "BUILTIN_TOOL_NAMES",
    "ToolService",
    "builtin_definitions",
    "create_tool_service",
]
