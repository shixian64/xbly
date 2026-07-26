from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "bbw_web"

# Concrete protocol imports belong at explicit adapter edges.  The migration
# exceptions are intentionally symbol-level rather than file-level so an
# already coupled module cannot silently acquire another protocol dependency.
PERMANENT_PROTOCOL_IMPORTS = frozenset(
    {
        (
            "bbw_web/providers/legacy_banghua.py",
            "bbw_protocol.adapters",
            "NativeBundle",
            "module",
        ),
        (
            "bbw_web/providers/legacy_banghua.py",
            "bbw_protocol.app",
            "BeibeiwuApp",
            "module",
        ),
        (
            "bbw_web/providers/legacy_banghua.py",
            "bbw_protocol.session",
            "Session",
            "module",
        ),
        (
            "bbw_web/transports/legacy_tim_rest.py",
            "bbw_protocol.adapters.tim_rest",
            "TimRestClient",
            "module",
        ),
    }
)

MIGRATION_DEBT_PROTOCOL_IMPORTS = frozenset()


def _is_protocol_module(module: str) -> bool:
    return module == "bbw_protocol" or module.startswith("bbw_protocol.")


class _ProtocolImportVisitor(ast.NodeVisitor):
    def __init__(self, relative_path: str) -> None:
        self.relative_path = relative_path
        self.function_depth = 0
        self.imports: set[tuple[str, str, str, str]] = set()

    @property
    def scope(self) -> str:
        return "local" if self.function_depth else "module"

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.function_depth += 1
        try:
            self.generic_visit(node)
        finally:
            self.function_depth -= 1

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.function_depth += 1
        try:
            self.generic_visit(node)
        finally:
            self.function_depth -= 1

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if _is_protocol_module(alias.name):
                self.imports.add(
                    (self.relative_path, alias.name, "<module>", self.scope)
                )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = str(node.module or "")
        if not _is_protocol_module(module):
            return
        for alias in node.names:
            self.imports.add(
                (self.relative_path, module, alias.name, self.scope)
            )

    def visit_Call(self, node: ast.Call) -> None:
        """Reject literal dynamic imports that would bypass Import nodes."""

        function_name = ""
        if isinstance(node.func, ast.Name):
            function_name = node.func.id
        elif (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "importlib"
        ):
            function_name = f"importlib.{node.func.attr}"
        if function_name in {"__import__", "import_module", "importlib.import_module"}:
            if node.args and isinstance(node.args[0], ast.Constant):
                module = node.args[0].value
                if isinstance(module, str) and _is_protocol_module(module):
                    self.imports.add(
                        (self.relative_path, module, "<dynamic>", self.scope)
                    )
        self.generic_visit(node)


def _direct_protocol_imports(paths: list[Path]) -> set[tuple[str, str, str, str]]:
    imports: set[tuple[str, str, str, str]] = set()
    for path in paths:
        relative_path = path.relative_to(ROOT).as_posix()
        tree = ast.parse(
            path.read_text(encoding="utf-8-sig"),
            filename=str(path),
        )
        visitor = _ProtocolImportVisitor(relative_path)
        visitor.visit(tree)
        imports.update(visitor.imports)
    return imports


class ProviderBoundaryContractTests(unittest.TestCase):
    def test_store_and_persistence_do_not_import_protocol_core(self) -> None:
        imports = _direct_protocol_imports(
            [WEB_ROOT / "store.py", WEB_ROOT / "persistence.py"]
        )
        self.assertEqual(imports, set())

    def test_web_protocol_imports_do_not_expand_beyond_explicit_edges(self) -> None:
        imports = _direct_protocol_imports(sorted(WEB_ROOT.rglob("*.py")))
        allowed = PERMANENT_PROTOCOL_IMPORTS | MIGRATION_DEBT_PROTOCOL_IMPORTS
        unexpected = imports - allowed
        self.assertEqual(
            unexpected,
            set(),
            "unexpected direct bbw_protocol imports: "
            + ", ".join(repr(item) for item in sorted(unexpected)),
        )

    def test_neutral_contracts_load_when_protocol_core_is_blocked(self) -> None:
        script = textwrap.dedent(
            """
            import importlib.abc
            import sys

            class BlockProtocol(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname == "bbw_protocol" or fullname.startswith("bbw_protocol."):
                        raise ModuleNotFoundError(
                            "bbw_protocol is blocked by the boundary contract",
                            name=fullname,
                        )
                    return None

            sys.meta_path.insert(0, BlockProtocol())

            from bbw_web import dependency_health
            from bbw_web.providers import contracts
            from bbw_web.transports import contracts as transport_contracts

            assert contracts.RuntimeProvider is not None
            assert dependency_health.DependencyStatusRegistry is not None
            assert transport_contracts.MessageHistoryTransport is not None
            assert not any(
                name == "bbw_protocol" or name.startswith("bbw_protocol.")
                for name in sys.modules
            )
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr or completed.stdout,
        )


if __name__ == "__main__":
    unittest.main()
