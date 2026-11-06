"""Behaviour tests for the two-layer catalog cache."""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auto_router import catalog


class CachedTest(unittest.TestCase):
    def setUp(self):
        catalog._MEMORY.clear()
        self.addCleanup(catalog._MEMORY.clear)
        read = mock.patch.object(catalog, "_read_disk", return_value=catalog._MISSING)
        write = mock.patch.object(catalog, "_write_disk")
        self.read_disk = read.start()
        self.write_disk = write.start()
        self.addCleanup(read.stop)
        self.addCleanup(write.stop)

    def test_caches_empty_results(self):
        produce = mock.Mock(return_value=[])
        self.assertEqual(catalog._cached("k", 60, produce), [])
        self.assertEqual(catalog._cached("k", 60, produce), [])
        produce.assert_called_once()

    def test_empty_result_expires_on_the_negative_ttl(self):
        clock = [1000.0]
        fake_time = mock.Mock(time=lambda: clock[0])
        produce = mock.Mock(side_effect=[[], ["a/one-1"]])
        with mock.patch.object(catalog, "time", fake_time):
            catalog._cached("k", 6 * 60 * 60, produce)
            clock[0] += catalog.NEGATIVE_TTL_SECONDS + 1
            self.assertEqual(catalog._cached("k", 6 * 60 * 60, produce), ["a/one-1"])
        self.assertEqual(produce.call_count, 2)

    def test_failure_is_cached_and_reraised(self):
        produce = mock.Mock(side_effect=RuntimeError("catalog down"))
        with self.assertRaises(RuntimeError):
            catalog._cached("k", 60, produce)
        self.assertEqual(catalog._cached("k", 60, produce), [])
        produce.assert_called_once()

    def test_refresh_skips_both_cache_layers(self):
        self.read_disk.return_value = ["stale"]
        produce = mock.Mock(return_value=["fresh"])
        self.assertEqual(catalog._cached("k", 60, produce, refresh=True), ["fresh"])
        self.read_disk.assert_not_called()

    def test_non_blocking_call_never_waits_for_another_fetch(self):
        produce = mock.Mock(return_value=["fresh"])
        catalog._LOCK.acquire()
        try:
            self.assertIs(
                catalog._cached("k", 60, produce, blocking=False),
                catalog._MISSING,
            )
        finally:
            catalog._LOCK.release()
        produce.assert_not_called()

    def test_disk_hit_populates_memory(self):
        self.read_disk.return_value = ["a/one-1"]
        produce = mock.Mock()
        self.assertEqual(catalog._cached("k", 60, produce), ["a/one-1"])
        produce.assert_not_called()


class ResolvedPoolTest(unittest.TestCase):
    def setUp(self):
        catalog._MEMORY.clear()
        self.addCleanup(catalog._MEMORY.clear)
        for name, value in (("_read_disk", catalog._MISSING), ("_write_disk", None)):
            patcher = mock.patch.object(catalog, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_resolves_aliases_through_the_target_map(self):
        targets = {"~a/one-latest": "a/one-1", "~b/two-latest": "b/two-1"}
        with mock.patch.object(catalog, "alias_target_map", return_value=targets) as targets_map:
            pool = catalog.resolved_pool(["~b/two-latest", "~a/one-latest"])
        self.assertEqual(pool, ["b/two-1", "a/one-1"])
        targets_map.assert_called_once()

    def test_unresolvable_alias_yields_an_empty_pool(self):
        with mock.patch.object(catalog, "alias_target_map", return_value={}):
            self.assertEqual(catalog.resolved_pool(["~nope/latest"]), [])

    def test_resolves_with_the_real_nested_caches(self):
        models = [{"id": "~a/one-latest", "alias_target": {"slug": "a/one-1"}}]
        result: list[str] = []

        def resolve():
            result.extend(catalog.resolved_pool(["~a/one-latest"]))

        with mock.patch.object(catalog, "openrouter_models", return_value=models):
            worker = threading.Thread(target=resolve)
            worker.start()
            worker.join(5)
        self.assertFalse(worker.is_alive(), "resolved_pool deadlocked on the cache lock")
        self.assertEqual(result, ["a/one-1"])


if __name__ == "__main__":
    unittest.main()
