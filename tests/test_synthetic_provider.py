"""Behaviour tests for the Synthetic provider profile.

Loads the provider module through its real file path so the test exercises the
same ``register_provider`` call the discovery path makes.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
HERMES_SRC = HERMES_HOME / "hermes-agent"
for path in (str(HERMES_SRC), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

PROVIDER = ROOT / "providers" / "synthetic" / "__init__.py"


def load_provider():
    # The provider imports Hermes' own `providers` package. A bare checkout
    # (CI, a docs machine) has no Hermes install. Detect that up front so a real
    # import error inside Hermes still fails the suite instead of skipping it.
    if not (HERMES_SRC / "providers" / "__init__.py").is_file():
        raise unittest.SkipTest(f"Hermes Agent is not installed at {HERMES_SRC}")
    spec = importlib.util.spec_from_file_location("auto_router_synthetic_provider", PROVIDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ParseAliasesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_provider()

    def test_keeps_only_syn_aliases(self):
        payload = {
            "data": [
                {"id": "syn:large:text"},
                {"id": "infinite:small:vision"},
                {"id": "hf:deepseek-ai/DeepSeek-V4.1"},
                {"id": "syn:small:text"},
            ]
        }
        self.assertEqual(self.module.parse_aliases(payload), ["syn:large:text", "syn:small:text"])

    def test_dedupes_and_preserves_order(self):
        payload = [{"id": "syn:b"}, {"id": "syn:a"}, {"id": "syn:b"}]
        self.assertEqual(self.module.parse_aliases(payload), ["syn:b", "syn:a"])

    def test_tolerates_garbage(self):
        self.assertEqual(self.module.parse_aliases(None), [])
        self.assertEqual(self.module.parse_aliases({"data": [None, 3, {"id": 7}]}), [])


class ProfileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_provider()
        cls.profile = cls.module.synthetic

    def test_registers_under_synthetic(self):
        # Assert the resolved profile's behaviour, not object identity: discovery
        # may register the installed copy of this same module, and last-writer-wins
        # is the documented contract.
        from providers import get_provider_profile

        for name in ("synthetic", "syn"):
            profile = get_provider_profile(name)
            self.assertIsNotNone(profile, name)
            self.assertEqual(profile.name, "synthetic")
            self.assertEqual(profile.base_url, "https://api.synthetic.new/openai/v1")

    def test_points_at_the_openai_compatible_endpoint(self):
        self.assertEqual(self.profile.base_url, "https://api.synthetic.new/openai/v1")
        self.assertEqual(self.profile.models_url, "https://api.synthetic.new/openai/v1/models")
        self.assertEqual(self.profile.env_vars[0], "SYNTHETIC_API_KEY")

    def test_aux_models_track_the_vision_split(self):
        self.assertEqual(self.profile.resolve_aux_model(), "syn:small:text")
        self.assertEqual(self.profile.resolve_aux_model(vision=True), "syn:small:vision")

    def test_fallback_catalog_is_all_syn_aliases(self):
        self.assertTrue(self.profile.fallback_models)
        self.assertTrue(all(m.startswith("syn:") for m in self.profile.fallback_models))


if __name__ == "__main__":
    unittest.main()
