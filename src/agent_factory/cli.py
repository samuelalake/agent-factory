"""Idempotent consumer installer."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


REVIEW_CALLER = """name: agent-review
on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
  workflow_dispatch:
    inputs:
      pr:
        description: Pull request number to review again
        required: true
        type: string
permissions:
  actions: read
  checks: read
  contents: write
  issues: write
  pull-requests: write
  statuses: write
jobs:
  review:
    if: github.event_name == 'workflow_dispatch' || github.event.pull_request.head.repo.fork == false
    uses: samuelalake/agent-factory/.github/workflows/review.yml@{factory_ref}
    with:
      pr: ${{{{ format('{{0}}', github.event.pull_request.number || inputs.pr) }}}}
      factory_ref: {factory_ref}
    secrets:
      GEMINI_API_KEY: ${{{{ secrets.GEMINI_API_KEY }}}}
      NVIDIA_API_KEY: ${{{{ secrets.NVIDIA_API_KEY }}}}
      AGENT_FACTORY_REVIEWER_APP_ID: ${{{{ secrets.AGENT_FACTORY_REVIEWER_APP_ID }}}}
      AGENT_FACTORY_REVIEWER_APP_PRIVATE_KEY: ${{{{ secrets.AGENT_FACTORY_REVIEWER_APP_PRIVATE_KEY }}}}
  integration:
    needs: review
    uses: samuelalake/agent-factory/.github/workflows/integration.yml@{factory_ref}
    with:
      pr: ${{{{ format('{{0}}', github.event.pull_request.number || inputs.pr) }}}}
      factory_ref: {factory_ref}
      timeout_seconds: 1800
    secrets:
      AGENT_FACTORY_STEWARD_APP_ID: ${{{{ secrets.AGENT_FACTORY_STEWARD_APP_ID }}}}
      AGENT_FACTORY_STEWARD_APP_PRIVATE_KEY: ${{{{ secrets.AGENT_FACTORY_STEWARD_APP_PRIVATE_KEY }}}}
"""

STEWARD_CALLER = """name: agent-steward
on:
  issues:
    types: [labeled]
  workflow_dispatch:
    inputs:
      issue:
        description: Issue number
        required: true
        type: string
permissions:
  contents: read
jobs:
  steward:
    if: github.event_name == 'workflow_dispatch' || github.event.label.name == 'ready' || github.event.label.name == 'agent:steward' || github.event.label.name == 'agent:retry'
    uses: samuelalake/agent-factory/.github/workflows/steward.yml@{factory_ref}
    with:
      issue: ${{{{ format('{{0}}', github.event.issue.number || inputs.issue) }}}}
      factory_ref: {factory_ref}
    secrets:
      AGENT_FACTORY_STEWARD_APP_ID: ${{{{ secrets.AGENT_FACTORY_STEWARD_APP_ID }}}}
      AGENT_FACTORY_STEWARD_APP_PRIVATE_KEY: ${{{{ secrets.AGENT_FACTORY_STEWARD_APP_PRIVATE_KEY }}}}
"""

BUILDER_CALLER = """name: agent-builder
on:
  issues:
    types: [labeled]
  workflow_dispatch:
    inputs:
      issue:
        description: Issue number
        required: true
        type: string
permissions:
  contents: read
jobs:
  builder:
    if: github.event_name == 'workflow_dispatch' || github.event.label.name == 'agent:builder'
    uses: samuelalake/agent-factory/.github/workflows/builder.yml@{factory_ref}
    with:
      issue: ${{{{ format('{{0}}', github.event.issue.number || inputs.issue) }}}}
      factory_ref: {factory_ref}
      runner: ubuntu-latest
      base_ref: main
      gemini_cli_version: 0.55.1
    secrets:
      GEMINI_API_KEY: ${{{{ secrets.GEMINI_API_KEY }}}}
      MINIMAX_API_KEY: ${{{{ secrets.MINIMAX_API_KEY }}}}
      NVIDIA_API_KEY: ${{{{ secrets.NVIDIA_API_KEY }}}}
      OPENROUTER_API_KEY: ${{{{ secrets.OPENROUTER_API_KEY }}}}
      AGENT_FACTORY_BUILDER_APP_ID: ${{{{ secrets.AGENT_FACTORY_BUILDER_APP_ID }}}}
      AGENT_FACTORY_BUILDER_APP_PRIVATE_KEY: ${{{{ secrets.AGENT_FACTORY_BUILDER_APP_PRIVATE_KEY }}}}
