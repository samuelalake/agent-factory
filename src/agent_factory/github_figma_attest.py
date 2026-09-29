"""Deterministic recovery receipt for a live Figma delivery.

This is an explicit operator recovery path for the narrow case where the hosted
Figma MCP call persisted its canvas mutation but never returned a terminal tool
result. It does not mutate Figma. It binds a checked-in manifest and delivery
record to the pull request's exact head using the Builder GitHub App identity.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any

from .config import load_config
from .github_builder import BuilderBlocked, _blocked_detail, _run
from .github_figma_writer import _gh, _upsert_issue_comment, format_issue_status


SAFE_RECORD_PREFIXES = ("docs/", ".agent-factory/figma-deliveries/")
NODE_RE = re.compile(r"^[0-9]+:[0-9]+$")
PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._/-]{0,79}$")


def _safe_repo_path(root: Path, value: str) -> Path:
    if not any(value.startswith(prefix) for prefix in SAFE_RECORD_PREFIXES):
        raise BuilderBlocked(
            "Figma attestation record must live under docs/ or "
            ".agent-factory/figma-deliveries/"
        )
    root_resolved = root.resolve()
    path = (root / value).resolve()
    if root_resolved not in path.parents or not path.is_file():
        raise BuilderBlocked("Figma attestation record is missing or outside the checkout")
    return path


def delivery_from_manifest(
    root: Path,
    *,
    record_path: str,
    screen_prefix: str,
) -> tuple[str, tuple[str, ...]]:
    if not PREFIX_RE.fullmatch(screen_prefix):
        raise BuilderBlocked("Figma attestation screen prefix is invalid")
    record = _safe_repo_path(root, record_path).read_text(encoding="utf-8")
    manifest_path = root / "tools/design-sync/manifest.json"
    if not manifest_path.is_file():
        raise BuilderBlocked("Figma attestation requires tools/design-sync/manifest.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuilderBlocked("Figma design-sync manifest is unreadable") from exc
    file_key = manifest.get("figmaFileKey")
    screens = manifest.get("screens")
    if not isinstance(file_key, str) or not re.fullmatch(r"[A-Za-z0-9]+", file_key):
        raise BuilderBlocked("Figma design-sync manifest has an invalid file key")
    if not isinstance(screens, list):
        raise BuilderBlocked("Figma design-sync manifest has no screens list")
    prefix = screen_prefix.casefold()
    nodes: list[str] = []
    for screen in screens:
        if not isinstance(screen, dict):
            continue
        name = screen.get("name")
        node = screen.get("node")
        if (
            isinstance(name, str)
            and name.casefold().startswith(prefix)
            and isinstance(node, str)
            and NODE_RE.fullmatch(node)
        ):
            nodes.append(node)
    nodes = list(dict.fromkeys(nodes))
    if not nodes:
        raise BuilderBlocked("Figma attestation found no manifest screens for the requested prefix")
    missing = [node for node in nodes if node not in record and node.replace(":", "-") not in record]
    if missing:
        raise BuilderBlocked("Figma delivery record does not cite every manifest node")
    file_url = f"https://www.figma.com/design/{file_key}"
    node_urls = tuple(f"{file_url}?node-id={node.replace(':', '-')}" for node in nodes)
    return file_url, node_urls


def run(
    repo: str,
    pr_number: str,
    issue_number: str,
    root: Path,
    config_path: Path,
    *,
    record_path: str,
    screen_prefix: str,
) -> str:
    if not os.environ.get("GH_TOKEN"):
        raise RuntimeError("GH_TOKEN must be a Builder App installation token")
    config = load_config(config_path)
    if not config.figma.enabled:
        raise RuntimeError("Figma Writer is not enabled")
    issue = json.loads(_gh([
        "issue", "view", issue_number, "--repo", repo, "--json", "number,state",
    ], cwd=root))
    pr = json.loads(_gh([
        "pr", "view", pr_number, "--repo", repo,
        "--json", "number,url,headRefName,headRefOid,isDraft",
    ], cwd=root))
    if str(issue.get("state") or "").upper() != "OPEN":
        raise RuntimeError("Figma attestation requires an open issue")
    expected_branch = f"{config.builder.branch_prefix}{issue_number}"
    if str(pr.get("headRefName") or "") != expected_branch:
        raise RuntimeError("Figma attestation received a pull request outside the Builder branch")
    if not bool(pr.get("isDraft")):
        raise RuntimeError("Figma attestation requires a draft pull request")
    head = str(pr.get("headRefOid") or "")
    if _run(["git", "rev-parse", "HEAD"], cwd=root).strip() != head:
        raise RuntimeError("Figma attestation checkout does not match the pull request head")
    file_url, node_urls = delivery_from_manifest(
        root,
        record_path=record_path,
        screen_prefix=screen_prefix,
    )
    detail = (
        "Operator-attested recovery: the live editable canvas was read back after the hosted MCP "
        "call persisted its mutation but did not return a terminal result. "
        + " ".join(node_urls)
    )
    body = format_issue_status(
        config.figma.marker,
        issue_number,
        "delivered",
        detail,
        str(pr["url"]),
        head=head,
        metadata={
            "attestation": "operator_verified_manifest",
            "record_path": record_path,
            "screen_prefix": screen_prefix,
            "file_url": file_url,
        },
    )
    _upsert_issue_comment(repo, issue_number, config.figma.marker, body, root=root)
    print(pr["url"])
    return str(pr["url"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True)
    parser.add_argument("--issue", required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--record-path", required=True)
    parser.add_argument("--screen-prefix", required=True)
    args = parser.parse_args()
    try:
        run(
            args.repo,
            args.pr,
            args.issue,
            args.root,
            args.config,
            record_path=args.record_path,
            screen_prefix=args.screen_prefix,
        )
    except (BuilderBlocked, RuntimeError, subprocess.TimeoutExpired) as exc:
        detail = _blocked_detail(str(exc), "deterministic-figma-attestation", None)
        print(f"blocked: {detail}", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
