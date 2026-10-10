"""Production entry: load config, attach logging, start ProxyServer + UI."""

from __future__ import annotations

import copy
import logging
import logging.handlers
import queue
import sys
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox

import config_store
import ui
from transport import create_server
from transport.process_guard import (
    RequiredProcessCheckFailed,
    RequiredProcessMissing,
    missing_processes,
)

BASE = (
    Path(sys.executable).parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
CONFIG_PATH = BASE / "config.json"
LOG_DIR = BASE / "logs"
LOG_FORMAT = "%(asctime)s  %(levelname)-7s %(name)-6s %(message)s"


class UiQueueHandler(logging.Handler):
    """LogPage._poll contract: queue items are (levelname, formatted_line)."""

    def __init__(self, log_queue: queue.Queue) -> None:
        super().__init__()
        self.log_queue = log_queue

    def prepare(self, record: logging.LogRecord) -> tuple[str, str]:
        level = "ERROR" if record.levelno >= logging.ERROR else record.levelname
        return (level, self.format(record))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.log_queue.put(self.prepare(record))
        except Exception:
            self.handleError(record)


def setup_logging(level: str, ui_queue: queue.Queue, log_dir: Path | None = None) -> None:
    target = Path(log_dir) if log_dir is not None else LOG_DIR
    target.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    file_handler = logging.handlers.RotatingFileHandler(
        target / "proxy.log",
        maxBytes=5_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
    root.addHandler(file_handler)

    ui_handler = UiQueueHandler(ui_queue)
    ui_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt="%H:%M:%S"))
    root.addHandler(ui_handler)


def build_startup(config_path: Path = CONFIG_PATH, base_dir: Path = BASE):
    """(config, initial_server, create_factory) 반환.

    driver는 config_path의 "driver"로만 결정된다 (oracle 또는 fake).
    create_factory는 App에 주입되어 설정 reload 시에도 동일 config 소스를 유지한다.
    require_process 에 지정된 프로그램이 없으면 RequiredProcessMissing,
    프로세스 열거 실패 시 RequiredProcessCheckFailed.
    """
    base_dir = Path(base_dir)
    config = config_store.load(Path(config_path))
    try:
        missing = missing_processes(config.get("require_process"))
    except Exception as exc:
        raise RequiredProcessCheckFailed(exc) from exc
    if missing:
        raise RequiredProcessMissing(missing)

    def create(cfg):
        return create_server(cfg, base_dir=base_dir)

    return config, create(copy.deepcopy(config)), create


def _parse_config_path(argv: list[str]) -> Path:
    for index, arg in enumerate(argv):
        if arg == "--config" and index + 1 < len(argv):
            return Path(argv[index + 1])
        if arg.startswith("--config="):
            return Path(arg.split("=", 1)[1])
    return CONFIG_PATH


def _startup_refusal(message: str) -> None:
    """windowed exe에는 콘솔이 없으므로 실행 거부 사유는 messagebox로 보여준다."""
    try:  # setup_logging 전이라 로그 핸들러가 없다 — 파일에 직접 한 줄 남긴다.
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_DIR / "proxy.log", "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  실행 거부: {message}\n")
    except OSError:
        pass
    ui.enable_windows_dpi_awareness()
    root = tk.Tk()
    root.withdraw()
    try:
        messagebox.showerror(ui.APP_TITLE, message)
    finally:
        root.destroy()


def main() -> None:
    config_path = _parse_config_path(sys.argv[1:])
    log_queue: queue.Queue = queue.Queue()
    try:
        config, server, create = build_startup(config_path)
    except (RequiredProcessMissing, RequiredProcessCheckFailed) as exc:
        _startup_refusal(str(exc))
        raise SystemExit(1)
    setup_logging(config["proxy"]["log_level"], log_queue)
    log = logging.getLogger("proxy")

    palette = str((config.get("ui") or {}).get("palette", "slate"))
    try:
        ui.set_palette(palette)
    except ValueError as exc:
        log.warning("%s — 기본 팔레트(slate) 사용", exc)
        ui.set_palette("slate")

    ui.enable_windows_dpi_awareness()
    app = ui.App(server, config, config_path, log_queue, create=create)
    try:
        try:
            server.start()
        except OSError as exc:
            log.error("프로시 기동 실패 (포트 %s 점유?): %s", config["proxy"]["port"], exc)
        app.mainloop()
    finally:
        app.server.stop()


if __name__ == "__main__":
    main()
