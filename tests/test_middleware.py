"""Behaviour tests for the llm_request middleware guards."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auto_router import middleware  # noqa: E402
from auto_router.settings import Settings  # noqa: E402

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

    def test_ignores_empty_pool(self):
        self.pool.return_value = []
        self.assertIsNone(call())

    def test_disabled_setting_is_a_no_op(self):
        middleware.configure(Settings(enabled=False))
        self.assertIsNone(call())

    def test_catalog_failure_is_fail_open(self):
        self.pool.side_effect = RuntimeError("network down")
        self.assertIsNone(call())


if __name__ == "__main__":
    unittest.main()
