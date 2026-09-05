"""Repository context and task-relevant skill discovery."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .config import ProjectConfig


@dataclass(frozen=True)
class ContextDocument:
    path: str
    content: str
    kind: str


@dataclass(frozen=True)
class SkillDocument:
    path: str
    name: str
    description: str
    content: str


_WORDS = re.compile(r"[a-z][a-z0-9-]{2,}")


def _terms(text: str) -> set[str]:
    return set(_WORDS.findall(text.lower()))


def _frontmatter(content: str, key: str) -> str:
    if not content.startswith("---\n"):
        return ""
    end = content.find("\n---\n", 4)
    if end < 0:
        return ""
    lines = content[4:end].splitlines()
    for index, line in enumerate(lines):
        match = re.match(rf"^{re.escape(key)}:\s*(.*?)\s*$", line)
        if not match:
            continue
        value = match.group(1).strip()
        if value in {">", ">-", "|", "|-"}:
            folded: list[str] = []
            for continuation in lines[index + 1:]:
                if continuation and not continuation[0].isspace():
                    break
                if continuation.strip():
                    folded.append(continuation.strip())
            return " ".join(folded)
        return value.strip('"\'')
    return ""


def catalog_skills(root: Path, skill_dirs: tuple[str, ...]) -> list[SkillDocument]:
    found: list[SkillDocument] = []
    seen: set[Path] = set()
    for relative in skill_dirs:
        directory = (root / relative).resolve()
        try:
            directory.relative_to(root.resolve())
        except ValueError:
            continue
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*/SKILL.md")):
            resolved = path.resolve()
            if resolved in seen or not resolved.is_file():
                continue
            seen.add(resolved)
            content = resolved.read_text(encoding="utf-8", errors="replace")
            found.append(SkillDocument(
                path=str(resolved.relative_to(root.resolve())),
                name=_frontmatter(content, "name") or resolved.parent.name,
                description=_frontmatter(content, "description"),
                content=content,
            ))
    return found


def select_skills(
    skills: list[SkillDocument],
    task: str,
    *,
    role: str,
    limit: int,
) -> list[SkillDocument]:
    query = _terms(f"{role} {task}")
    ranked: list[tuple[int, str, SkillDocument]] = []
    for skill in skills:
        header = _terms(f"{skill.name} {skill.description}")
        score = len(query & header)
        if role.lower() in header:
            score += 3
        if score:
            ranked.append((score, skill.path, skill))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in ranked[:limit]]


def discover_context(root: Path, project: ProjectConfig, task: str, *, role: str) -> list[ContextDocument]:
    documents: list[ContextDocument] = []
    resolved_root = root.resolve()
    for relative in project.context_files:
        path = (resolved_root / relative).resolve()
        try:
            path.relative_to(resolved_root)
        except ValueError:
            continue
        if path.is_file():
            documents.append(ContextDocument(
                path=str(path.relative_to(resolved_root)),
                content=path.read_text(encoding="utf-8", errors="replace"),
                kind="context",
            ))
    skills = select_skills(
        catalog_skills(resolved_root, project.skill_dirs),
        task,
        role=role,
        limit=project.max_skills,
    )
    documents.extend(
        ContextDocument(path=skill.path, content=skill.content, kind="skill")
        for skill in skills
    )
    return documents