"""

GATE_CALLER = """name: agent-gate
on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review, edited]
  pull_request_review:
    types: [submitted, edited, dismissed]
  workflow_dispatch:
    inputs:
      pr:
        description: Pull request number
        required: true
        type: string
permissions:
  contents: read
  pull-requests: read
  issues: read
  statuses: write
jobs:
  gate:
    uses: samuelalake/agent-factory/.github/workflows/gate.yml@{factory_ref}
    with:
      pr: ${{{{ format('{{0}}', github.event.pull_request.number || inputs.pr) }}}}
      factory_ref: {factory_ref}
    secrets: inherit
"""


def default_config(project_name: str) -> dict:
    return {
        "version": 1,
        "project": {
            "name": project_name,
            "context_files": ["AGENTS.md"],
            "skill_dirs": ["skills", "skill"],
            "max_skills": 3,
        },
        "steward": {
            "marker": "<!-- steward:agent-factory -->",
            "ready_labels": ["ready"],
            "dispatch_label": "agent:builder",
            "retry_label": "agent:retry",
        },
        "builder": {
            "marker": "<!-- builder:agent-factory -->",
            "provider": "gemini",
            "harness": "gemini-cli",
            "model": "gemini-3.6-flash",
            "cli_version": "0.55.1",
            "timeout_seconds": 1800,
            "branch_prefix": "agent-factory/issue-",
            "base_branch": "main",
            "runner": "ubuntu-latest",
            "fallback_provider": "nvidia",
            "fallback_model": "moonshotai/kimi-k3",
            "max_model_requests": 40,
            "max_model_cost_usd": 3.0,
            "input_cost_per_million": 0.0,
            "output_cost_per_million": 0.0,
        },
        "review": {
            "marker": "<!-- reviewer:agent-factory -->",
            "failure_marker": "<!-- reviewer:agent-factory-failure -->",
            "required": True,
            "max_diff_bytes": 200_000,
            "provider": "gemini",
            "model": "gemini-3.6-flash",
            "fallback_provider": "nvidia",
            "fallback_model": "moonshotai/kimi-k3",
        },
        "integration": {
            "marker": "<!-- steward:agent-factory-integration -->",
            "status_context": "agent-factory/integration",
            "environment": "development",
            "mode": "pull_request_merge_ref",
            "automatic_promotion": True,
        },
        "gate": {
            "context": "agent-factory",
            "required_checks": [],
            "allow_followup_issues": True,
        },
    }


def _write(path: Path, content: str, *, force: bool) -> str:
    if path.exists() and not force:
        return "preserved"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return "written"


def install(root: Path, *, factory_ref: str, force: bool) -> dict[str, str]:
    root = root.resolve()
    if not (root / ".git").exists():
        raise SystemExit(f"not a Git repository: {root}")
    outcomes = {
        ".agent-factory/config.json": _write(
            root / ".agent-factory/config.json",
            json.dumps(default_config(root.name), indent=2) + "\n",
            force=force,
        ),
        ".github/workflows/agent-review.yml": _write(
            root / ".github/workflows/agent-review.yml",
            REVIEW_CALLER.format(factory_ref=factory_ref),
            force=force,
        ),
        ".github/workflows/agent-steward.yml": _write(
            root / ".github/workflows/agent-steward.yml",
            STEWARD_CALLER.format(factory_ref=factory_ref),
            force=force,
        ),
        ".github/workflows/agent-builder.yml": _write(
            root / ".github/workflows/agent-builder.yml",
            BUILDER_CALLER.format(factory_ref=factory_ref),
            force=force,
        ),
        ".github/workflows/agent-gate.yml": _write(
            root / ".github/workflows/agent-gate.yml",
            GATE_CALLER.format(factory_ref=factory_ref),
            force=force,
        ),
    }
    return outcomes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-factory")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="install thin caller workflows")
    init.add_argument("root", type=Path)
    init.add_argument("--factory-ref", default="main")
    init.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    outcomes = install(args.root, factory_ref=args.factory_ref, force=args.force)
    for path, outcome in outcomes.items():
        print(f"{outcome}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
