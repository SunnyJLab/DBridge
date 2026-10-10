"""Streamable HTTP MCP transport. Importing this package never opens a DB."""

from __future__ import annotations

from .config import (
    ConfigError,
    build_database,
    load_config,
    validate_config,
)
from .dispatch import TOOLS
from .server import ProxyServer, create_server

__version__ = "0.3.0"

__all__ = [
    "ConfigError",
    "ProxyServer",
    "TOOLS",
    "__version__",
    "build_database",
    "create_server",
    "load_config",
    "validate_config",
]
