"""Wiki writer: executes postprocess actions verbatim, regenerates catalog pages.

ADR-004: this module makes NO merge/supersede/gate judgment of its own — it
applies the actions `postprocess.process()` already decided. It is the ONLY
writer of the markdown wiki (the source of truth; SQLite is the derived cache,
ADR-001/Manifesto P4).

Actions (shapes fixed by postprocess.py):
  ("write", card)                  -> save active card + index it
  ("merge", existing_id, cand)     -> union sources into existing, keep newer body
  ("supersede", existing_id, cand) -> retire old (status+superseded_by), write new
  ("queue", card, reason)          -> hand to the human review queue

Also regenerates `index.md` (catalog) and per-project `brief.md` (what the
`project_brief` MCP tool serves), and maintains the managed `<!-- afterwit:begin -->`
block inside a project's CLAUDE.md (SPEC §9.3) without touching a byte outside
the fence.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import cards as cards_mod
from . import config as config_mod
from . import index_db, ui

_FENCE_RE = re.compile(r"<!-- afterwit:begin -->.*?<!-- afterwit:end -->", re.DOTALL)
_BEGIN, _END = "<!-- afterwit:begin -->", "<!-- afterwit:end -->"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def log_path(wiki_root: Path) -> Path:
    """One append-only audit log per device (ADR-019/020). A shared `log.md` is
    appended by every device at the same tail line and conflicts on every
    `git pull --rebase`; per-device files never collide and the audit trail
    (P6) still syncs. Canonical definition lives in config (wiki imports ui, so
    ui/distill/mcp cannot import wiki)."""
    return config_mod.log_path(wiki_root)


def _log(wiki_root: Path, line: str) -> None:
    log = log_path(wiki_root)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as f:
        f.write(f"- {_now()} {line}\n")


def _load_existing(conn: sqlite3.Connection, card_id: str) -> cards_mod.Card:
    row = conn.execute("SELECT path FROM cards WHERE id=?", (card_id,)).fetchone()
    if row is None:
        raise KeyError(f"unknown card in action: {card_id}")
    return cards_mod.load(Path(row["path"]))


def _union_sources(a: list[dict], b: list[dict]) -> list[dict]:
    out, seen = [], set()
    for s in [*a, *b]:
        key = frozenset((k, str(v)) for k, v in s.items())
        if key not in seen:
            seen.add(key)
            out.append(s)
    # Provenance is useful evidence, not an unbounded transcript inventory.
    return out if len(out) <= 16 else out[:8] + out[-8:]


def execute(action: tuple, cfg: config_mod.Config, conn: sqlite3.Connection) -> cards_mod.Card | None:
    """Apply one postprocess action to the wiki + index. Returns the written
    card (the new active card for supersede) or None for queue actions."""
    kind = action[0]

    if kind == "write":
        card = action[1]
        path = cards_mod.save(card, cfg.wiki_root)
        index_db.upsert_card(conn, card, str(path))
        _log(cfg.wiki_root, f"write {card.id} [{card.type}] {card.title}")
        return card

    if kind == "merge":
        _, existing_id, cand = action
        existing = _load_existing(conn, existing_id)
        # SPEC §7.1 says the newer body wins — but NOT over a body a human approved.
        # That text passed the review gate (ADR-011); replacing it with fresh model
        # prose would edit an approved claim with nobody looking. Corroboration is
        # additive: the evidence grows, the approved wording stands (ADR-031).
        if not existing.verified:
            existing.body = cand.body
        existing.sources = _union_sources(existing.sources, cand.sources)
        existing.files = sorted(set(existing.files) | set(cand.files))
        # Re-anchor to the candidate's commit (ADR-020): the body now describes
        # the code as of the candidate's HEAD, so keeping the old anchor would
        # flag the card stale the instant it was refreshed — the drift-triggered
        # refresh loop (ADR-014) could then never clear drift. Residual: `files`
        # is a union, so old-only entries were not re-verified at the new commit;
        # the next distill/survey re-checks them.
        if cand.source_commit:
            existing.source_commit = cand.source_commit
        if cand.repo_url:
            existing.repo_url = cand.repo_url
        existing.updated = _today()
        path = cards_mod.save(existing, cfg.wiki_root)
        index_db.upsert_card(conn, existing, str(path))
        verb = "corroborate" if existing.verified else "merge"
        _log(cfg.wiki_root, f"{verb} {cand.id} -> {existing.id} {existing.title}")
        return existing

    if kind == "supersede":
        _, existing_id, cand = action
        old = _load_existing(conn, existing_id)
        old.status = "superseded"
        old.superseded_by = cand.id
        old.updated = _today()
        old_path = cards_mod.save(old, cfg.wiki_root)
        index_db.upsert_card(conn, old, str(old_path))
        new_path = cards_mod.save(cand, cfg.wiki_root)
        index_db.upsert_card(conn, cand, str(new_path))
        _log(cfg.wiki_root, f"supersede {old.id} -> {cand.id} {cand.title}")
        return cand

    if kind == "queue":
        _, card, reason = action
        ui.queue_insert(conn, card, reason, cfg.wiki_root)
        _log(cfg.wiki_root, f"queue {card.id} ({reason}) {card.title}")
        return None

    raise ValueError(f"unknown action kind: {kind!r}")


# --- catalog regeneration ---------------------------------------------------

def _rel_link(wiki_root: Path, path: str) -> str:
    try:
        # as_posix: this goes inside a markdown link, where `\` is the escape
        # character — a Windows-native relpath renders every index.md link broken,
        # and the wiki is git-synced to machines that are not Windows.
        return Path(path).relative_to(wiki_root).as_posix()
    except ValueError:
        return path


def _first_line(body: str) -> str:
    for line in body.splitlines():
        line = line.strip()
        if line and not line.startswith("```"):
            return line[:120]
    return ""


def regenerate(cfg: config_mod.Config, conn: sqlite3.Connection) -> None:
    """Rewrite index.md and every project's brief.md from the current index."""
    write_index(cfg, conn)
    projects = [r["project"] for r in conn.execute(
        "SELECT DISTINCT project FROM cards WHERE status='active' AND project!='global'")]
    for p in projects:
        write_brief(cfg, conn, p)


