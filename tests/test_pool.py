"""Behaviour tests for the pure routing logic."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auto_router.pool import (  # noqa: E402
    alias_targets,
    auto_router_entry,
    inject,
    merge_auto_router,
    resolve_pool,
)


class AliasTargetsTest(unittest.TestCase):
    def test_maps_alias_to_concrete_slug(self):
        models = [
            {"id": "~anthropic/claude-sonnet-latest", "alias_target": {"slug": "anthropic/claude-sonnet-4.6"}},
            {"id": "anthropic/claude-sonnet-4.6"},
        ]
        self.assertEqual(
            alias_targets(models),
            {"~anthropic/claude-sonnet-latest": "anthropic/claude-sonnet-4.6"},
        )

    def test_skips_alias_without_live_target(self):
        models = [{"id": "~openai/gpt-luna-latest"}, {"id": "~openai/gpt-sol-latest", "alias_target": {}}]
        self.assertEqual(alias_targets(models), {})

    def test_ignores_concrete_ids(self):
        models = [{"id": "openai/gpt-6-astra", "alias_target": {"slug": "openai/gpt-6-astra"}}]
        self.assertEqual(alias_targets(models), {})


class ResolvePoolTest(unittest.TestCase):
    def test_preserves_alias_order_and_dedupes(self):
        targets = {"~a/one-latest": "a/one-1", "~b/two-latest": "b/two-1", "~c/one-again-latest": "a/one-1"}
        pool = resolve_pool(["~b/two-latest", "~a/one-latest", "~c/one-again-latest"], targets)
        self.assertEqual(pool, ["b/two-1", "a/one-1"])

    def test_unresolvable_aliases_are_dropped(self):
        self.assertEqual(resolve_pool(["~nope/latest"], {}), [])


class InjectTest(unittest.TestCase):
    def test_adds_auto_router_entry(self):
        request = {"model": "openrouter/auto", "messages": []}
        out = inject(request, ["a/one-1"], "high")
        self.assertEqual(
            out["extra_body"]["plugins"],
            [{"id": "auto-router", "allowed_models": ["a/one-1"], "cost_tier": "high"}],
        )
        self.assertNotIn("extra_body", request, "input request must not be mutated")

    def test_preserves_unrelated_plugins(self):
        request = {"extra_body": {"plugins": [{"id": "pareto-router", "min_coding_score": 0.5}]}}
        out = inject(request, ["a/one-1"])
        ids = [p["id"] for p in out["extra_body"]["plugins"]]
        self.assertEqual(ids, ["pareto-router", "auto-router"])

    def test_replaces_stale_auto_router_entry(self):
        request = {"extra_body": {"plugins": [{"id": "auto-router", "allowed_models": ["stale/model"]}]}}
        out = inject(request, ["a/one-1"])
        self.assertEqual(len(out["extra_body"]["plugins"]), 1)
        self.assertEqual(out["extra_body"]["plugins"][0]["allowed_models"], ["a/one-1"])

    def test_empty_pool_is_a_no_op(self):
        request = {"model": "openrouter/auto"}
        self.assertEqual(inject(request, []), request)

    def test_merge_keeps_non_mapping_entries(self):
        self.assertEqual(merge_auto_router(["junk"], auto_router_entry(["a/one-1"])), ["junk", auto_router_entry(["a/one-1"])])


if __name__ == "__main__":
    unittest.main()
