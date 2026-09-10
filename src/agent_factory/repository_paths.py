"""Repository-path matching shared by role adapters."""

from __future__ import annotations

import fnmatch


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