def write_index(cfg: config_mod.Config, conn: sqlite3.Connection) -> Path:
    lines = ["# Knowledge Index", f"_generated {_today()}_", ""]

    by_type = conn.execute(
        "SELECT type, COUNT(*) n FROM cards WHERE status='active' GROUP BY type ORDER BY n DESC"
    ).fetchall()
    if by_type:
        lines.append("## By type")
        lines += [f"- {r['type']}: {r['n']}" for r in by_type]
        lines.append("")

    projects = conn.execute(
        "SELECT DISTINCT project FROM cards WHERE status='active' ORDER BY project"
    ).fetchall()
    for pr in projects:
        p = pr["project"]
        rows = conn.execute(
            "SELECT title, type, path FROM cards WHERE status='active' AND project=? "
            "ORDER BY type, title", (p,)).fetchall()
        lines.append(f"## {p} ({len(rows)} active)")
        lines += [f"- [{r['title']}]({_rel_link(cfg.wiki_root, r['path'])}) `{r['type']}`"
                  for r in rows]
        lines.append("")

    cfg.wiki_root.mkdir(parents=True, exist_ok=True)
    path = cfg.wiki_root / "index.md"
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path


def write_brief(cfg: config_mod.Config, conn: sqlite3.Connection, project: str) -> Path:
    """One page per project: active decisions + top gotchas. Served by the
    `project_brief` MCP tool. Also feeds the managed CLAUDE.md block."""
    decisions = conn.execute(
        "SELECT title, body, path FROM cards WHERE status='active' AND project=? "
        "AND type='decision' ORDER BY usefulness DESC, updated DESC LIMIT 10",
        (project,)).fetchall()
    gotchas = conn.execute(
        "SELECT title, body, path FROM cards WHERE status='active' AND project=? "
        "AND type IN ('gotcha','error_fix') ORDER BY usefulness DESC, updated DESC LIMIT 10",
        (project,)).fetchall()

    lines = [f"# {project} — brief", f"_generated {_today()}_", "",
             "## Active decisions"]
    lines += [f"- [{r['title']}]({Path(_rel_link(cfg.wiki_root, r['path'])).name}) — {_first_line(r['body'])}"
              for r in decisions] or ["- (none yet)"]
    lines += ["", "## Top gotchas & fixes"]
    lines += [f"- **{r['title']}** — {_first_line(r['body'])}" for r in gotchas] or ["- (none yet)"]
    lines += ["", "## Deep history",
              "Use the `recall`, `why`, `lookup_error`, `for_file` MCP tools for more."]

    path = cfg.wiki_root / "projects" / project / "brief.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path


# --- managed CLAUDE.md block (SPEC §9.3) ------------------------------------

def write_managed_block(path: Path, inner: str) -> None:
    """Replace (or create at EOF) the `<!-- afterwit:begin -->…<!-- afterwit:end -->` block
    in an existing file. NEVER touches a byte outside the fence."""
    block = f"{_BEGIN}\n{inner.rstrip()}\n{_END}"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if _FENCE_RE.search(text):
        text = _FENCE_RE.sub(lambda _: block, text, count=1)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += ("\n" if text else "") + block + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def managed_block_text(cfg: config_mod.Config, conn: sqlite3.Connection, project: str) -> str:
    """≤10 lines: top decisions + gotchas + tool hint, for the CLAUDE.md fence."""
    decisions = conn.execute(
        "SELECT title FROM cards WHERE status='active' AND project=? AND type='decision' "
        "ORDER BY usefulness DESC, updated DESC LIMIT 3", (project,)).fetchall()
    gotchas = conn.execute(
        "SELECT title FROM cards WHERE status='active' AND project=? "
        "AND type IN ('gotcha','error_fix') ORDER BY usefulness DESC, updated DESC LIMIT 2",
        (project,)).fetchall()
    lines = ["## afterwit knowledge (auto-generated — edits below the fence are overwritten)"]
    lines += [f"- decision: {r['title']}" for r in decisions]
    lines += [f"- gotcha: {r['title']}" for r in gotchas]
    lines.append("Deep history: `recall` / `why` / `lookup_error` / `for_file` MCP tools.")
    return "\n".join(lines[:10])
