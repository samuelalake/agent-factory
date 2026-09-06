from __future__ import annotations

import unittest

from agent_factory.config import ConfigError, parse_config
from agent_factory.cli import default_config


class ConfigTests(unittest.TestCase):
    def test_default_round_trip(self) -> None:
        config = parse_config(default_config("demo"))
        self.assertEqual(config.version, 1)
        self.assertEqual(config.project.name, "demo")
        self.assertEqual(config.gate.context, "agent-factory")
        self.assertEqual(config.steward.dispatch_label, "agent:builder")
        self.assertEqual(config.steward.retry_label, "agent:retry")
        self.assertEqual(config.builder.harness, "gemini-cli")
        self.assertEqual(config.builder.provider, "gemini")
        self.assertEqual(config.builder.cli_version, "0.55.1")
        self.assertEqual(config.builder.max_model_cost_usd, 3.0)
        self.assertEqual(config.integration.mode, "pull_request_merge_ref")
        self.assertEqual(config.review.provider, "gemini")
        self.assertEqual(config.review.fallback_provider, "nvidia")

    def test_supported_provider_is_configurable(self) -> None:
        raw = default_config("demo")
        raw["review"]["provider"] = "gemini"
        raw["review"]["model"] = "gemini-3.5-flash"
        config = parse_config(raw)
        self.assertEqual((config.review.provider, config.review.model), ("gemini", "gemini-3.5-flash"))

    def test_fallback_pair_is_required(self) -> None:
        raw = default_config("demo")
        raw["review"].pop("fallback_model")
        with self.assertRaisesRegex(ConfigError, "must be set together"):
            parse_config(raw)

    def test_builder_limits_are_validated(self) -> None:
        raw = default_config("demo")
        raw["builder"]["timeout_seconds"] = 1
        with self.assertRaisesRegex(ConfigError, "at least 60"):
            parse_config(raw)

    def test_minimax_builder_and_costs_are_configurable(self) -> None:
        raw = default_config("demo")
        raw["builder"].update(
            {
                "provider": "minimax",
                "harness": "openai-compatible",
                "model": "MiniMax-M2.7",
                "max_model_cost_usd": 2.5,
                "input_cost_per_million": 0.3,
                "output_cost_per_million": 1.2,
            }
        )
        config = parse_config(raw)
        self.assertEqual(config.builder.provider, "minimax")
        self.assertEqual(config.builder.input_cost_per_million, 0.3)

    def test_builder_cost_limit_must_be_positive(self) -> None:
        raw = default_config("demo")
        raw["builder"]["max_model_cost_usd"] = 0
        with self.assertRaisesRegex(ConfigError, "greater than zero"):
            parse_config(raw)

    def test_unknown_integration_mode_fails(self) -> None:
        raw = default_config("demo")
        raw["integration"]["mode"] = "mystery"
        with self.assertRaisesRegex(ConfigError, "integration.mode"):
            parse_config(raw)

    def test_unknown_provider_fails(self) -> None:
        raw = default_config("demo")
        raw["review"]["provider"] = "mystery"
        with self.assertRaisesRegex(ConfigError, "unsupported review.provider"):
            parse_config(raw)

    def test_unknown_top_level_key_fails(self) -> None:
        raw = default_config("demo")
        raw["surprise"] = True
        with self.assertRaisesRegex(ConfigError, "unknown top-level"):
            parse_config(raw)

    def test_invalid_version_fails(self) -> None:
        raw = default_config("demo")
        raw["version"] = 2
        with self.assertRaisesRegex(ConfigError, "unsupported config version"):
            parse_config(raw)
