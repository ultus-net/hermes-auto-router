"""Behaviour tests for the catalog caches and the request-path guarantee."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auto_router import catalog


class CachedTest(unittest.TestCase):
    def setUp(self):
        catalog._MEMORY.clear()
        catalog._REFRESHING.clear()
        self.addCleanup(catalog._MEMORY.clear)
        self.addCleanup(catalog._REFRESHING.clear)
        read = mock.patch.object(catalog, "_read_disk", return_value=catalog._MISSING)
        write = mock.patch.object(catalog, "_write_disk")
        refresh = mock.patch.object(catalog, "_refresh_in_background")
        self.read_disk = read.start()
        self.write_disk = write.start()
        self.refresh_in_background = refresh.start()
        self.addCleanup(read.stop)
        self.addCleanup(write.stop)
        self.addCleanup(refresh.stop)

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

    def test_refresh_skips_both_cache_layers_and_writes_the_result(self):
        self.read_disk.return_value = ["stale"]
        produce = mock.Mock(return_value=["fresh"])
        self.assertEqual(catalog._cached("k", 60, produce, refresh=True), ["fresh"])
        self.read_disk.assert_not_called()
        self.write_disk.assert_called_once_with("k", ["fresh"])

    def test_cold_non_blocking_call_schedules_and_does_not_produce(self):
        produce = mock.Mock(return_value=["fresh"])
        self.assertIs(
            catalog._cached("k", 60, produce, blocking=False),
            catalog._MISSING,
        )
        self.refresh_in_background.assert_called_once()
        produce.assert_not_called()

    def test_non_blocking_call_serves_stale_value_while_refreshing(self):
        catalog._MEMORY["k"] = (catalog.time.time() - 7200, ["old"])
        produce = mock.Mock(return_value=["fresh"])
        self.assertEqual(catalog._cached("k", 60, produce, blocking=False), ["old"])
        self.refresh_in_background.assert_called_once()
        produce.assert_not_called()

    def test_disk_hit_populates_memory(self):
        self.read_disk.return_value = ["a/one-1"]
        produce = mock.Mock()
        self.assertEqual(catalog._cached("k", 60, produce), ["a/one-1"])
        produce.assert_not_called()

    def test_background_refresh_populates_the_cache(self):
        produce = mock.Mock(return_value=["fresh"])
        catalog._refresh("k", 60, produce)
        self.assertEqual(catalog._cached("k", 60, produce), ["fresh"])
        self.assertEqual(produce.call_count, 1)
        self.assertNotIn("k", catalog._REFRESHING)


class ResolvedPoolTest(unittest.TestCase):
    def setUp(self):
        catalog._MEMORY.clear()
        catalog._REFRESHING.clear()
        self.addCleanup(catalog._MEMORY.clear)
        self.addCleanup(catalog._REFRESHING.clear)
        for name, value in (("_read_disk", catalog._MISSING), ("_write_disk", None)):
            patcher = mock.patch.object(catalog, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_uses_the_cached_target_map(self):
        targets = {"~a/one-latest": "a/one-1", "~b/two-latest": "b/two-1"}
        with mock.patch.object(catalog, "alias_target_map", return_value=targets) as targets_map:
            pool = catalog.resolved_pool(["~b/two-latest", "~a/one-latest"])
        self.assertEqual(pool, ["b/two-1", "a/one-1"])
        targets_map.assert_called_once()

    def test_unresolvable_alias_yields_an_empty_pool(self):
        with mock.patch.object(catalog, "alias_target_map", return_value={}):
            self.assertEqual(catalog.resolved_pool(["~nope/latest"]), [])

    def test_non_blocking_cold_path_does_not_fetch(self):
        with (
            mock.patch.object(catalog, "openrouter_models") as models,
            mock.patch.object(catalog, "_refresh_in_background") as refresh,
        ):
            self.assertEqual(catalog.resolved_pool(["~a/one-latest"], blocking=False), [])
        models.assert_not_called()
        refresh.assert_called_once()

    def test_resolves_from_the_real_map_and_fetches_once(self):
        models = [{"id": "~a/one-latest", "alias_target": {"slug": "a/one-1"}}]
        with mock.patch.object(catalog, "openrouter_models", return_value=models) as fetch:
            self.assertEqual(catalog.resolved_pool(["~a/one-latest"]), ["a/one-1"])
            self.assertEqual(catalog.resolved_pool(["~a/one-latest"]), ["a/one-1"])
        fetch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
