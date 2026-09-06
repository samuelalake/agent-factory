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
