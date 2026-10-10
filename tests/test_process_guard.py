#!/usr/bin/env python3
"""require_process 게이트 테스트. Oracle/Tk 불필요."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app
import config_store
import transport.__main__ as headless
from transport.config import ConfigError, validate_config
from transport.process_guard import (
    DEFAULT_REQUIRE_PROCESS,
    RequiredProcessCheckFailed,
    RequiredProcessMissing,
    missing_processes,
)

VALID_CONFIG = {
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
}


def _config_with_require_process(value, tmp: str) -> Path:
    data = json.loads(json.dumps(VALID_CONFIG))
    data["require_process"] = value
    path = Path(tmp) / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class MissingProcessesTests(unittest.TestCase):
    def test_none_uses_default_list(self) -> None:
        """기본값은 빈 목록이라 게이트가 꺼져 있고 프로세스 열거도 하지 않는다."""
        self.assertEqual(DEFAULT_REQUIRE_PROCESS, ())
        with mock.patch(
            "transport.process_guard._running_image_names",
            side_effect=AssertionError("열거가 호출됐다"),
        ):
            self.assertEqual(missing_processes(None), [])

    def test_none_with_nonempty_default_checks_that_list(self) -> None:
        """배포본이 기본값을 채우면 키 없는 config도 그 목록으로 검사한다."""
        with mock.patch(
            "transport.process_guard.DEFAULT_REQUIRE_PROCESS",
            ("access-control-agent.exe",),
        ), mock.patch(
            "transport.process_guard._running_image_names", return_value=set()
        ) as enum:
            self.assertEqual(missing_processes(None), ["access-control-agent.exe"])
        self.assertEqual(enum.call_count, 1)

    def test_normalizes_str_and_list(self) -> None:
        with mock.patch(
            "transport.process_guard._running_image_names", return_value=set()
        ) as enum:
            self.assertEqual(missing_processes("a.exe"), ["a.exe"])
            self.assertEqual(missing_processes(["a.exe", "b.exe"]), ["a.exe", "b.exe"])
        self.assertEqual(enum.call_count, 2)

    def test_empty_required_skips_enumeration(self) -> None:
        with mock.patch(
            "transport.process_guard._running_image_names",
            side_effect=AssertionError("열거가 호출됐다"),
        ):
            self.assertEqual(missing_processes([]), [])
            self.assertEqual(missing_processes(["  ", ""]), [])

    def test_names_are_stripped_before_match(self) -> None:
        with mock.patch(
            "transport.process_guard._running_image_names", return_value=set()
        ):
            self.assertEqual(missing_processes([" a.exe "]), ["a.exe"])

    def test_case_insensitive_match_and_missing_names(self) -> None:
        with mock.patch(
            "transport.process_guard._running_image_names",
            return_value={"a.exe", "b.exe"},
        ):
            self.assertEqual(missing_processes(["A.EXE", "c.exe"]), ["c.exe"])

    def test_enumeration_error_propagates(self) -> None:
        with mock.patch(
            "transport.process_guard._running_image_names", side_effect=OSError("boom")
        ), self.assertRaises(OSError):
            missing_processes(["a.exe"])

    def test_exception_carries_missing_names(self) -> None:
        exc = RequiredProcessMissing(["a.exe", "b.exe"])
        self.assertEqual(exc.missing, ["a.exe", "b.exe"])
        self.assertIn("a.exe, b.exe", str(exc))


class RequireProcessConfigTests(unittest.TestCase):
    def test_str_normalized_to_list(self) -> None:
        config = validate_config({**VALID_CONFIG, "require_process": "a.exe"})
        self.assertEqual(config["require_process"], ["a.exe"])

    def test_list_is_kept(self) -> None:
        config = validate_config({**VALID_CONFIG, "require_process": ["a.exe", "b.exe"]})
        self.assertEqual(config["require_process"], ["a.exe", "b.exe"])

    def test_absent_key_is_untouched(self) -> None:
        self.assertNotIn("require_process", validate_config(VALID_CONFIG))

    def test_bad_values_are_rejected(self) -> None:
        for bad in (123, "", "  ", ["a.exe", ""], ["a.exe", 1]):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                validate_config({**VALID_CONFIG, "require_process": bad})

    def test_config_store_validate_rejects_too(self) -> None:
        with self.assertRaises(ValueError):
            config_store.validate({**VALID_CONFIG, "require_process": [1]})


class BuildStartupGateTests(unittest.TestCase):
    def test_missing_process_refuses_startup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _config_with_require_process(["need.exe"], tmp)
            with mock.patch("app.missing_processes", return_value=["need.exe"]), (
                self.assertRaises(RequiredProcessMissing)
            ):
                app.build_startup(config_path=path)

    def test_running_process_builds_normally(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _config_with_require_process(["python.exe"], tmp)
            with mock.patch("app.missing_processes", return_value=[]):
                config, server, create = app.build_startup(config_path=path)
            try:
                self.assertEqual(config["require_process"], ["python.exe"])
                self.assertTrue(callable(create))
            finally:
                server.stop()

    def test_enumeration_failure_is_wrapped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _config_with_require_process(["need.exe"], tmp)
            with mock.patch("app.missing_processes", side_effect=OSError("boom")), (
                self.assertRaises(RequiredProcessCheckFailed)
            ) as ctx:
                app.build_startup(config_path=path)
            self.assertIn("확인할 수 없습니다", str(ctx.exception))

    def test_config_load_error_is_not_masked(self) -> None:
        with mock.patch("app.config_store.load", side_effect=OSError("locked")), (
            mock.patch("app.missing_processes", side_effect=AssertionError("호출됐다"))
        ), self.assertRaises(OSError):
            app.build_startup(config_path=Path("unused.json"))


class AppMainRefusalTests(unittest.TestCase):
    def test_refusal_exits_1(self) -> None:
        for exc in (RequiredProcessMissing(["need.exe"]), RequiredProcessCheckFailed(OSError("boom"))):
            with self.subTest(exc=type(exc).__name__), mock.patch(
                "app.build_startup", side_effect=exc
            ), mock.patch("app._startup_refusal") as refusal, mock.patch(
                "sys.argv", ["app.py"]
            ), self.assertRaises(SystemExit) as ctx:
                app.main()
            self.assertEqual(ctx.exception.code, 1)
            refusal.assert_called_once_with(str(exc))


class HeadlessMainGateTests(unittest.TestCase):
    def test_missing_process_returns_1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _config_with_require_process(["need.exe"], tmp)
            with mock.patch(
                "transport.__main__.missing_processes", return_value=["need.exe"]
            ):
                self.assertEqual(headless.main(["--config", str(path)]), 1)


if __name__ == "__main__":
    unittest.main()
