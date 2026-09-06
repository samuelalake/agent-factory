"""Versioned consumer configuration with strict, useful validation."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ProjectConfig:
    name: str
    context_files: tuple[str, ...]
    skill_dirs: tuple[str, ...]
    max_skills: int


@dataclass(frozen=True)
class ReviewConfig:
    marker: str
    failure_marker: str
    required: bool
    max_diff_bytes: int
    provider: str
    model: str
    fallback_provider: str | None
    fallback_model: str | None


@dataclass(frozen=True)
class StewardConfig:
    marker: str
    ready_labels: tuple[str, ...]
    dispatch_label: str
    retry_label: str


@dataclass(frozen=True)
class BuilderConfig:
    marker: str
    provider: str
    harness: str
    model: str
    cli_version: str
    timeout_seconds: int
    branch_prefix: str
    base_branch: str
    runner: str
    fallback_provider: str | None
    fallback_model: str | None
    max_model_requests: int
    max_model_cost_usd: float
    input_cost_per_million: float
    output_cost_per_million: float


@dataclass(frozen=True)
class IntegrationConfig:
    marker: str
    status_context: str
    environment: str
    mode: str
    automatic_promotion: bool


@dataclass(frozen=True)
class GateConfig:
    context: str
    required_checks: tuple[str, ...]
    allow_followup_issues: bool


@dataclass(frozen=True)
class Config:
    version: int
    project: ProjectConfig
    steward: StewardConfig
    builder: BuilderConfig
    review: ReviewConfig
    integration: IntegrationConfig
    gate: GateConfig


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{path} must be an object")
    return value


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{path} must be a non-empty string")
    return value.strip()


def _strings(value: Any, path: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
        raise ConfigError(f"{path} must be an array of non-empty strings")
    return tuple(value)


def parse_config(raw: dict[str, Any]) -> Config:
    allowed = {"version", "project", "steward", "builder", "review", "integration", "gate"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(f"unknown top-level keys: {', '.join(unknown)}")
    version = raw.get("version")
    if version != 1:
        raise ConfigError(f"unsupported config version: {version!r}")

    project = _mapping(raw.get("project"), "project")
    steward = _mapping(raw.get("steward", {}), "steward")
    builder = _mapping(raw.get("builder", {}), "builder")
    review = _mapping(raw.get("review"), "review")
    integration = _mapping(raw.get("integration", {}), "integration")
    gate = _mapping(raw.get("gate"), "gate")
    max_diff = review.get("max_diff_bytes", 200_000)
    if not isinstance(max_diff, int) or max_diff < 1:
        raise ConfigError("review.max_diff_bytes must be a positive integer")
    provider = _string(review.get("provider", "anthropic"), "review.provider").lower()
    supported_providers = {"anthropic", "gemini", "nvidia", "openrouter"}
    if provider not in supported_providers:
        raise ConfigError(f"unsupported review.provider: {provider}")
    fallback_provider_value = review.get("fallback_provider")
    fallback_model_value = review.get("fallback_model")
    if (fallback_provider_value is None) != (fallback_model_value is None):
        raise ConfigError("review.fallback_provider and review.fallback_model must be set together")
    fallback_provider = None
    fallback_model = None
    if fallback_provider_value is not None:
        fallback_provider = _string(fallback_provider_value, "review.fallback_provider").lower()
        if fallback_provider not in supported_providers:
            raise ConfigError(f"unsupported review.fallback_provider: {fallback_provider}")
        fallback_model = _string(fallback_model_value, "review.fallback_model")
    max_skills = project.get("max_skills", 3)
    if not isinstance(max_skills, int) or max_skills < 0:
        raise ConfigError("project.max_skills must be a non-negative integer")
    timeout_seconds = builder.get("timeout_seconds", 1800)
    if not isinstance(timeout_seconds, int) or timeout_seconds < 60:
        raise ConfigError("builder.timeout_seconds must be an integer of at least 60")
    max_model_requests = builder.get("max_model_requests", 40)
    if not isinstance(max_model_requests, int) or max_model_requests < 1:
        raise ConfigError("builder.max_model_requests must be a positive integer")
    max_model_cost_usd = builder.get("max_model_cost_usd", 3.0)
    input_cost_per_million = builder.get("input_cost_per_million", 0.0)
    output_cost_per_million = builder.get("output_cost_per_million", 0.0)
    for value, path in (
        (max_model_cost_usd, "builder.max_model_cost_usd"),
        (input_cost_per_million, "builder.input_cost_per_million"),
        (output_cost_per_million, "builder.output_cost_per_million"),
    ):
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            raise ConfigError(f"{path} must be a non-negative number")
    if max_model_cost_usd <= 0:
        raise ConfigError("builder.max_model_cost_usd must be greater than zero")
    builder_provider = _string(builder.get("provider", "gemini"), "builder.provider").lower()
    if builder_provider not in {"gemini", "minimax", "nvidia", "openrouter"}:
        raise ConfigError(f"unsupported builder.provider: {builder_provider}")
    builder_harness = _string(builder.get("harness", "gemini-cli"), "builder.harness")
    expected_harness = "gemini-cli" if builder_provider == "gemini" else "openai-compatible"
    if builder_harness != expected_harness:
        raise ConfigError(
            f"builder.harness must be {expected_harness!r} for provider {builder_provider!r}"
        )
    builder_fallback_provider = builder.get("fallback_provider", "nvidia")
    builder_fallback_model = builder.get("fallback_model", "moonshotai/kimi-k3")
    if (builder_fallback_provider is None) != (builder_fallback_model is None):
        raise ConfigError("builder.fallback_provider and builder.fallback_model must be set together")
    if builder_fallback_provider is not None:
        builder_fallback_provider = _string(builder_fallback_provider, "builder.fallback_provider").lower()
        if builder_fallback_provider not in {"minimax", "nvidia", "openrouter"}:
            raise ConfigError(f"unsupported builder.fallback_provider: {builder_fallback_provider}")
        builder_fallback_model = _string(builder_fallback_model, "builder.fallback_model")
    integration_mode = _string(
        integration.get("mode", "pull_request_merge_ref"), "integration.mode"
    )
    if integration_mode not in {"pull_request_merge_ref", "branch"}:
        raise ConfigError(f"unsupported integration.mode: {integration_mode}")

    return Config(
        version=1,
        project=ProjectConfig(
            name=_string(project.get("name"), "project.name"),
            context_files=_strings(project.get("context_files", []), "project.context_files"),
            skill_dirs=_strings(project.get("skill_dirs", ["skills", "skill"]), "project.skill_dirs"),
            max_skills=max_skills,
        ),
        steward=StewardConfig(
            marker=_string(
                steward.get("marker", "<!-- steward:agent-factory -->"), "steward.marker"
            ),
            ready_labels=_strings(steward.get("ready_labels", ["ready"]), "steward.ready_labels"),
            dispatch_label=_string(
                steward.get("dispatch_label", "agent:builder"), "steward.dispatch_label"
            ),
            retry_label=_string(
                steward.get("retry_label", "agent:retry"), "steward.retry_label"
            ),
        ),
        builder=BuilderConfig(
            marker=_string(
                builder.get("marker", "<!-- builder:agent-factory -->"), "builder.marker"
            ),
            provider=builder_provider,
            harness=builder_harness,
            model=_string(builder.get("model", "gemini-3.6-flash"), "builder.model"),
            cli_version=_string(builder.get("cli_version", "0.55.1"), "builder.cli_version"),
            timeout_seconds=timeout_seconds,
            branch_prefix=_string(
                builder.get("branch_prefix", "agent-factory/issue-"), "builder.branch_prefix"
            ),
            base_branch=_string(builder.get("base_branch", "main"), "builder.base_branch"),
            runner=_string(builder.get("runner", "ubuntu-latest"), "builder.runner"),
            fallback_provider=builder_fallback_provider,
            fallback_model=builder_fallback_model,
            max_model_requests=max_model_requests,
            max_model_cost_usd=float(max_model_cost_usd),
            input_cost_per_million=float(input_cost_per_million),
            output_cost_per_million=float(output_cost_per_million),
        ),
        review=ReviewConfig(
            marker=_string(review.get("marker"), "review.marker"),
            failure_marker=_string(review.get("failure_marker"), "review.failure_marker"),
            required=bool(review.get("required", True)),
            max_diff_bytes=max_diff,
            provider=provider,
            model=_string(review.get("model", "claude-opus-5"), "review.model"),
            fallback_provider=fallback_provider,
            fallback_model=fallback_model,
        ),
        integration=IntegrationConfig(
            marker=_string(
                integration.get("marker", "<!-- steward:agent-factory-integration -->"),
                "integration.marker",
            ),
            status_context=_string(
                integration.get("status_context", "agent-factory/integration"),
                "integration.status_context",
            ),
            environment=_string(
                integration.get("environment", "development"), "integration.environment"
            ),
            mode=integration_mode,
            automatic_promotion=bool(integration.get("automatic_promotion", True)),
        ),
        gate=GateConfig(
            context=_string(gate.get("context", "agent-factory"), "gate.context"),
            required_checks=_strings(gate.get("required_checks", []), "gate.required_checks"),
            allow_followup_issues=bool(gate.get("allow_followup_issues", True)),
        ),
    )


def load_config(path: str | Path) -> Config:
    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot load {source}: {exc}") from exc
    return parse_config(_mapping(raw, "root"))
