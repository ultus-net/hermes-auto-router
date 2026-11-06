"""Behaviour tests for the llm_request middleware guards."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auto_router import middleware
from auto_router.settings import Settings

REQUEST = {"model": "openrouter/auto", "messages": [{"role": "user", "content": "hi"}]}


def call(**overrides):
    kwargs = {
        "provider": "openrouter",
        "model": "openrouter/auto",
        "base_url": "https://openrouter.ai/api/v1",
        "request": dict(REQUEST),
    }
    kwargs.update(overrides)
    return middleware.on_llm_request(**kwargs)


class MiddlewareTest(unittest.TestCase):
    def setUp(self):
        middleware.configure(Settings(aliases=("~a/one-latest",)))
        patcher = mock.patch.object(middleware.catalog, "resolved_pool", return_value=["a/one-1"])
        self.pool = patcher.start()
        self.addCleanup(patcher.stop)

    def test_injects_pool_for_auto_router(self):
        result = call()
        self.assertIsNotNone(result)
        self.assertEqual(
            result["request"]["extra_body"]["plugins"],
            [{"id": "auto-router", "allowed_models": ["a/one-1"]}],
        )
        self.assertEqual(result["source"], "auto-router")

    def test_ignores_other_models(self):
        self.assertIsNone(call(model="anthropic/claude-sonnet-4.6"))

    def test_ignores_other_providers(self):
        self.assertIsNone(call(provider="synthetic"))

    def test_ignores_custom_base_url_named_openrouter(self):
        self.assertIsNone(call(base_url="https://proxy.internal/v1"))

    def test_ignores_lookalike_host(self):
        self.assertIsNone(call(base_url="https://openrouter.ai.attacker.test/v1"))

    def test_ignores_path_that_mentions_openrouter(self):
        self.assertIsNone(call(base_url="https://proxy.internal/openrouter.ai/v1"))

    def test_accepts_openrouter_subdomain(self):
        self.assertIsNotNone(call(base_url="https://api.openrouter.ai/v1"))

    def test_ignores_empty_base_url(self):
        self.assertIsNone(call(base_url=""))
        self.assertIsNone(call(base_url=None))

    def test_ignores_empty_pool(self):
        self.pool.return_value = []
        self.assertIsNone(call())

    def test_disabled_setting_is_a_no_op(self):
        middleware.configure(Settings(enabled=False))
        self.assertIsNone(call())

    def test_catalog_failure_is_fail_open(self):
        self.pool.side_effect = RuntimeError("network down")
        self.assertIsNone(call())


class RequestPathTest(unittest.TestCase):
    """The middleware must not fetch a catalog on the request path."""

    def setUp(self):
        middleware.configure(Settings(aliases=("~a/one-latest",)))
        middleware.catalog._MEMORY.clear()
        middleware.catalog._REFRESHING.clear()
        self.addCleanup(middleware.catalog._MEMORY.clear)
        self.addCleanup(middleware.catalog._REFRESHING.clear)

    def test_cold_cache_schedules_a_refresh_and_returns_unchanged(self):
        with (
            mock.patch.object(
                middleware.catalog,
                "_read_disk",
                return_value=middleware.catalog._MISSING,
            ),
            mock.patch.object(middleware.catalog, "openrouter_models") as models,
            mock.patch.object(middleware.catalog, "_refresh_in_background") as refresh,
        ):
            self.assertIsNone(call())
        models.assert_not_called()
        refresh.assert_called_once()


if __name__ == "__main__":
    unittest.main()
