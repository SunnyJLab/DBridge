#!/usr/bin/env python3
"""Regression tests for per-tool module ownership and explicit assembly."""

from __future__ import annotations

import ast
import importlib
import inspect
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dbtools.registry import ToolDefinition, ToolRegistry
from dbtools.service import BUILTIN_TOOL_NAMES, ToolService, builtin_definitions
from dbtools.tools import definitions as assembled_definitions
from tests.test_http_transport import FakeDatabase

EXPECTED_TOOL_NAMES = (
    "list_connections",
    "my_scope",
    "list_tables",
    "describe_table",
    "search_catalog",
    "catalog_refresh",
    "sample_rows",
    "count_rows",
    "query",
    "resolve_object",
    "validate_sql",
    "explain",
    "partition_info",
    "table_profile",
    "cross_query",
    "query_summary",
    "suggest_joins",
    "compare_schema",
)
GROUPED_TOOL_FILES = (
    "basic_tools.py",
    "analysis_tools.py",
    "advanced_tools.py",
    "lookup_tools.py",
)
EXPECTED_COMMON_MODULES = (
    "params.py",
    "execution.py",
    "results.py",
    "catalog_access.py",
)
OLD_COMMON_PROVENANCE_FILES = (
    "basic.py",
    "analysis.py",
    "advanced.py",
)
COMMON_PACKAGE = Path(__file__).resolve().parent.parent / "dbtools" / "common"
TOOLS_PACKAGE = Path(__file__).resolve().parent.parent / "dbtools" / "tools"
DBTOOLS_PACKAGE = Path(__file__).resolve().parent.parent / "dbtools"


