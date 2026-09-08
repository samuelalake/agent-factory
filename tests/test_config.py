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
        self.assertEqual(config.builder.max_output_tokens, 4096)
        self.assertEqual(config.builder.max_revision_attempts, 3)
        self.assertFalse(config.builder.visual_revision_context)
        self.assertFalse(config.builder.fallback_visual_revision_context)
        self.assertEqual(config.integration.mode, "pull_request_merge_ref")
        self.assertEqual(config.review.provider, "gemini")
        self.assertEqual(config.review.app_login, "agent-factory-reviewer[bot]")
        self.assertEqual(config.review.fallback_provider, "nvidia")
        self.assertFalse(config.review.visual_evidence)
        self.assertFalse(config.review.fallback_visual_evidence)
        self.assertFalse(config.review.require_builder_delivery)
        self.assertEqual(config.review.delivery_wait_seconds, 1800)

    def test_supported_provider_is_configurable(self) -> None:
        raw = default_config("demo")
        raw["review"]["provider"] = "gemini"
        raw["review"]["model"] = "gemini-3.5-flash"
        config = parse_config(raw)
        self.assertEqual((config.review.provider, config.review.model), ("gemini", "gemini-3.5-flash"))

    def test_reviewer_app_login_is_configurable_and_required(self) -> None:
        raw = default_config("demo")
        raw["review"]["app_login"] = "acme-reviewer[bot]"
        self.assertEqual(parse_config(raw).review.app_login, "acme-reviewer[bot]")
        raw["review"].pop("app_login")
        with self.assertRaisesRegex(ConfigError, "review.app_login"):
            parse_config(raw)

    def test_fallback_pair_is_required(self) -> None:
        raw = default_config("demo")
        raw["review"].pop("fallback_model")
        with self.assertRaisesRegex(ConfigError, "must be set together"):
            parse_config(raw)

    def test_reviewer_visual_capability_is_explicit_per_route(self) -> None:
        raw = default_config("demo")
        raw["review"]["visual_evidence"] = True
        raw["review"]["fallback_visual_evidence"] = True
        config = parse_config(raw)
        self.assertTrue(config.review.visual_evidence)
        self.assertTrue(config.review.fallback_visual_evidence)

        raw["review"]["fallback_provider"] = None
        raw["review"]["fallback_model"] = None
        with self.assertRaisesRegex(ConfigError, "fallback provider"):
            parse_config(raw)

    def test_steward_fallback_pair_is_required(self) -> None:
        raw = default_config("demo")
        raw["steward"]["fallback_model"] = None
        with self.assertRaisesRegex(ConfigError, "steward.fallback_provider"):
            parse_config(raw)

    def test_minimax_is_configurable_for_text_roles(self) -> None:
        raw = default_config("demo")
        raw["review"]["provider"] = "minimax"
        raw["review"]["model"] = "MiniMax-M2.7"
        raw["steward"]["fallback_provider"] = "minimax"
        raw["steward"]["fallback_model"] = "MiniMax-M2.7"
        config = parse_config(raw)
        self.assertEqual(config.review.provider, "minimax")
        self.assertEqual(config.steward.fallback_provider, "minimax")

    def test_steward_split_limit_is_bounded(self) -> None:
        raw = default_config("demo")
        raw["steward"]["max_subtasks"] = -1
        with self.assertRaisesRegex(ConfigError, "steward.max_subtasks"):
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

    def test_visual_revision_context_requires_compatible_harness(self) -> None:
        raw = default_config("demo")
        raw["builder"]["visual_revision_context"] = True
        with self.assertRaisesRegex(ConfigError, "openai-compatible"):
            parse_config(raw)
        raw["builder"].update(
            {
                "provider": "openrouter",
                "harness": "openai-compatible",
                "model": "vision-tool-model",
            }
        )
        self.assertTrue(parse_config(raw).builder.visual_revision_context)
        raw["builder"]["fallback_visual_revision_context"] = True
        self.assertTrue(parse_config(raw).builder.fallback_visual_revision_context)

    def test_visual_fallback_cannot_be_enabled_without_visual_primary(self) -> None:
        raw = default_config("demo")
        raw["builder"]["fallback_visual_revision_context"] = True
        with self.assertRaisesRegex(ConfigError, "requires visual_revision_context"):
            parse_config(raw)

    def test_builder_cost_limit_must_be_positive(self) -> None:
        raw = default_config("demo")
        raw["builder"]["max_model_cost_usd"] = 0
        with self.assertRaisesRegex(ConfigError, "greater than zero"):
            parse_config(raw)

    def test_builder_revision_limit_must_be_positive(self) -> None:
        raw = default_config("demo")
        raw["builder"]["max_revision_attempts"] = 0
        with self.assertRaisesRegex(ConfigError, "max_revision_attempts"):
            parse_config(raw)

    def test_builder_output_limit_is_bounded(self) -> None:
        raw = default_config("demo")
        raw["builder"]["max_output_tokens"] = 2048
        self.assertEqual(parse_config(raw).builder.max_output_tokens, 2048)
        raw["builder"]["max_output_tokens"] = 0
        with self.assertRaisesRegex(ConfigError, "max_output_tokens"):
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

    def test_delivery_review_contract_is_validated(self) -> None:
        raw = default_config("demo")
        raw["review"]["require_builder_delivery"] = True
        raw["review"]["delivery_wait_seconds"] = 1200
        config = parse_config(raw)
        self.assertTrue(config.review.require_builder_delivery)
        self.assertEqual(config.review.delivery_wait_seconds, 1200)

        raw["review"]["delivery_wait_seconds"] = -1
        with self.assertRaisesRegex(ConfigError, "delivery_wait_seconds"):
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
