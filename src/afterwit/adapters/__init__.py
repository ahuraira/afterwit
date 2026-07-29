"""Source adapters for SPEC §6 ingestion."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sqlite3
import sys
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from afterwit import config as config_mod
from afterwit import cards as cards_mod
from afterwit import index_db

if TYPE_CHECKING:
    from afterwit.events import Event

log = logging.getLogger(__name__)

_warned: set[tuple] = set()


def warn_once(key: tuple, msg: str, *args: object) -> None:
    """Skip-and-log, once per distinct cause (CLAUDE.md: adapters must not crash
    on schema drift — but they must not drown the run either).

    Measured 2026-07-29: a single 5k-line transcript carrying an unknown
    `file-history-delta` record emitted 585 KB of warnings and buried the actual
    stdout of the command that triggered it. The cause is one unhandled record
    TYPE per file, not one per line, so that is the dedupe key; the line number
    of the first occurrence is kept as the pointer to go look at."""
    if key in _warned:
        return
    _warned.add(key)
    log.warning(msg, *args)


_DECISION_DOCS = {"adr.md", "decisions.md"}
_DECISION_HEADING = re.compile(
    r"^\[?(?:ADR|BD|DD|DECISION|D)[-_ ]?\d+\]?(?=\s|:|—|–|$)", re.IGNORECASE
)
_DECISION_IMPORT_VERSION = "decision-headings-v2"
_MEMORY_IMPORT_VERSION = "memory-links-v2"


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def checkpoint_current(conn: sqlite3.Connection, path: Path, digest: str | None = None) -> bool:
    st = path.stat()
    row = conn.execute(
        "SELECT mtime, bytes_done, content_hash FROM checkpoints WHERE source=?",
        (str(path),),
    ).fetchone()
    if row is None:
        return False
    same_stat = (float(row["mtime"]) == st.st_mtime
                 and int(row["bytes_done"]) == st.st_size)
    if not same_stat:
        return False
    return digest is None or row["content_hash"] == digest


def write_checkpoint(conn: sqlite3.Connection, path: Path, digest: str | None = None) -> None:
    st = path.stat()
    conn.execute(
        """INSERT INTO checkpoints(source, mtime, bytes_done, content_hash)
           VALUES(?,?,?,?)
           ON CONFLICT(source) DO UPDATE SET
             mtime=excluded.mtime,
             bytes_done=excluded.bytes_done,
             content_hash=excluded.content_hash""",
        (str(path), st.st_mtime, st.st_size, digest or file_hash(path)),
    )
    conn.commit()


def guarded_events(
    conn: sqlite3.Connection | None,
    path: Path,
    parser: Callable[[Path], Iterable["Event"]],
) -> tuple[list["Event"], bool]:
    if conn is not None and checkpoint_current(conn, path):
        return [], True
    digest = file_hash(path) if conn is not None else None
    events = list(parser(path))
    if conn is not None:
        write_checkpoint(conn, path, digest)
    return events, False


def _stable_id(source: str, path: Path, key: str) -> str:
    return hashlib.sha256(f"{source}\0{path}\0{key}".encode()).hexdigest()[:26].upper()


def _is_decision_doc(source: str, path: Path) -> bool:
    return source == "docs" and path.name.lower() in _DECISION_DOCS


def _materialization_version(source: str, path: Path) -> str | None:
    if _is_decision_doc(source, path):
        return _DECISION_IMPORT_VERSION
    if source == "memory":
        return _MEMORY_IMPORT_VERSION
    return None


def _prune_obsolete_decisions(
    cfg: config_mod.Config, conn: sqlite3.Connection, path: Path, keep: set[str]
) -> None:
    """Remove deterministic cards produced by the old every-heading importer."""
    target = path.resolve()
    stale: list[tuple[Path, str]] = []
    for card_path, card in cards_mod.iter_cards(cfg.wiki_root):
        same_source = any(
            Path(str(source.get("path", ""))).expanduser().resolve() == target
            for source in card.sources
        )
        if (same_source and card.type == "decision"
                and card.reviewed_by == "deterministic-import" and card.id not in keep):
            stale.append((card_path, card.id))
    has_embeddings = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='embeddings'"
    ).fetchone()
    for card_path, card_id in stale:
        card_path.unlink(missing_ok=True)
        conn.execute("DELETE FROM cards WHERE id=?", (card_id,))
        conn.execute("DELETE FROM cards_fts WHERE id=?", (card_id,))
        conn.execute("DELETE FROM links WHERE src=? OR dst=?", (card_id, card_id))
        if has_embeddings:
            conn.execute("DELETE FROM embeddings WHERE card_id=?", (card_id,))


def _materialize(cfg: config_mod.Config, conn: sqlite3.Connection, source: str,
                 path: Path, events: list["Event"]) -> int:
    """Copy already-distilled memory/docs into the canonical card wiki."""
    if source not in {"memory", "docs", "db"}:
        return 0
    today = datetime.now(timezone.utc).date().isoformat()
    written = 0
    is_decisions = _is_decision_doc(source, path)
    selected = ([event for event in events
                 if _DECISION_HEADING.match(str(event.meta.get("heading", "")))]
                if is_decisions else events[:1])
    keep = {_stable_id(source, path, str(event.meta.get("heading") or event.text))
            for event in selected}
    if is_decisions:
        _prune_obsolete_decisions(cfg, conn, path, keep)
    for event in selected:
        title, _, detail = event.text.partition("\n")
        title = title.strip() or path.stem
        heading = str(event.meta.get("heading") or title)
        card_type = str(event.meta.get("card_type") or
                        ("db_schema" if source == "db" else "doc_ref"))
        if is_decisions:
            card_type = "decision"
        start, end = event.lines
        card = cards_mod.Card(
            id=_stable_id(source, path, heading), type=card_type,
            title=title[:80], project=event.project or "global", status="active",
            body=((detail.strip() or f"See {path.name}, section {heading}.")
                  + (f"\n\nHeadings: {event.meta.get('outline')}"
                     if source == "docs" and event.meta.get("outline") and not is_decisions else ""))[:1500],
            sources=[{"path": str(path), "lines": f"L{start}-L{end}", "heading": heading,
                      "harness": source, "kind": "doc"}],
            tags=[source], files=[], confidence=1.0, verified=True,
            created=today, updated=today, reviewed_by="deterministic-import",
            distilled_by="deterministic-import",  # no LLM read this; a parser did
        )
        saved = cards_mod.save(card, cfg.wiki_root)
        index_db.upsert_card(conn, card, str(saved))
        written += 1
    conn.commit()
    return written


def _iter_paths(source: str, projects_root: Path) -> Iterator[Path]:
    from .. import harness  # honours CLAUDE_CONFIG_DIR / CODEX_HOME

    if source == "claude":
        # rglob is deliberate: subagents/**/agent-*.jsonl are real transcripts and hold
        # real knowledge. journal.jsonl is the workflow runner's LEDGER — a different
        # schema entirely (`started`/`result`), so feeding it to a transcript parser
        # warned "unknown record type" on every workflow session it found.
        yield from sorted(p for p in harness.sessions_dir("claude").rglob("*.jsonl")
                          if p.name != "journal.jsonl")
    elif source == "codex":
        yield from sorted(harness.sessions_dir("codex").rglob("rollout-*.jsonl"))
    elif source == "memory":
        yield from sorted(harness.sessions_dir("claude").glob("*/memory/*.md"))
    elif source == "docs":
        yield from sorted(projects_root.glob("*/docs/**/*.md"))


def _parser(source: str) -> Callable[[Path], Iterable["Event"]]:
    if source == "claude":
        from .claude_jsonl import iter_events
    elif source == "codex":
        from .codex_jsonl import iter_events
    elif source == "memory":
        from .memory_md import iter_events
    elif source == "docs":
        from .docs_md import iter_events
    else:
        raise ValueError(source)
    return iter_events


def ingest(source: str, *, limit: int | None = None) -> list[tuple[Path, int, bool]]:
    cfg = config_mod.load()
    try:
        conn = index_db.connect(cfg.db_path)
    except sqlite3.OperationalError:
        fallback = Path("/tmp/afterwit/index.db")
        log.warning("configured DB unavailable; using %s for adapter checkpoints", fallback)
        conn = index_db.connect(fallback)
    rows: list[tuple[Path, int, bool]] = []
    if source == "all":
        for name in ("claude", "codex", "memory", "docs", "db"):
            rows.extend(ingest(name, limit=limit))
        conn.close()
        return rows
    if source == "db":
        from .db_schema import iter_config_events
        for i, (label, events) in enumerate(iter_config_events(cfg)):
            if limit is not None and i >= limit:
                break
            pseudo = Path(label)
            _materialize(cfg, conn, source, pseudo, events)
            rows.append((pseudo, len(events), False))
        conn.close()
        return rows
    parser = _parser(source)
    if source in {"memory", "docs"}:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS materialized_sources("
            "source TEXT PRIMARY KEY, imported TEXT)"
        )
        conn.commit()
    for i, path in enumerate(_iter_paths(source, cfg.projects_root)):
        if limit is not None and i >= limit:
            break
        events, skipped = guarded_events(conn, path, parser)
        marker = (conn.execute(
            "SELECT imported FROM materialized_sources WHERE source=?", (str(path),)
        ).fetchone() if source in {"memory", "docs"} else None)
        version = _materialization_version(source, path)
        needs_upgrade = bool(version and (marker is None or marker["imported"] != version))
        if skipped and (marker is None or needs_upgrade):
            events, skipped = list(parser(path)), False
        if not skipped:
            _materialize(cfg, conn, source, path, events)
            if source in {"memory", "docs"}:
                imported = _materialization_version(source, path)
                conn.execute(
                    "INSERT OR REPLACE INTO materialized_sources(source, imported) "
                    "VALUES(?, COALESCE(?, datetime('now')))", (str(path), imported),
                )
                conn.commit()
        rows.append((path, len(events), skipped))
    conn.close()
    return rows


def iter_events(cfg: config_mod.Config, source: str, *,
                limit: int | None = None) -> Iterator["Event"]:
    """Distill seam (SPEC §7): fresh parse, NO checkpoint reads or writes —
    ingest checkpoints mean "seen", not "distilled". Re-distilling a session is
    safe: postprocess dedupe merges instead of duplicating (ADR-004)."""
    parser = _parser(source)
    for i, path in enumerate(_iter_paths(source, config_mod.load().projects_root
                                         if cfg is None else cfg.projects_root)):
        if limit is not None and i >= limit:
            break
        try:
            yield from parser(path)
        except Exception as exc:  # skip-and-log, never crash on drift (CLAUDE.md)
            log.warning("iter_events skip %s: %s", path, exc)


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def session_text_lookup(harness_name: str, session_id: str,
                        at_ts: str) -> tuple[str, str] | None:
    """Resolve a session_id to its transcript and split its assistant/tool text
    at `at_ts`, returning `(before, after)` (SPEC §10 usage mining; consumed by
    consolidate.mine_servings).

    BOTH halves, from ONE parse. The miner needs the before-text to tell a card
    that taught the session something from a card that merely repeated what the
    session had already said — and a second lookup for it would mean parsing the
    transcript twice per serving, which on this user's 170 MB session is minutes,
    not milliseconds.

    None means the transcript is missing (retry next run — a live session may
    not be flushed/ingested yet); `("", "")` means it exists but produced no text
    (a real judged outcome, mined as ignored).

    Takes the harness explicitly: since ADR-040 the Codex hooks log servings too,
    and the two transcripts live in different roots under different filenames
    (claude `<uuid>.jsonl`; codex `sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl`).
    Resolving every serving against ~/.claude/projects would return None forever
    for the Codex rows — mined as "skipped", re-scanned every night, never judged
    and never counted by the kill-switch."""
    if not session_id:
        return None
    from .. import harness

    if harness_name == "codex":
        from .codex_jsonl import iter_events
        pattern = f"*{session_id}.jsonl"          # rollout-<ts>-<uuid>.jsonl
    else:
        from .claude_jsonl import iter_events     # type: ignore[assignment]
        pattern = f"{session_id}.jsonl"
    root = harness.sessions_dir("codex" if harness_name == "codex" else "claude")
    matches = sorted(root.rglob(pattern)) if root.is_dir() else []
    if not matches:
        return None
    cutoff = _parse_ts(at_ts)
    before: list[str] = []
    after: list[str] = []
    for path in matches:
        try:
            for e in iter_events(path):
                # Assistant/tool activity only — skip the user's own prompts,
                # but keep tool_result records (claude tags them role="user").
                if e.role == "user" and e.meta.get("block_type") != "tool_result":
                    continue
                if not e.text:
                    continue
                et = _parse_ts(e.ts)
                # An undated event cannot be placed on either side of the cut.
                # It goes to `after`, which is where the old after-only lookup
                # put it: counting it as prior context would let a card be
                # written off as "already known" on no evidence.
                (before if cutoff and et and et <= cutoff else after).append(e.text)
        except Exception as exc:  # skip-and-log, never crash on drift (CLAUDE.md)
            log.warning("session_text_lookup skip %s: %s", path, exc)
    return "\n".join(before), "\n".join(after)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m afterwit.adapters")
    p.add_argument("--source", choices=["claude", "codex", "memory", "docs", "db", "all"], required=True)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    try:
        rows = ingest(args.source, limit=args.limit)
    except OSError as exc:
        print(f"ingest failed: {exc}", file=sys.stderr)
        return 1
    total = sum(n for _, n, skipped in rows if not skipped)
    skipped = sum(1 for _, _, was_skipped in rows if was_skipped)
    print(json.dumps({"source": args.source, "files": len(rows), "events": total, "skipped": skipped}))
    for path, count, was_skipped in rows:
        state = "skipped" if was_skipped else "parsed"
        print(json.dumps({"path": str(path), "state": state, "events": count}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
