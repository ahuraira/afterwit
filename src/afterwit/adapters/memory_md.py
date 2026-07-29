"""Claude memory markdown adapter. SPEC §6.3."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from afterwit.events import Event
from afterwit.redact import redact

_WIKILINK = re.compile(r"\[\[([^\]|#]+)([^\]]*)\]\]")


def iter_events(path: Path) -> Iterator[Event]:
    text = path.read_text(encoding="utf-8")
    frontmatter, body, body_line = _split_frontmatter(text)
    title = str(frontmatter.get("name") or path.stem).strip()
    desc = str(frontmatter.get("description") or "").strip()
    card_type = _card_type(frontmatter)
    body = _canonicalize_links(body, path)
    event_text = "\n\n".join(p for p in (title, desc, body.strip()) if p)
    yield Event(
        source_path=str(path),
        lines=(body_line, max(body_line, len(text.splitlines()))),
        project=_project_from_memory_path(path),
        ts=None,
        role="memory",
        kind="doc",
        text=redact(event_text),
        meta={
            "harness": "memory",
            "model": None,
            "kind": "doc",
            "frontmatter": frontmatter,
            "card_type": card_type,
        },
    )


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str, int]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text, 1
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            raw = "\n".join(lines[1:i])
            data = yaml.safe_load(raw) or {}
            return (data if isinstance(data, dict) else {}), "\n".join(lines[i + 1:]), i + 2
    return {}, text, 1


def _card_type(frontmatter: dict[str, Any]) -> str:
    meta = frontmatter.get("metadata")
    raw = meta.get("type") if isinstance(meta, dict) else frontmatter.get("type")
    return {
        "user": "preference",
        "feedback": "preference",
        "project": "fact",
        "reference": "doc_ref",
        "memory": "fact",
    }.get(str(raw or "memory"), "fact")


def _canonicalize_links(body: str, path: Path) -> str:
    """Translate memory filename links to the imported card's frontmatter title."""
    def replace(match: re.Match[str]) -> str:
        target = match.group(1).strip()
        if Path(target).name != target:
            return match.group(0)
        sibling = path.parent / f"{target}.md"
        if not sibling.is_file():
            return match.group(0)
        frontmatter, _, _ = _split_frontmatter(sibling.read_text(encoding="utf-8"))
        title = str(frontmatter.get("name") or sibling.stem).strip()
        return f"[[{title}{match.group(2)}]]"

    return _WIKILINK.sub(replace, body)


def _project_from_memory_path(path: Path) -> str:
    for part in path.parts:
        if part.startswith("-home-"):
            return part.rsplit("-Projects-", 1)[-1].replace("-", "_") or "global"
    return "global"
