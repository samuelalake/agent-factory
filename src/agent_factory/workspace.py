"""Content-aware repository snapshots for Builder candidate boundaries."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess


WorkspaceSnapshot = tuple[str, str, str, tuple[tuple[str, str], ...]]


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
    resolved_root = root.resolve()
    for relative in _git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0"):
        if not relative:
            continue
        path = resolved_root / relative
        try:
            path.relative_to(resolved_root)
        except ValueError as exc:
            raise RuntimeError(f"untracked path escaped repository: {relative}") from exc
        digest = hashlib.sha256()
        if path.is_symlink():
            digest.update(b"symlink\0")
            digest.update(os.readlink(path).encode(errors="surrogateescape"))
        elif path.is_file():
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
