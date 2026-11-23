"""The durable store attach, driven through a stand-in for Hermes storage.

The fake ``plugins.plugin_storage`` keeps Hermes' real shape: ``plugin_db(name,
filename="data.db")`` returns a connection with ``row_factory=None``. A test
that passed because the fake pre-set the row factory would prove nothing.
"""

from __future__ import annotations

import importlib
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mail_organiser
from mail_organiser import store


class FakeStorage(types.ModuleType):
    """Stand in for ``plugins.plugin_storage`` with Hermes' signature and factory."""

    def __init__(self, directory: Path) -> None:
        super().__init__("plugins.plugin_storage")
        self.calls: list[tuple[str, str]] = []
        self.directory = directory
        self.plugin_db = self._open

    def _open(self, name: str, filename: str = "data.db") -> sqlite3.Connection:
        self.calls.append((name, filename))
        return sqlite3.connect(self.directory / filename)


class AttachTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.storage = FakeStorage(Path(self.directory.name))
        package = types.ModuleType("plugins")
        package.plugin_storage = self.storage
        package.__path__ = []
        patcher = mock.patch.dict(
            sys.modules,
            {"plugins": package, "plugins.plugin_storage": self.storage},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def attach(self) -> sqlite3.Connection:
        conn = store.attach_plugin_store()
        self.addCleanup(conn.close)
        return conn

    def test_attach_uses_the_plugin_store_name_and_applies_the_manifest(self) -> None:
        conn = self.attach()

        self.assertEqual(self.storage.calls, [("hermes-personal", "data.db")])
        self.assertIn("table:mail_messages", store.schema_manifest(conn))
        self.assertEqual(store.schema_version(conn), store.SCHEMA_VERSION)

    def test_attach_sets_the_row_factory_the_queries_need(self) -> None:
        conn = self.attach()

        self.assertIs(conn.row_factory, sqlite3.Row)
        row = conn.execute("SELECT 1 AS answer").fetchone()
        self.assertEqual(row["answer"], 1)

    def test_attach_is_idempotent(self) -> None:
        self.attach()
        second = self.attach()

        self.assertEqual(store.schema_version(second), store.SCHEMA_VERSION)

    def test_register_attaches_one_connection_and_reuses_it(self) -> None:
        importlib.reload(mail_organiser)
        self.addCleanup(setattr, mail_organiser, "_connection", None)

        mail_organiser.register(object())
        conn = mail_organiser.connection()
        if conn is None:
            self.fail("register did not attach a store")
        self.addCleanup(conn.close)

        mail_organiser.register(object())

        self.assertIs(mail_organiser.connection(), conn)
        self.assertEqual(self.storage.calls, [("hermes-personal", "data.db")])
        self.assertEqual(store.schema_version(conn), store.SCHEMA_VERSION)

    def test_a_store_failure_reaches_the_caller(self) -> None:
        with (
            mock.patch.object(self.storage, "plugin_db", side_effect=RuntimeError("no storage")),
            self.assertRaises(RuntimeError),
        ):
            store.attach_plugin_store()


class NoStorageTest(unittest.TestCase):
    def test_the_seam_returns_none_before_register_runs(self) -> None:
        importlib.reload(mail_organiser)
        self.addCleanup(setattr, mail_organiser, "_connection", None)

        self.assertIsNone(mail_organiser.connection())


if __name__ == "__main__":
    unittest.main()
