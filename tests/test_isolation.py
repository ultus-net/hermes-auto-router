"""The mail seam's isolation contract.

Two halves, because they check different things:

1. **Static, always runs.** No seam imports another seam's package, and the
   mail root imports nothing local at module scope. That is the prevention: a
   mail import cannot even reach the router.
2. **Loader-driven, skips without Hermes.** Hermes' real deadline loader keeps
   a mail failure local, and abandons every registration when a mail import
   overruns the deadline. The design's N3: asserting that the router *survives*
   a timeout would contradict the loader.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import sys
import threading
import types
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: Each seam, and the packages it may not import.
FORBIDDEN: dict[str, tuple[str, ...]] = {
    "mail_organiser": ("auto_router", "providers"),
    "auto_router": ("mail_organiser",),
    "providers": ("mail_organiser",),
}

#: What the mail root may import at module scope. Everything else waits for a call.
MAIL_ROOT_IMPORTS = frozenset({"__future__", "logging", "sqlite3"})


def module_imports(path: Path) -> set[str]:
    """Every absolute module named by an import statement in one file."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
    return names


def crosses(names: set[str], package: str) -> bool:
    return any(name == package or name.startswith(package + ".") for name in names)


class NoCrossSeamImportTest(unittest.TestCase):
    def test_no_seam_imports_another(self) -> None:
        for seam, forbidden in FORBIDDEN.items():
            for path in sorted((ROOT / seam).rglob("*.py")):
                names = module_imports(path)
                for package in forbidden:
                    with self.subTest(seam=seam, imported=package, file=str(path.relative_to(ROOT))):
                        self.assertFalse(
                            crosses(names, package),
                            f"{path.relative_to(ROOT)} imports {package}",
                        )

    def test_the_mail_root_imports_nothing_local_at_module_scope(self) -> None:
        tree = ast.parse((ROOT / "mail_organiser" / "__init__.py").read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.level:
                self.fail(f"module-scope relative import at line {node.lineno}")
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                roots = (
                    {alias.name.split(".")[0] for alias in node.names}
                    if isinstance(node, ast.Import)
                    else {(node.module or "").split(".")[0]}
                )
                for root in roots:
                    self.assertIn(root, MAIL_ROOT_IMPORTS, f"line {node.lineno}")


class StubContext:
    """The registration surface the root seam touches, and nothing more."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.abandoned = False

    def get_config(self, key: str, default: Any = None) -> Any:
        return False if key == "enabled" else default

    def register_middleware(self, kind: str, callback: Any) -> None:
        self.calls.append(("middleware", kind))

    def register_cli_command(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("cli", name))

    def _abandon_load(self) -> None:
        self.abandoned = True


def load_root(name: str):
    """Load the real root ``__init__.py`` as a package, relative imports intact."""

    parent = name.rpartition(".")[0]
    if parent and parent not in sys.modules:
        # A relative submodule import needs the synthetic parent to exist.
        package = types.ModuleType(parent)
        package.__path__ = []
        sys.modules[parent] = package
    spec = importlib.util.spec_from_file_location(
        name,
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {ROOT / '__init__.py'}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def active_loader():
    try:
        from hermes_cli import plugins_loader
    except ImportError as exc:
        raise unittest.SkipTest(f"Hermes plugin loader not importable: {exc}") from exc
    return plugins_loader


ROUTER_CALLS = [("middleware", "llm_request"), ("cli", "auto-router")]


class RootGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = load_root("hermes_plugins.isolation_guard")
        self.addCleanup(sys.modules.pop, self.root.__name__, None)
        self.ctx = StubContext()

    def test_a_mail_seam_without_hermes_storage_disables_mail_only(self) -> None:
        with mock.patch.dict(sys.modules):
            sys.modules.pop("plugins", None)
            sys.modules.pop("plugins.plugin_storage", None)

            with self.assertLogs(self.root.__name__, level="ERROR") as logs:
                self.root.register(self.ctx)

        self.assertEqual(self.ctx.calls, ROUTER_CALLS)
        self.assertIn("auto-router", "\n".join(logs.output))
        self.assertIn("mail seam unavailable", "\n".join(logs.output))
        self.assertFalse(self.ctx.abandoned)

    def test_a_mail_store_failure_leaves_the_router_registered(self) -> None:
        mail = importlib.import_module(self.root.__name__ + ".mail_organiser")
        store = importlib.import_module(self.root.__name__ + ".mail_organiser.store")
        self.addCleanup(sys.modules.pop, mail.__name__, None)
        self.addCleanup(sys.modules.pop, store.__name__, None)

        with (
            mock.patch.object(store, "attach_plugin_store", side_effect=RuntimeError("boom")),
            self.assertLogs(self.root.__name__, level="ERROR"),
        ):
            self.root.register(self.ctx)

        self.assertEqual(self.ctx.calls, ROUTER_CALLS)
        self.assertIsNone(mail.connection())


class LoaderTest(unittest.TestCase):
    """Driven through Hermes' own deadline loader, so the contract is the host's."""

    def setUp(self) -> None:
        self.loader = active_loader()
        self.root = load_root("hermes_plugins.isolation_loader")
        self.addCleanup(sys.modules.pop, self.root.__name__, None)
        self.mail = importlib.import_module(self.root.__name__ + ".mail_organiser")
        self.addCleanup(sys.modules.pop, self.mail.__name__, None)
        self.store = importlib.import_module(self.root.__name__ + ".mail_organiser.store")
        self.addCleanup(sys.modules.pop, self.store.__name__, None)
        self.ctx = StubContext()

    def test_a_seam_that_raises_inside_its_guard_stays_local(self) -> None:
        with (
            mock.patch.object(self.store, "attach_plugin_store", side_effect=RuntimeError("boom")),
            self.assertLogs(self.root.__name__, level="ERROR") as logs,
        ):
            self.loader.run_with_load_deadline(
                self.root.__name__,
                self.ctx,
                lambda: self.root.register(self.ctx),
            )

        self.assertEqual(self.ctx.calls, ROUTER_CALLS)
        self.assertFalse(self.ctx.abandoned)
        self.assertIn("mail seam unavailable", "\n".join(logs.output))

    def test_a_seam_past_the_deadline_abandons_every_registration(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)

        def hang(*args: Any, **kwargs: Any) -> None:
            release.wait(10)
            raise RuntimeError("released")

        with (
            mock.patch.object(self.store, "attach_plugin_store", side_effect=hang),
            mock.patch.object(self.loader, "_resolve_plugin_load_timeout", return_value=0.2),
            self.assertRaises(self.loader.PluginLoadTimeout),
        ):
            self.loader.run_with_load_deadline(
                self.root.__name__,
                self.ctx,
                lambda: self.root.register(self.ctx),
            )

        self.assertTrue(self.ctx.abandoned)
        self.assertEqual(self.ctx.calls, ROUTER_CALLS)


if __name__ == "__main__":
    unittest.main()
