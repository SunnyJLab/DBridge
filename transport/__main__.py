"""CLI entry: python -m transport --config config.json"""

from __future__ import annotations

import argparse
import logging
import sys
import threading

from .config import ConfigError, load_config
from .process_guard import (
    RequiredProcessCheckFailed,
    RequiredProcessMissing,
    missing_processes,
)
from .server import create_server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m transport")
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    try:
        missing = missing_processes(config.get("require_process"))
    except Exception as exc:
        print(str(RequiredProcessCheckFailed(exc)), file=sys.stderr)
        return 1
    if missing:
        print(str(RequiredProcessMissing(missing)), file=sys.stderr)
        return 1
    server = create_server(config)
    try:
        server.start()
    except OSError as exc:
        print(f"failed to bind: {exc}", file=sys.stderr)
        return 1
    stop = threading.Event()
    try:
        stop.wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
