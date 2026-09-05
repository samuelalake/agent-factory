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


@dataclass(frozen=True)
class ReviewConfig:
    marker: str
    failure_marker: str
    required: bool
    max_diff_bytes: int


@dataclass(frozen=True)
class GateConfig:
    context: str
    required_checks: tuple[str, ...]
    allow_followup_issues: bool


@dataclass(frozen=True)
class Config:
    version: int
    project: ProjectConfig
    review: ReviewConfig
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
    allowed = {"version", "project", "review", "gate"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(f"unknown top-level keys: {', '.join(unknown)}")
    version = raw.get("version")
    if version != 1:
        raise ConfigError(f"unsupported config version: {version!r}")

    project = _mapping(raw.get("project"), "project")
    review = _mapping(raw.get("review"), "review")
    gate = _mapping(raw.get("gate"), "gate")
    max_diff = review.get("max_diff_bytes", 200_000)
    if not isinstance(max_diff, int) or max_diff < 1:
        raise ConfigError("review.max_diff_bytes must be a positive integer")

    return Config(
        version=1,
        project=ProjectConfig(
            name=_string(project.get("name"), "project.name"),
            context_files=_strings(project.get("context_files", []), "project.context_files"),
        ),
        review=ReviewConfig(
            marker=_string(review.get("marker"), "review.marker"),
            failure_marker=_string(review.get("failure_marker"), "review.failure_marker"),
            required=bool(review.get("required", True)),
            max_diff_bytes=max_diff,
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
