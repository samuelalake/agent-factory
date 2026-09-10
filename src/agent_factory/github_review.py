"""Generic current-head reviewer adapter for reusable workflows."""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .app_auth import get_installation_token
from .config import load_config
from .context import discover_context
from .github_delivery import (
    authenticated_delivery_history,
    delivery_status,
    wait_for_delivery,
)
from .github_builder import (
    BuilderBlocked,
    _current_delivery_media,
    _delivery_provenance,
    _fetch_delivery_images,
)
from .model import ModelError, complete
from .protocol import decode_data, encode_data, extract_json_reply


ARBITRATION_MARKER = "<!-- steward:agent-factory-evidence-arbitration -->"


def _gh(args: list[str], *, stdin: str | None = None) -> str:
    result = subprocess.run(["gh", *args], input=stdin, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout


def repository_glob_match(path: str, pattern: str) -> bool:
    """Match repository paths while allowing ``**/`` to span zero directories."""
    candidates = {pattern}
    pending = [pattern]
    while pending:
        candidate = pending.pop()
        start = candidate.find("**/")
        if start < 0:
            continue
        collapsed = candidate[:start] + candidate[start + 3:]
        if collapsed not in candidates:
            candidates.add(collapsed)
            pending.append(collapsed)
    return any(fnmatch.fnmatchcase(path, candidate) for candidate in candidates)


def changed_file_paths(repo: str, pr: str) -> tuple[str, ...]:
    """Fetch every current and previous PR file path through paginated REST."""
    raw = json.loads(_gh([
        "api", f"repos/{repo}/pulls/{pr}/files?per_page=100", "--paginate", "--slurp",
    ]))
    if not isinstance(raw, list) or any(not isinstance(page, list) for page in raw):
        raise RuntimeError("GitHub returned an invalid paginated pull-file response")
    paths: list[str] = []
    for page in raw:
        for item in page:
            if not isinstance(item, dict):
                raise RuntimeError("GitHub returned an invalid pull-file entry")
            for key in ("filename", "previous_filename"):
                value = item.get(key)
                if isinstance(value, str) and value and value not in paths:
                    paths.append(value)
    return tuple(paths)


def normalize_review(
    raw: dict[str, Any], *, allow_evidence_conflict: bool = False
) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    for item in raw.get("findings") or []:
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity") or "").upper()
        if severity not in {"P1", "P2", "P3"}:
            continue
        path = str(item.get("file") or "").strip()
        line = item.get("line")
        line = line if type(line) is int and line > 0 else None
        location = path + (f":{line}" if path and line is not None else "")
        findings.append({
            "severity": severity,
            "key": location or "review-wide",
            "path": path,
            "line": line,
            "title": str(item.get("title") or "untitled finding").strip(),
            "reasoning": str(item.get("reasoning") or "").strip(),
            "suggestion": str(item.get("suggestion") or "").strip(),
        })
    if allow_evidence_conflict and raw.get("evidence_interpretation_conflict") is True:
        findings.insert(0, {
            "severity": "P1",
            "key": "agent-factory://evidence-interpretation-conflict",
            "path": "",
            "line": None,
            "title": "Evidence interpretation conflict",
            "reasoning": (
                "The same authenticated reference digest would receive materially conflicting "
                "visual guidance across Reviewer runs."
            ),
            "suggestion": (
                "Steward must resolve the evidence interpretation before Builder changes code."
            ),
        })
    approve = bool(raw.get("approve")) and not any(f["severity"] == "P1" for f in findings)
    return {"summary": str(raw.get("summary") or "").strip(), "approve": approve, "findings": findings}


def changed_between_heads(repo: str, before: str, after: str) -> tuple[str, ...]:
    """Return files changed between two delivered heads."""
    raw = json.loads(_gh(["api", f"repos/{repo}/compare/{before}...{after}"]))
    files = raw.get("files") if isinstance(raw, dict) else None
    if not isinstance(files, list):
        raise RuntimeError("GitHub returned an invalid compare response")
    if len(files) >= 300:
        raise RuntimeError(
            "GitHub compare reached its 300-file evidence limit; split the revision before review"
        )
    return tuple(
        str(item["filename"])
        for item in files
        if isinstance(item, dict) and isinstance(item.get("filename"), str)
    )


def evidence_digests(
    manifest: dict[str, dict[str, str]], role: str
) -> tuple[str, ...]:
    return tuple(
        item["sha256"] for item in manifest.values() if item.get("role") == role
    )


def evidence_output_digests(
    manifest: dict[str, dict[str, str]],
) -> tuple[tuple[str, str, str], ...]:
    selected = [
        item for item in manifest.values()
        if item.get("role") in {"render", "diff", "recording"}
    ]
    roles = [item["role"] for item in selected]
    single_pattern = len(roles) == len(set(roles))
    return tuple(
        (item["role"], "" if single_pattern else item["scope"], item["sha256"])
        for item in selected
    )


def stale_visual_evidence_review(
    prior_head: str,
    current_head: str,
    prior_manifest: dict[str, dict[str, str]],
    current_manifest: dict[str, dict[str, str]],
    changed_paths: tuple[str, ...],
) -> dict[str, Any] | None:
    """Fail closed when visual source changes but its rendered bytes do not."""
    prior_render = evidence_digests(prior_manifest, "render")
    current_render = evidence_digests(current_manifest, "render")
    prior_outputs = evidence_output_digests(prior_manifest)
    current_outputs = evidence_output_digests(current_manifest)
    if (
        not prior_render
        or prior_render != current_render
        or not prior_outputs
        or prior_outputs != current_outputs
        or not changed_paths
    ):
        return None
    review = normalize_review({
        "approve": False,
        "summary": "Authenticated current-head visual evidence appears stale or targets the wrong build.",
        "findings": [{
            "severity": "P1",
            "key": "agent-factory://evidence-stale-or-wrong-target",
            "title": "Visual implementation changed but rendered evidence did not",
            "reasoning": (
                f"Visual source changed from {prior_head[:7]} to {current_head[:7]} "
                f"({', '.join(changed_paths[:5])}), but Builder published the same render "
                f"SHA-256 digest{'s' if len(current_render) != 1 else ''}: "
                f"{', '.join(current_render)}. Every generated render, diff, and recording "
                "digest also remained unchanged."
            ),
            "suggestion": (
                "Steward should inspect the runner checkout, build target, cache, and capture path. "
                "Do not route another implementation revision to Builder until fresh evidence is proven."
            ),
        }],
    })
    review["findings"][0]["key"] = "agent-factory://evidence-stale-or-wrong-target"
    return review


def prior_review_for_head(
    repo: str,
    pr: str,
    head: str,
    marker: str,
    reviewer_app_login: str,
) -> str:
    """Return the latest non-dismissed authenticated Reviewer narrative for a prior head."""
    raw = json.loads(_gh([
        "api", f"repos/{repo}/pulls/{pr}/reviews?per_page=100", "--paginate", "--slurp",
    ]))
    pages = raw if isinstance(raw, list) and raw and isinstance(raw[0], list) else [raw]
    matches: list[str] = []
    for page in pages:
        if not isinstance(page, list):
            continue
        for review in page:
            if not isinstance(review, dict) or str(review.get("state") or "").upper() == "DISMISSED":
                continue
            user = review.get("user") or {}
            if not (
                isinstance(user, dict)
                and user.get("type") == "Bot"
                and user.get("login") == reviewer_app_login
            ):
                continue
            body = str(review.get("body") or "")
            data = decode_data(body)
            if marker in body and isinstance(data, dict) and data.get("head_sha") == head:
                matches.append(body)
    return matches[-1][-12000:] if matches else ""


def authenticated_arbitration_for_head(
    comments: list[Any], *, repo: str, pr: int, head: str,
    reference_digests: tuple[str, ...], steward_app_login: str
) -> str:
    """Return only a same-head, same-reference ruling from the configured Steward App."""
    pages = comments if comments and isinstance(comments[0], list) else [comments]
    matches: list[dict[str, Any]] = []
    for page in pages:
        if not isinstance(page, list):
            continue
        for comment in page:
            if not isinstance(comment, dict):
                continue
            user = comment.get("user") or {}
            body = str(comment.get("body") or "")
            data = decode_data(body)
            if not (
                isinstance(user, dict)
                and user.get("type") == "Bot"
                and str(user.get("login") or "").removesuffix("[bot]")
                == steward_app_login.removesuffix("[bot]")
                and ARBITRATION_MARKER in body
                and isinstance(data, dict)
                and data.get("role") == "steward"
                and data.get("kind") == "evidence_arbitration"
                and data.get("repo") == repo
                and data.get("pr") == pr
                and data.get("head_sha") == head
                and tuple(data.get("reference_digests") or ()) == reference_digests
                and data.get("resolved") is True
            ):
                continue
            matches.append(data)
    if not matches:
        return ""
    canonical = {
        json.dumps(value, sort_keys=True, separators=(",", ":")) for value in matches
    }
    if len(canonical) != 1:
        return ""
    selected = matches[-1]
    observations = selected.get("observations") or []
    if not isinstance(observations, list) or any(not isinstance(item, str) for item in observations):
        return ""
    interpretation = str(selected.get("authoritative_interpretation") or "").strip()
    if not interpretation:
        return ""
    facts = "\n".join(f"- {item[:500]}" for item in observations[:12])
    return (
        f"Steward ruling for `{head[:7]}` and reference "
        f"`{', '.join(reference_digests)}`:\n\n{facts}\n\n"
        f"Authoritative interpretation: {interpretation[:2000]}"
    )


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def diff_right_lines(diff: str) -> dict[str, set[int]]:
    """Return RIGHT-side line numbers GitHub accepts for each file in a diff."""
    lines: dict[str, set[int]] = {}
    path: str | None = None
    right_line: int | None = None
    for raw_line in diff.splitlines():
        if raw_line.startswith("diff --git "):
            path = None
            right_line = None
            continue
        if raw_line.startswith("+++ "):
            candidate = raw_line[4:].strip()
            path = None if candidate == "/dev/null" else candidate.removeprefix("b/")
            if path is not None:
                lines.setdefault(path, set())
            continue
        match = _HUNK.match(raw_line)
        if match:
            right_line = int(match.group(1))
            continue
        if path is None or right_line is None or not raw_line:
            continue
        prefix = raw_line[0]
        if prefix == "\\":
            continue
        if prefix == "-":
            continue
        if prefix in {"+", " "}:
            lines[path].add(right_line)
            right_line += 1
    return lines


def format_inline_comment(finding: dict[str, Any]) -> str:
    lines = [f"**[{finding['severity']}] {finding['title']}**"]
    if finding["reasoning"]:
        lines.extend(["", finding["reasoning"]])
    if finding["suggestion"]:
        lines.extend(["", f"Suggested change: {finding['suggestion']}"])
    return "\n".join(lines)


def partition_findings(
    findings: list[dict[str, Any]], diff: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split findings into GitHub-inline comments and summary-only findings."""
    valid_lines = diff_right_lines(diff)
    inline: list[dict[str, Any]] = []
    summary_only: list[dict[str, Any]] = []
    for finding in findings:
        path = str(finding.get("path") or "").removeprefix("a/").removeprefix("b/")
        line = finding.get("line")
        if path and type(line) is int and line in valid_lines.get(path, set()):
            inline.append({
                "path": path,
                "line": line,
                "side": "RIGHT",
                "body": format_inline_comment(finding),
            })
        else:
            reason = (
                "the finding is repository-wide and has no file anchor"
                if not path
                else "the supplied line is not on the current right-side diff"
            )
            summary_only.append({**finding, "summary_reason": reason})
    return inline, summary_only


def format_body(
    marker: str,
    head_sha: str,
    review: dict[str, Any],
    provider: str,
    model: str,
    *,
    inline_count: int = 0,
    summary_findings: list[dict[str, Any]] | None = None,
) -> str:
    counts = {severity: 0 for severity in ("P1", "P2", "P3")}
    for finding in review["findings"]:
        counts[finding["severity"]] += 1
    lines = [
        marker,
        "",
        "## Reviewer",
        f"**{'Approved' if review['approve'] else 'Changes requested'}** for current head `{head_sha[:7]}`",
        "",
        review["summary"] or "No summary provided.",
        "",
        f"Findings: **P1 {counts['P1']} · P2 {counts['P2']} · P3 {counts['P3']}**",
        f"Model: `{provider}/{model}`",
        "",
        "### Findings",
        "",
    ]
    displayed = review["findings"] if summary_findings is None else summary_findings
    if inline_count:
        lines.append(f"_{inline_count} finding{' is' if inline_count == 1 else 's are'} attached inline to the changed code._")
        if displayed:
            lines.append("")
    if not review["findings"]:
        lines.append("_No findings._")
    for finding in displayed:
        lines.extend([
            f"- **[{finding['severity']}] `{finding['key']}`** {finding['title']}",
            *( [f"  - {finding['reasoning']}"] if finding["reasoning"] else [] ),
            *( [f"  - _suggestion:_ {finding['suggestion']}"] if finding["suggestion"] else [] ),
            *( [f"  - _Summary-only because {finding['summary_reason']}._"] if finding.get("summary_reason") else [] ),
        ])
    machine = {
        "version": 1,
        "head_sha": head_sha,
        "verdict": "approve" if review["approve"] else "request_changes",
        "findings": [
            {
                "severity": f["severity"],
                "key": f["key"],
                "title": f["title"],
                "reasoning": f["reasoning"],
                "suggestion": f["suggestion"],
            }
            for f in review["findings"]
        ],
    }
    lines.extend(["", encode_data(machine)])
    return "\n".join(lines) + "\n"


def review_payload(
    marker: str,
    head_sha: str,
    review: dict[str, Any],
    provider: str,
    model: str,
    diff: str,
) -> dict[str, Any]:
    inline, summary_only = partition_findings(review["findings"], diff)
    body = format_body(
        marker,
        head_sha,
        review,
        provider,
        model,
        inline_count=len(inline),
        summary_findings=summary_only,
    )
    payload: dict[str, Any] = {
        "body": body,
        "event": "APPROVE" if review["approve"] else "REQUEST_CHANGES",
        "commit_id": head_sha,
    }
    if inline:
        payload["comments"] = inline
    return payload


def failed_review(detail: str) -> dict[str, Any]:
    return normalize_review({
        "approve": False,
        "summary": (
            "Reviewer could not produce a valid structured verdict. "
            "Steward must resolve the provider or review-input failure before integration."
        ),
        "findings": [{
            "severity": "P1",
            "title": "Reviewer unavailable",
            "reasoning": detail[:1000],
            "suggestion": "Restore a structured Reviewer response, then review this same head again.",
        }],
    })


def failed_delivery_review(status: str, body: str = "") -> dict[str, Any]:
    failures: list[str] = []
    if status == "failed":
        sections = re.finditer(
            r"^### (?!#)([^\n]+)\n([\s\S]*?)(?=^### (?!#)|\Z)",
            body,
            re.MULTILINE,
        )
        for section in sections:
            detail = section.group(2)
            if "catastrophic sanity **fail**" not in detail:
                continue
            score_match = re.search(r"normalized SSIM ([^\s·]+)", detail)
            score = score_match.group(1) if score_match else "unavailable"
            failures.append(
                f"{section.group(1).strip()} mismatches its Origami reference "
                f"(normalized SSIM {score})"
            )
        if "DocC: **failed**" in body:
            failures.append("the current-head DocC preview is unavailable")
        if "Interaction recording: **missing**" in body:
            failures.append("the required interaction recording is missing")
    if failures:
        return normalize_review({
            "approve": False,
            "summary": "Builder's current-head evidence reports material delivery failures.",
            "findings": [{
                "severity": "P1",
                "title": "Current-head evidence fails fidelity requirements",
                "reasoning": "; ".join(failures) + ".",
                "suggestion": (
                    "Use the current-head Swami, Origami, and diff evidence plus the interaction "
                    "recording to correct the implementation, then regenerate every artifact."
                ),
            }],
        })
    return normalize_review({
        "approve": False,
        "summary": "Builder's current-head delivery evidence is not reviewable.",
        "findings": [{
            "severity": "P1",
            "title": "Builder delivery evidence is not ready",
            "reasoning": (
                f"The canonical Builder delivery section reported {status!r}. "
                "A URL or completed workflow alone is not proof of visual, behavioral, or documentation fidelity."
            ),
            "suggestion": "Produce current-head evidence and publish a ready delivery section before review.",
        }],
    })


def missing_visual_delivery_review(paths: tuple[str, ...]) -> dict[str, Any]:
    changed = ", ".join(f"`{path}`" for path in paths[:5])
    if len(paths) > 5:
        changed += f", and {len(paths) - 5} more"
    return normalize_review({
        "approve": False,
        "summary": "A visual change has no canonical current-head Builder delivery.",
        "findings": [{
            "severity": "P1",
            "title": "Visual change lacks current-head evidence",
            "reasoning": (
                f"The consumer classifies {changed} as visual evidence scope, but this pull "
                "request has no canonical Builder delivery marker or exact-head images."
            ),
            "suggestion": (
                "Route the change through Builder and publish the exact-head delivery before review."
            ),
        }],
    })


def request_review(
    candidates: list[tuple[str, str, bool]],
    system: str,
    user: str,
    *,
    image_urls: tuple[str, ...] = (),
    allow_evidence_conflict: bool = False,
) -> tuple[dict[str, Any], str, str]:
    failures: list[str] = []
    for provider, model, supports_images in candidates:
        if image_urls and not supports_images:
            failures.append(f"{provider}/{model}: visual evidence is not enabled for this route")
            continue
        env_name = f"{provider.upper()}_API_KEY"
        api_key = os.environ.get(env_name, "") or os.environ.get("MODEL_API_KEY", "")
        try:
            reply = complete(
                provider,
                model,
                system,
                user,
                api_key,
                image_urls=image_urls,
            )
            return normalize_review(
                extract_json_reply(reply),
                allow_evidence_conflict=allow_evidence_conflict,
            ), provider, model
        except (ModelError, ValueError) as exc:
            failures.append(f"{provider}/{model}: {type(exc).__name__}: {exc}")
    raise ModelError("all configured review providers failed: " + "; ".join(failures))


def run(
    repo: str,
    pr: str,
    root: Path,
    config_path: Path,
    provider_override: str | None = None,
    model_override: str | None = None,
) -> None:
    # A distinct App identity is required for a formal approval that can gate a
    # consumer pull request. Keep its short-lived token out of files and logs.
    os.environ["GH_TOKEN"] = get_installation_token(repo)
    config = load_config(config_path)
    meta = json.loads(_gh([
        "pr", "view", pr, "--repo", repo, "--json", "headRefOid,title,body",
    ]))
    has_builder_delivery = config.builder.marker in str(meta.get("body") or "")
    changed_paths = (
        changed_file_paths(repo, pr)
        if not has_builder_delivery and config.review.visual_evidence_paths
        else ()
    )
    visual_paths = tuple(
        path
        for path in changed_paths
        if any(repository_glob_match(path, pattern) for pattern in config.review.visual_evidence_paths)
    )
    delivery_gate: dict[str, Any] | None = None
    evidence_consistency_gate: dict[str, Any] | None = None
    prior_review_context = ""
    arbitration_context = ""
    delivery_image_urls: tuple[str, ...] = ()
    delivery_image_failure = False
    if config.review.require_builder_delivery and has_builder_delivery:
        status, refreshed_body = wait_for_delivery(
            repo,
            pr,
            str(meta["headRefOid"]),
            timeout_seconds=config.review.delivery_wait_seconds,
        )
        meta["body"] = refreshed_body
        if status not in {"ready", "failed"}:
            raw = failed_delivery_review(status, refreshed_body)
            payload = json.dumps(review_payload(
                config.review.marker,
                meta["headRefOid"],
                raw,
                "deterministic",
                "builder-delivery-gate",
                _gh(["pr", "diff", pr, "--repo", repo]),
            ))
            _gh(["api", f"repos/{repo}/pulls/{pr}/reviews", "-X", "POST", "--input", "-"], stdin=payload)
            return
        if status == "failed":
            delivery_gate = failed_delivery_review(status, refreshed_body)
        if config.review.visual_evidence or config.review.fallback_visual_evidence:
            try:
                provenance = (
                    _delivery_provenance(
                        repo,
                        int(pr),
                        str(meta["headRefOid"]),
                        config.builder.app_login,
                        refreshed_body,
                        root=Path("."),
                    )
                    if "https://github.com/user-attachments/assets/" in refreshed_body
                    else None
                )
                if provenance is not None:
                    comments = json.loads(_gh([
                        "api", f"repos/{repo}/issues/{pr}/comments?per_page=100",
                        "--paginate", "--slurp",
                    ]))
                    history = authenticated_delivery_history(
                        comments,
                        expected_repo=repo,
                        expected_pr=int(pr),
                        builder_app_login=config.builder.app_login,
                    )
                    arbitration_context = authenticated_arbitration_for_head(
                        comments,
                        repo=repo,
                        pr=int(pr),
                        head=str(meta["headRefOid"]),
                        reference_digests=evidence_digests(provenance, "reference"),
                        steward_app_login=config.steward.app_login,
                    )
                    prior = next(
                        (
                            item for item in reversed(history)
                            if item.get("head") != str(meta["headRefOid"])
                        ),
                        None,
                    )
                    if prior is not None:
                        prior_manifest = prior.get("attachments")
                        prior_head = str(prior.get("head") or "")
                        if isinstance(prior_manifest, dict) and prior_head:
                            between = changed_between_heads(
                                repo, prior_head, str(meta["headRefOid"])
                            )
                            changed_visual = tuple(
                                path for path in between
                                if any(
                                    repository_glob_match(path, pattern)
                                    for pattern in config.review.visual_evidence_paths
                                )
                            )
                            if status == "failed":
                                evidence_consistency_gate = stale_visual_evidence_review(
                                    prior_head,
                                    str(meta["headRefOid"]),
                                    prior_manifest,
                                    provenance,
                                    changed_visual,
                                )
                            if (
                                evidence_digests(prior_manifest, "reference")
                                and evidence_digests(prior_manifest, "reference")
                                == evidence_digests(provenance, "reference")
                            ):
                                prior_review_context = prior_review_for_head(
                                    repo,
                                    pr,
                                    prior_head,
                                    config.review.marker,
                                    config.review.app_login,
                                )
                images, _ = _current_delivery_media(
                    refreshed_body,
                    str(meta["headRefOid"]),
                    repo=repo,
                    pr=int(pr),
                    provenance=provenance,
                )
                if evidence_consistency_gate is not None:
                    delivery_image_urls = ()
                elif not images:
                    raise BuilderBlocked(
                        "Reviewer found no trusted current-head visual evidence."
                    )
                else:
                    delivery_image_urls = _fetch_delivery_images(
                        repo,
                        int(pr),
                        str(meta["headRefOid"]),
                        images,
                        os.environ["GH_TOKEN"],
                    )
            except (BuilderBlocked, RuntimeError) as exc:
                # Never let a text-only review approve after the consumer opted
                # into visual review but its exact-head evidence was unreadable.
                delivery_image_urls = ()
                delivery_image_failure = True
                reason = str(exc)
                if reason != "Reviewer found no trusted current-head visual evidence.":
                    reason = (
                        "Reviewer could not verify GitHub-hosted current-head visual evidence."
                    )
                evidence_failure = failed_review(reason)
                if delivery_gate is None:
                    delivery_gate = evidence_failure
                else:
                    delivery_gate["findings"].extend(evidence_failure["findings"])
    elif visual_paths:
        delivery_gate = missing_visual_delivery_review(visual_paths)
    diff = _gh(["pr", "diff", pr, "--repo", repo])
    encoded = diff.encode()
    omitted = max(0, len(encoded) - config.review.max_diff_bytes)
    diff = encoded[: config.review.max_diff_bytes].decode(errors="ignore")
    task = f"{meta.get('title', '')}\n{meta.get('body', '')}\n{diff}"
    discovered = discover_context(root, config.project, task, role="review")
    context = [
        f"## {document.kind}: {document.path}\n\n{document.content}"
        for document in discovered
    ]
    system = (
        f"You are the required code reviewer for {config.project.name}. "
        "Return only JSON with summary:string, approve:boolean, and findings:array. "
        "Also return evidence_interpretation_conflict:boolean. When the supplied authenticated "
        "prior review describes the same unchanged reference digest, preserve that interpretation. "
        "When an authenticated same-head Steward evidence ruling is supplied, its observable visual "
        "interpretation is authoritative; preserve it while independently reviewing code and behavior. "
        "If you believe it is materially wrong or your new guidance would reverse it, set "
        "evidence_interpretation_conflict true and do not direct Builder to change code; Steward "
        "must arbitrate the evidence. "
        "Each finding has severity P1|P2|P3, file, optional integer line, title, "
        "reasoning, and suggestion. P1 is merge-blocking. Do not approve a partial diff. "
        "Treat the canonical Builder delivery section as evidence, not decoration: do not approve "
        "material visual, behavioral, framing, documentation, or current-head mismatches. The "
        "existence of a URL is not proof. For every file-specific finding, cite an exact integer "
        "line that appears on the right side of the supplied diff; omit file and line only for a "
        "genuinely repository-wide finding. When current-head evidence images are supplied, compare "
        "the labeled Swami render, Origami reference, and diff in their PR-body order. Describe "
        "specific visible differences and concrete corrections; never merely repeat the SSIM score. "
        "Visual evidence is scoped to Builder deliveries and visual/product changes. When a pull "
        "request has no canonical Builder marker and no current-head images are supplied, do not "
        "request a screenshot triplet, recording, or other unrelated visual artifact merely because "
        "the consumer supports visual review. Review non-visual control-plane, documentation, and "
        "policy changes from their diff, executable checks, and directly linked prior evidence."
        " A finding must identify a concrete defect caused or preserved by the supplied diff. Do "
        "not block on a hypothetical failure that the shown code explicitly guards against, and "
        "do not require a consumer wrapper to duplicate enforcement implemented by an immutable "
        "referenced reusable workflow. For dependency-pin changes, review the consumer wiring and "
        "supplied dependency evidence; absence of dependency source code from the consumer diff is "
        "not itself a defect."
    )
    evidence_scope = (
        "Builder delivery: present. Apply the configured delivery and visual-evidence contract."
        if has_builder_delivery
        else (
            "Builder delivery: absent. This diff matches configured visual-evidence paths, so the "
            "deterministic current-head evidence gate blocks approval."
            if visual_paths
            else
            "Builder delivery: absent. No current-head images are expected unless this diff itself "
            "changes a visual/product surface; absence of a triplet is not a finding."
        )
    )
    user = "\n\n".join(context + [
        f"## Evidence scope\n\n{evidence_scope}",
        *(
            [
                "## Authenticated prior-head Reviewer continuity\n\n"
                + prior_review_context
            ]
            if prior_review_context else []
        ),
        *(
            ["## Authenticated Steward evidence ruling\n\n" + arbitration_context]
            if arbitration_context else []
        ),
        f"## Pull request\n\n{meta.get('title','')}\n\n{meta.get('body','')}",
        f"## Diff\n\n```diff\n{diff}\n```",
    ])
    provider = provider_override or config.review.provider
    model = model_override or config.review.model
    candidates = [(provider, model, config.review.visual_evidence)]
    if config.review.fallback_provider and config.review.fallback_model:
        candidates.append((
            config.review.fallback_provider,
            config.review.fallback_model,
            config.review.fallback_visual_evidence,
        ))
    marker = config.review.marker
    if delivery_image_failure:
        marker = f"{config.review.marker}\n{config.review.failure_marker}"
    if evidence_consistency_gate is not None:
        raw = evidence_consistency_gate
        provider, model = "deterministic", "evidence-consistency-gate"
    else:
        try:
            raw, provider, model = request_review(
                candidates,
                system,
                user,
                image_urls=delivery_image_urls,
                allow_evidence_conflict=bool(prior_review_context and not arbitration_context),
            )
        except ModelError as exc:
            raw = failed_review(str(exc))
            provider, model = "unavailable", "configured providers exhausted"
            marker = f"{config.review.marker}\n{config.review.failure_marker}"
    if delivery_gate is not None:
        raw["approve"] = False
        raw["summary"] = " ".join(
            value for value in (delivery_gate["summary"], raw.get("summary", "")) if value
        )
        raw["findings"] = delivery_gate["findings"] + raw["findings"]
    if omitted:
        raw["approve"] = False
        raw["findings"].insert(0, {
            "severity": "P1", "key": "agent-factory://truncated-diff",
            "title": f"Reviewer input omitted {omitted} bytes", "reasoning": "The full diff was not reviewed.",
            "suggestion": "Split the pull request or raise the configured review limit.",
        })
    payload = json.dumps(review_payload(
        marker, meta["headRefOid"], raw, provider, model, diff
    ))
    _gh(["api", f"repos/{repo}/pulls/{pr}/reviews", "-X", "POST", "--input", "-"], stdin=payload)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--provider")
    parser.add_argument("--model")
    args = parser.parse_args()
    run(args.repo, args.pr, args.root, args.config, args.provider, args.model)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