def _module_source(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imported_module_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module:
                    names.add(node.module)
            else:
                names.add("." * node.level + (node.module or ""))
            for alias in node.names:
                names.add(alias.name)
                if node.module:
                    names.add(f"{'.' * node.level}{node.module}.{alias.name}")
                elif node.level:
                    names.add(f"{'.' * node.level}{alias.name}")
    return names


def _tool_module(name: str) -> ModuleType:
    return importlib.import_module(f"dbtools.tools.{name}")


class ToolModuleAssemblyTests(unittest.TestCase):
    def test_assembled_order_is_exactly_the_contract_names(self) -> None:
        assembled = assembled_definitions()
        names = tuple(item.name for item in assembled)
        self.assertEqual(names, EXPECTED_TOOL_NAMES)
        self.assertEqual(len(names), 18)
        self.assertEqual(len(set(names)), 18)
        self.assertEqual(BUILTIN_TOOL_NAMES, EXPECTED_TOOL_NAMES)
        self.assertEqual(tuple(item.name for item in builtin_definitions()), EXPECTED_TOOL_NAMES)
        self.assertIsInstance(assembled, tuple)
        self.assertTrue(all(isinstance(item, ToolDefinition) for item in assembled))

    def test_each_handler_lives_on_its_named_module_and_matches_definition(self) -> None:
        for name, definition in zip(EXPECTED_TOOL_NAMES, assembled_definitions(), strict=True):
            with self.subTest(name=name):
                module = _tool_module(name)
                self.assertEqual(module.__name__, f"dbtools.tools.{name}")
                self.assertTrue(callable(getattr(module, name)))
                handler = getattr(module, name)
                self.assertIs(handler, definition.handler)
                self.assertEqual(handler.__module__, f"dbtools.tools.{name}")
                self.assertIs(inspect.unwrap(handler), handler)
                owned = module.definition()
                self.assertIs(owned.handler, handler)
                self.assertEqual(owned.name, name)
                self.assertEqual(owned.handler.__module__, f"dbtools.tools.{name}")

    def test_definition_owns_metadata_and_returns_fresh_objects(self) -> None:
        for name in EXPECTED_TOOL_NAMES:
            with self.subTest(name=name):
                module = _tool_module(name)
                first = module.definition()
                second = module.definition()
                self.assertIsNot(first, second)
                self.assertEqual(first.name, name)
                self.assertEqual(first.description, second.description)
                self.assertTrue(first.description.strip())
                self.assertIs(first.handler, second.handler)
                self.assertIs(first.handler, getattr(module, name))
                self.assertIsNot(first.example, second.example)
                self.assertEqual(first.example, second.example)
                first.example["name"] = "mutated"
                self.assertEqual(second.example["name"], name)
                self.assertEqual(module.definition().example["name"], name)
                self.assertIsInstance(first.params, tuple)
                mutated_metadata = first.to_metadata()
                mutated_metadata["name"] = "mutated"
                mutated_metadata["example"]["name"] = "mutated"
                self.assertEqual(second.to_metadata()["name"], name)
                self.assertEqual(second.to_metadata()["example"]["name"], name)

    def test_definitions_register_and_dispatch_through_real_tool_registry(self) -> None:
        registry = ToolRegistry()
        assembled = assembled_definitions()
        for definition in assembled:
            registry.register(definition)
        metadata = registry.list_metadata()
        self.assertEqual(tuple(item["name"] for item in metadata), EXPECTED_TOOL_NAMES)
        self.assertEqual(len(metadata), 18)
        metadata[0]["name"] = "mutated"
        metadata[0]["example"]["name"] = "mutated"
        self.assertEqual(registry.list_metadata()[0]["name"], "list_connections")
        self.assertEqual(
            registry.list_metadata()[0]["example"]["name"],
            "list_connections",
        )
        for name in EXPECTED_TOOL_NAMES:
            with self.subTest(name=name):
                registered = registry.get(name)
                module_definition = _tool_module(name).definition()
                self.assertIsNotNone(registered)
                self.assertIs(registered.handler, module_definition.handler)
                self.assertEqual(registered.description, module_definition.description)
                self.assertEqual(
                    [spec.to_metadata() for spec in registered.params],
                    [spec.to_metadata() for spec in module_definition.params],
                )

        calls: list[str] = []

        def _probe(context: Any, **arguments: Any) -> dict[str, Any]:
            calls.append(context["marker"])
            return {"ok": True, "arguments": dict(arguments)}

        probe_registry = ToolRegistry()
        original = _tool_module("list_connections").definition()
        probe_registry.register(
            ToolDefinition(
                name=original.name,
                description=original.description,
                params=original.params,
                example=dict(original.example),
                handler=_probe,
            )
        )
        result = probe_registry.dispatch("list_connections", {}, {"marker": "via-registry"})
        self.assertEqual(result, {"ok": True, "arguments": {}})
        self.assertEqual(calls, ["via-registry"])

    def test_service_registers_assembled_handlers_and_dispatches_list_connections(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        service = ToolService(
            {
                "proxy": {"query_timeout": 5},
                "connections": [
                    {"name": "dw", "dsn": "dw:1521/DW"},
                    {"name": "src", "dsn": "src:1521/SRC"},
                ],
            },
            base_dir=temp.name,
            database=FakeDatabase(),
            auto_refresh_catalog=False,
        )
        self.addCleanup(service.close)
        listed = service.list_tools()
        self.assertEqual(tuple(item["name"] for item in listed), EXPECTED_TOOL_NAMES)
        for name in EXPECTED_TOOL_NAMES:
            registered = service.registry.get(name)
            module = _tool_module(name)
            self.assertIs(registered.handler, getattr(module, name))
            self.assertEqual(registered.handler.__module__, f"dbtools.tools.{name}")
        payload = service.dispatch("list_connections", {})
        self.assertNotIn("error", payload)
        names = [item["name"] for item in payload["connections"]]
        self.assertEqual(names, ["dw", "src"])
        self.assertTrue(all(item["state"] == "ok" for item in payload["connections"]))

    def test_assembly_is_explicit_and_does_not_scan(self) -> None:
        source = (TOOLS_PACKAGE / "__init__.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = _imported_module_names(tree)
        for name in EXPECTED_TOOL_NAMES:
            self.assertIn(name, source)
        self.assertNotIn("pkgutil", imported)
        self.assertNotIn("importlib", imported)
        self.assertNotIn("pkgutil", source)
        self.assertNotIn("importlib", source)
        self.assertNotIn("glob", source)
        self.assertNotIn("iter_modules", source)
        self.assertNotIn("__path__", source)
        self.assertIn("_TOOL_MODULES", source)
        self.assertIn("module.definition()", source)
        assignments = [
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "_TOOL_MODULES" for target in node.targets)
        ]
        self.assertEqual(len(assignments), 1)
        value = assignments[0].value
        self.assertIsInstance(value, ast.Tuple)
        assembled_names = []
        for elt in value.elts:
            self.assertIsInstance(elt, ast.Name)
            assembled_names.append(elt.id)
        self.assertEqual(tuple(assembled_names), EXPECTED_TOOL_NAMES)

    def test_common_modules_never_import_tools(self) -> None:
        self.assertTrue(COMMON_PACKAGE.is_dir())
        inspected = 0
        for path in sorted(COMMON_PACKAGE.rglob("*.py")):
            tree = _module_source(path)
            inspected += 1
            for name in _imported_module_names(tree):
                self.assertFalse(
                    name == "tools"
                    or name.startswith("tools.")
                    or name == "dbtools.tools"
                    or name.startswith("dbtools.tools.")
                    or name.endswith(".tools")
                    or ".tools." in name
                    or name.startswith("..tools")
                    or name.startswith(".tools"),
                    f"{path} imports tools via {name!r}",
                )
        self.assertGreaterEqual(inspected, 3)

    def test_old_grouped_tool_files_are_gone(self) -> None:
        remaining = [name for name in GROUPED_TOOL_FILES if (DBTOOLS_PACKAGE / name).exists()]
        self.assertEqual(remaining, [])
        for path in TOOLS_PACKAGE.glob("*.py"):
            if path.name == "__init__.py":
                continue
            self.assertIn(path.stem, EXPECTED_TOOL_NAMES)

    def test_every_global_referenced_by_tool_code_resolves(self) -> None:
        """import 누락을 잡는다 — 조건 분기 안에서만 쓰이는 이름은 테스트가 안 밟는다.

        각 도구 모듈의 함수가 LOAD_GLOBAL 하는 이름이 실제로 해석되는지 본다.
        (리팩터 중 validate_sql 의 ora_fields import 누락을 이 검사로 잡았다.)
        """
        import builtins
        import dis
        import importlib
        import types

        for name in EXPECTED_TOOL_NAMES:
            module = importlib.import_module(f"dbtools.tools.{name}")
            for attr in vars(module).values():
                if not isinstance(attr, types.FunctionType) or attr.__module__ != module.__name__:
                    continue
                stack = [attr.__code__]
                while stack:
                    code = stack.pop()
                    stack.extend(c for c in code.co_consts if hasattr(c, "co_names"))
                    for ins in dis.get_instructions(code):
                        if ins.opname != "LOAD_GLOBAL":
                            continue
                        referenced = ins.argval
                        with self.subTest(module=name, function=attr.__qualname__, ref=referenced):
                            self.assertTrue(
                                referenced in vars(module) or hasattr(builtins, referenced),
                                f"dbtools/tools/{name}.py: {attr.__qualname__} 가 참조하는 "
                                f"{referenced!r} 를 모듈에서 찾을 수 없습니다",
                            )

    def test_tools_only_call_methods_the_real_adapters_implement(self) -> None:
        """도구가 부르는 협력자 메서드가 실제 구현에 존재하는지 본다.

        `query_to_file` 은 존재하지 않는 ``stream_rows``/``iter_rows`` 를,
        `validate_sql` 은 존재하지 않는 ``parse`` 를 호출하고 있었다. 둘 다
        페이크만 그 메서드를 갖고 있어서 테스트는 통과했고 실 DB 에서만
        조용히 죽었다. 이 검사가 그 구멍을 막는다.
        """
        import ast

        from dbtools.catalog import CatalogCache
        from dbtools.database import OracleDatabase

        surfaces = {"database": OracleDatabase, "catalog": CatalogCache}
        offenders: list[str] = []
        for path in sorted(TOOLS_PACKAGE.glob("*.py")) + sorted(COMMON_PACKAGE.glob("*.py")):
            if path.name == "__init__.py":
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                # context.database.X / context.catalog.X 또는 getattr(db, "X", ...)
                target = attr = None
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute):
                    target, attr = node.value.attr, node.attr
                elif (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "getattr"
                    and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant)
                    and isinstance(node.args[1].value, str)
                    and isinstance(node.args[0], ast.Attribute)
                ):
                    target, attr = node.args[0].attr, node.args[1].value
                if target not in surfaces or not attr or attr.startswith("_"):
                    continue
                if not hasattr(surfaces[target], attr):
                    offenders.append(f"{path.name}: {target}.{attr}")
        self.assertEqual(sorted(set(offenders)), [])

    def test_common_package_is_functional_by_name(self) -> None:
        for name in EXPECTED_COMMON_MODULES:
            with self.subTest(name=name):
                self.assertTrue((COMMON_PACKAGE / name).is_file(), f"missing functional common module: {name}")
        for name in OLD_COMMON_PROVENANCE_FILES:
            with self.subTest(name=name):
                self.assertFalse((COMMON_PACKAGE / name).exists(), f"old provenance common module still present: {name}")


if __name__ == "__main__":
    unittest.main()
