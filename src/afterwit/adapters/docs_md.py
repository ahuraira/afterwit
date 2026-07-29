"""Project docs markdown adapter. SPEC §6.4."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from afterwit import config as config_mod
from afterwit.events import Event
from afterwit.redact import redact

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def iter_events(path: Path) -> Iterator[Event]:
    cfg = config_mod.load()
    text = path.read_text(encoding="utf-8")
    sections = _sections(text)
    project = config_mod.project_from_cwd(_project_dir(path), cfg.projects_root,
                                          cfg.project_aliases)
    for heading, start, end, body in sections:
        outline = "\n".join(h for h, _, _, _ in sections[:30])
        first_para = _first_paragraph(body)
        event_text = f"{heading}\n\n{first_para}".strip()
        if not event_text:
            continue
        yield Event(
            source_path=str(path),
            lines=(start, end),
            project=project,
            ts=None,
            role="doc",
            kind="doc",
            text=redact(event_text),
            meta={
                "harness": "doc",
                "model": None,
                "kind": "doc",
                "heading": heading,
                "outline": outline,
            },
        )


def _sections(text: str) -> list[tuple[str, int, int, str]]:
    lines = text.splitlines()
    matches = [(i + 1, m.group(2).strip()) for i, line in enumerate(lines) if (m := _HEADING.match(line))]
    if not matches:
        return [(Path("document").stem, 1, max(1, len(lines)), text)]
    out: list[tuple[str, int, int, str]] = []
    for idx, (start, heading) in enumerate(matches):
        next_start = matches[idx + 1][0] if idx + 1 < len(matches) else len(lines) + 1
        body = "\n".join(lines[start: next_start - 1])
        out.append((heading, start, max(start, next_start - 1), body))
    return out


def _first_paragraph(text: str) -> str:
    parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return parts[0] if parts else ""


def _project_dir(path: Path) -> Path:
    parts = path.parts
    if "docs" in parts:
        return Path(*parts[: parts.index("docs")])
    return path.parent
