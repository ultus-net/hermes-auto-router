"""Behaviour tests for the CLI writes, credential lookup, and structured values."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auto_router import cli, credentials
from auto_router.settings import Settings


def namespace(**overrides):
    base = {"primary": None, "small": None, "no_default": False, "dry_run": False}
    base.update(overrides)
    return argparse.Namespace(**base)


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.set_config = mock.Mock()
        fake = types.ModuleType("hermes_cli.config")
        fake.set_config_value = self.set_config
        modules = mock.patch.dict(sys.modules, {"hermes_cli.config": fake})
        modules.start()
        self.addCleanup(modules.stop)
        self.use_settings(Settings(primary_model="syn:large:text", small_model="syn:small:text"))
        caps = mock.patch.object(cli, "_capabilities", return_value={"syn:large:text": {}})
        caps.start()
        self.addCleanup(caps.stop)

    def use_settings(self, settings):
        patcher = mock.patch.object(cli, "_settings", return_value=settings)
        patcher.start()
        self.addCleanup(patcher.stop)

    def writes(self):
        return {call.args[0]: call.args[1] for call in self.set_config.call_args_list}

    def run_apply(self, args):
        with contextlib.redirect_stdout(io.StringIO()):
            return cli._apply(args)

    def test_defaults_come_from_plugin_settings(self):
        self.use_settings(
            Settings(primary_model="syn:large:vision", small_model="syn:small:vision")
        )
        self.assertEqual(self.run_apply(namespace()), 0)
        writes = self.writes()
        self.assertEqual(writes["model.default"], "syn:large:vision")
        self.assertEqual(writes["auxiliary.title_generation.model"], "syn:small:vision")

    def test_explicit_flags_win_over_settings(self):
        self.assertEqual(self.run_apply(namespace(primary="syn:small:text")), 0)
        self.assertEqual(self.writes()["model.default"], "syn:small:text")

    def test_no_default_leaves_the_model_keys_alone(self):
        self.assertEqual(self.run_apply(namespace(no_default=True)), 0)
        writes = self.writes()
        self.assertNotIn("model.provider", writes)
        self.assertNotIn("model.default", writes)
        self.assertIn("fallback_providers", writes)

    def test_rejects_non_syn_aliases(self):
        self.assertEqual(self.run_apply(namespace(primary="openai/gpt-6-astra")), 2)
        self.set_config.assert_not_called()

    def test_dry_run_writes_nothing(self):
        self.assertEqual(self.run_apply(namespace(dry_run=True)), 0)
        self.set_config.assert_not_called()


class StructuredValueTest(unittest.TestCase):
    def test_round_trips_through_json(self):
        value = [{"provider": "openrouter", "model": "openrouter/auto"}]
        self.assertEqual(json.loads(cli._structured(value)), value)

    def test_keeps_the_yaml_mapping_trigger(self):
        # Hermes only parses a config value as structured when it looks like a
        # YAML mapping or list, so the separator has to stay ": ".
        self.assertIn(": ", cli._structured({"model": "syn:large:text"}))


class ApiKeyTest(unittest.TestCase):
    def test_prefers_the_hermes_dotenv_resolver(self):
        fake = types.ModuleType("agent.credential_pool")
        fake.get_env_prefer_dotenv = mock.Mock(return_value="sk-hermes")
        modules = mock.patch.dict(sys.modules, {"agent.credential_pool": fake})
        env = mock.patch.dict("os.environ", {"SYNTHETIC_API_KEY": "sk-shell"})
        with modules, env:
            self.assertEqual(credentials.api_key("SYNTHETIC_API_KEY"), "sk-hermes")

    def test_falls_back_to_the_environment(self):
        modules = mock.patch.dict(sys.modules, {"agent.credential_pool": None})
        env = mock.patch.dict("os.environ", {"SYNTHETIC_API_KEY": "sk-shell"})
        with modules, env:
            self.assertEqual(credentials.api_key("SYNTHETIC_API_KEY"), "sk-shell")

    def test_missing_key_is_an_empty_string(self):
        modules = mock.patch.dict(sys.modules, {"agent.credential_pool": None})
        with modules, mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(credentials.api_key("SYNTHETIC_API_KEY"), "")


if __name__ == "__main__":
    unittest.main()
