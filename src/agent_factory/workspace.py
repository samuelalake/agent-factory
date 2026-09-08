"""Content-aware repository snapshots for Builder candidate boundaries."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess


WorkspaceSnapshot = tuple[str, str, str, tuple[tuple[str, str], ...]]
MAX_UNTRACKED_FILES = 1_000
MAX_UNTRACKED_BYTES = 64 * 1024 * 1024


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    ).stdout


def _untracked_identity(root: Path) -> tuple[tuple[str, str], ...]:
    identities: list[tuple[str, str]] = []
    aggregate_bytes = 0
    resolved_root = root.resolve()
    relatives = [
        item
        for item in _git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
        if item
    ]
    if len(relatives) > MAX_UNTRACKED_FILES:
        raise RuntimeError(
            f"workspace snapshot exceeds {MAX_UNTRACKED_FILES} untracked files"
        )
    for relative in relatives:
        path = resolved_root / relative
        try:
            path.relative_to(resolved_root)
        except ValueError as exc:
            raise RuntimeError(f"untracked path escaped repository: {relative}") from exc
        digest = hashlib.sha256()
        if path.is_symlink():
            target = os.readlink(path).encode(errors="surrogateescape")
            aggregate_bytes += len(target)
            if aggregate_bytes > MAX_UNTRACKED_BYTES:
                raise RuntimeError(
                    f"workspace snapshot exceeds {MAX_UNTRACKED_BYTES} untracked bytes"
                )
            digest.update(b"symlink\0")
            digest.update(target)
        elif path.is_file():
            aggregate_bytes += path.stat().st_size
            if aggregate_bytes > MAX_UNTRACKED_BYTES:
                raise RuntimeError(
                    f"workspace snapshot exceeds {MAX_UNTRACKED_BYTES} untracked bytes"
                )
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        else:
            digest.update(b"non-file")
        identities.append((relative, digest.hexdigest()))
    return tuple(identities)


def workspace_snapshot(root: Path) -> WorkspaceSnapshot:
    """Capture status plus exact tracked, staged, and untracked content state."""
    return (
        _git(root, "status", "--porcelain=v1", "-z"),
        _git(root, "diff", "--binary"),
        _git(root, "diff", "--cached", "--binary"),
        _untracked_identity(root),
    )
