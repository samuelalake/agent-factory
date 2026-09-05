"""Idempotent consumer installer."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


REVIEW_CALLER = """name: agent-review
on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
permissions:
  contents: read
  pull-requests: write
jobs:
  review:
    uses: samuelalake/agent-factory/.github/workflows/review.yml@{factory_ref}
    with:
      pr: ${{{{ github.event.pull_request.number }}}}
      factory_ref: {factory_ref}
    secrets: inherit
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
        type: number
permissions:
  contents: read
  pull-requests: read
  issues: read
  statuses: write
jobs:
  gate:
    uses: samuelalake/agent-factory/.github/workflows/gate.yml@{factory_ref}
    with:
      pr: ${{{{ github.event.pull_request.number || inputs.pr }}}}
      factory_ref: {factory_ref}
    secrets: inherit
"""


def default_config(project_name: str) -> dict:
    return {
        "version": 1,
        "project": {"name": project_name, "context_files": ["AGENTS.md"]},
        "review": {
            "marker": "<!-- reviewer:agent-factory -->",
            "failure_marker": "<!-- reviewer:agent-factory-failure -->",
            "required": True,
            "max_diff_bytes": 200_000,
            "provider": "anthropic",
            "model": "claude-opus-5",
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
