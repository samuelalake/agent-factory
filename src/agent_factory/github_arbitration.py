"""Exceptional Steward arbitration for authenticated visual-evidence conflicts."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
from typing import Any

from .config import load_config
from .github_builder import (
    BuilderBlocked,
    _current_delivery_media,
    _delivery_provenance,
    _fetch_delivery_images,
)
from .github_review import evidence_digests
from .model import ModelError, complete
from .protocol import decode_data, encode_data, extract_json_reply


MARKER = "<!-- steward:agent-factory-evidence-arbitration -->"
CONFLICT_KEY = "agent-factory://evidence-interpretation-conflict"


def _gh(args: list[str], *, stdin: str | None = None, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["gh", *args], input=stdin, text=True, capture_output=True, cwd=cwd
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout


def _same_app(actual: str, expected: str) -> bool:
    return actual.removesuffix("[bot]") == expected.removesuffix("[bot]")


def _pages(raw: str) -> list[dict[str, Any]]:
    value = json.loads(raw)
    if not isinstance(value, list):
        return []
    if value and all(isinstance(page, list) for page in value):
        return [item for page in value for item in page if isinstance(item, dict)]
    return [item for item in value if isinstance(item, dict)]


def current_conflict_review(
    reviews: list[dict[str, Any]], head: str, marker: str, app_login: str
) -> dict[str, Any] | None:
    selected = None
    for review in reviews:
        user = review.get("user") or {}
        body = str(review.get("body") or "")
        data = decode_data(body)
        if not (
            isinstance(user, dict)
            and user.get("type") == "Bot"
            and _same_app(str(user.get("login") or ""), app_login)
            and marker in body
            and isinstance(data, dict)
            and data.get("head_sha") == head
            and str(review.get("state") or "").upper() != "DISMISSED"
        ):
            continue
        findings = data.get("findings") or []
        if any(
            isinstance(finding, dict) and finding.get("key") == CONFLICT_KEY
            for finding in findings
        ):
            selected = review
    return selected


def authenticated_ruling(
    comments: list[dict[str, Any]], *, app_login: str, repo: str, pr: int,
    head: str, references: tuple[str, ...]
) -> dict[str, Any] | None:
    selected = None
    for comment in comments:
        user = comment.get("user") or {}
        body = str(comment.get("body") or "")
        data = decode_data(body)
        if not (
            isinstance(user, dict)
            and _same_app(str(user.get("login") or ""), app_login)
            and MARKER in body
            and isinstance(data, dict)
            and data.get("role") == "steward"
            and data.get("kind") == "evidence_arbitration"
            and data.get("repo") == repo
            and data.get("pr") == pr
            and data.get("head_sha") == head
            and tuple(data.get("reference_digests") or ()) == references
            and data.get("resolved") is True
        ):
            continue
        selected = data
    return selected


def normalize_ruling(raw: dict[str, Any]) -> dict[str, Any]:
    observations = []
    for value in raw.get("observations") or []:
        text = str(value or "").strip()
        if text and text not in observations:
            observations.append(text[:500])
        if len(observations) == 12:
            break
    interpretation = str(raw.get("authoritative_interpretation") or "").strip()[:2000]
    resolved = raw.get("resolved") is True and bool(interpretation) and bool(observations)
    return {
        "resolved": resolved,
        "observations": observations,
        "authoritative_interpretation": interpretation,
        "reason": str(raw.get("reason") or "").strip()[:1000],
    }


def request_ruling(
    config: Any, system: str, user: str, image_urls: tuple[str, ...]
) -> tuple[dict[str, Any], str, str]:
    candidates = [(
        config.steward.arbitration_provider,
        config.steward.arbitration_model,
        config.steward.arbitration_visual_evidence,
    )]
    if config.steward.arbitration_fallback_provider:
        candidates.append((
            config.steward.arbitration_fallback_provider,
            config.steward.arbitration_fallback_model,
            config.steward.arbitration_fallback_visual_evidence,
        ))
    failures = []
    for provider, model, supports_images in candidates:
        if not supports_images:
            failures.append(f"{provider}/{model}: visual evidence is not enabled")
            continue
        key = os.environ.get(f"{provider.upper()}_API_KEY", "") or os.environ.get(
            "MODEL_API_KEY", ""
        )
        try:
            raw = extract_json_reply(complete(
                provider, str(model), system, user, key, image_urls=image_urls
            ))
            ruling = normalize_ruling(raw)
            if not ruling["resolved"]:
                raise ValueError("Steward arbitration remained unresolved")
            return ruling, provider, str(model)
        except (ModelError, ValueError) as exc:
            failures.append(f"{provider}/{model}: {type(exc).__name__}: {exc}")
    raise ModelError("all configured Steward arbitration providers failed: " + "; ".join(failures))


def _format(ruling: dict[str, Any], payload: dict[str, Any]) -> str:
    facts = "\n".join(f"- {item}" for item in ruling["observations"])
    return "\n\n".join([
        MARKER,
        "## Steward · evidence arbitration",
        f"**Resolved** for current head `{payload['head_sha'][:7]}`",
        "### Observable evidence facts\n\n" + facts,
        "### Authoritative interpretation\n\n" + ruling["authoritative_interpretation"],
        (
            "This exceptional ruling resolves only the authenticated evidence interpretation. "
            "Reviewer still owns code findings and merge approval; Builder is not dispatched by arbitration."
        ),
        encode_data(payload),
    ])


def _upsert(repo: str, pr: int, comments: list[dict[str, Any]], body: str, app_login: str) -> None:
    target = None
    for comment in comments:
        user = comment.get("user") or {}
        if (
            isinstance(user, dict)
            and _same_app(str(user.get("login") or ""), app_login)
            and MARKER in str(comment.get("body") or "")
        ):
            target = comment.get("id")
    args = ["api", f"repos/{repo}/issues/comments/{target}", "-X", "PATCH"] if target else [
        "api", f"repos/{repo}/issues/{pr}/comments", "-X", "POST"
    ]
    _gh([*args, "-f", f"body={body}"])


def _set_output(value: bool) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"arbitrated={'true' if value else 'false'}\n")


def run(repo: str, pr: int, root: Path, config_path: Path) -> bool:
    config = load_config(config_path)
    meta = json.loads(_gh([
        "pr", "view", str(pr), "--repo", repo, "--json", "headRefOid,title,body"
    ], cwd=root))
    head = str(meta.get("headRefOid") or "")
    reviews = _pages(_gh([
        "api", f"repos/{repo}/pulls/{pr}/reviews?per_page=100", "--paginate", "--slurp"
    ], cwd=root))
    if current_conflict_review(reviews, head, config.review.marker, config.review.app_login) is None:
        _set_output(False)
        return False
    provenance = _delivery_provenance(
        repo, pr, head, config.builder.app_login, str(meta.get("body") or ""), root=root
    )
    if provenance is None:
        raise BuilderBlocked("Steward found no authenticated current-head Builder evidence")
    references = evidence_digests(provenance, "reference")
    if not references:
        raise BuilderBlocked("Steward found no authenticated reference digest")
    comments = _pages(_gh([
        "api", f"repos/{repo}/issues/{pr}/comments?per_page=100", "--paginate", "--slurp"
    ], cwd=root))
    if authenticated_ruling(
        comments, app_login=config.steward.app_login, repo=repo, pr=pr,
        head=head, references=references
    ) is not None:
        _set_output(True)
        return True
    images, _ = _current_delivery_media(
        str(meta.get("body") or ""), head, repo=repo, pr=pr, provenance=provenance
    )
    if not images:
        raise BuilderBlocked("Steward found no authenticated current-head evidence images")
    image_urls = _fetch_delivery_images(repo, pr, head, images, os.environ.get("GH_TOKEN", ""))
    history = "\n\n".join(
        str(review.get("body") or "")[:8000]
        for review in reviews
        if config.review.marker in str(review.get("body") or "")
    )[-24000:]
    labels = "\n".join(
        f"Image {index + 1}: {label}" for index, (label, _) in enumerate(images)
    )
    system = (
        f"You are Steward, the engineering-manager evidence arbiter for {config.project.name}. "
        "This is an exceptional arbitration, not routine code review. Compare only the supplied, "
        "authenticated images in their labeled order and reconcile contradictory Reviewer descriptions. "
        "State concrete visible facts without inferring implementation. Do not suggest code changes, approve "
        "the PR, or dispatch Builder. Return only JSON with resolved:boolean, observations:string[], "
        "authoritative_interpretation:string, and reason:string. Resolve only when the images make the disputed "
        "composition unambiguous; otherwise return resolved false."
    )
    user = "\n\n".join([
        f"## Pull request\n\n{meta.get('title', '')}\nHead: {head}",
        f"## Authenticated image order\n\n{labels}",
        f"## Authenticated Reviewer history\n\n{history}",
    ])
    ruling, provider, model = request_ruling(config, system, user, image_urls)
    payload = {
        "version": 1,
        "role": "steward",
        "kind": "evidence_arbitration",
        "repo": repo,
        "pr": pr,
        "head_sha": head,
        "reference_digests": list(references),
        "resolved": True,
        "observations": ruling["observations"],
        "authoritative_interpretation": ruling["authoritative_interpretation"],
        "provider": provider,
        "model": model,
    }
    _upsert(repo, pr, comments, _format(ruling, payload), config.steward.app_login)
    _set_output(True)
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    try:
        run(args.repo, args.pr, args.root, args.config)
    except (BuilderBlocked, ModelError, RuntimeError, ValueError) as exc:
        _set_output(False)
        print(f"Steward retained unresolved evidence arbitration: {str(exc)[:500]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
