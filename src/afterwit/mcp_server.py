"""stdio MCP server. SPEC §9.1 — the retrieval surface both harnesses share.

Codex has no per-prompt push channel, so this server is Codex's ENTIRE access
to the knowledge base; Claude Code uses it alongside hook injection.

Contract:
- Registers every dict in `toolspecs.TOOLS` verbatim (name/description/inputSchema
  are load-bearing — they are what makes an agent decide to call a tool).
- Ranking goes through `rank.rank()` ONLY (ADR-006: the single scoring path).
  Superseded cards `why` returns are lineage attached to a scored active card,
  never independently scored.
- `save_insight` can only reach the review queue (ui.queue_insert), never the
  wiki (P6, anti-poisoning); confidence is capped at AGENT_CONFIDENCE_CAP.
- Every result carries card ids for provenance; empty results are a clear
  "no known history — proceed normally" text, never an error.

Handlers are plain sync functions (dispatch) so tests exercise them against a
tmp db with no stdio; the async mcp wrappers just marshal to/from them.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import cards as cards_mod
from . import config as config_mod
from . import embed, index_db, postprocess, rank, ui
from .toolspecs import TOOLS

_EMPTY = "No known history for this — proceed normally."


def _active(rows: list) -> list:
    """Keep only active cards before ranking. rank() drops non-active anyway, but
    leaving superseded rows in pollutes its bm25 min-max normalization and can push
    the surviving active card below the floor (rank.py is frozen — filter here)."""
    return [r for r in rows if r["status"] == "active"]


# ---------------------------------------------------------------- formatting

def _fmt(*, id: str, type: str, title: str, body: str, project: str,
        updated: str, verified: bool, prefix: str = "", path: str | None = None) -> str:
    flag = "" if verified else " UNVERIFIED"
    date = (updated or "")[:10]
    one_line = " ".join((body or "").split())
    prov = f"id: {id} · {project}/{type} · {date}"
    if path:
        prov += f" · {path}"
    return f"{prefix}[{type}{flag}] {title}\n  {one_line}\n  ({prov})"


def _fmt_scored(s: rank.Scored, prefix: str = "") -> str:
    return _fmt(id=s.id, type=s.type, title=s.title, body=s.body, project=s.project,
                updated=s.updated, verified=s.verified, prefix=prefix)


def _fmt_row(r, prefix: str = "") -> str:
    return _fmt(id=r["id"], type=r["type"], title=r["title"], body=r["body"],
                project=r["project"], updated=r["updated"], verified=bool(r["verified"]),
                prefix=prefix, path=r["path"] if "path" in r.keys() else None)


# --------------------------------------------------------------------- tools

def _recall(cfg, conn, args: dict) -> str:
    query = args.get("query") or ""
    project = args.get("project")
    type_ = args.get("type")
    k = min(int(args.get("k", 5) or 5), 10)
    rows = _active(index_db.search(conn, query, project=project, k=20))
    if type_:
        rows = [r for r in rows if r["type"] == type_]
    cosines = embed.cosines(conn, query, [r["id"] for r in rows])
    scored = rank.rank(rows, project, floor=cfg.floor, k=k, query_text=query,
                       cosines=cosines)
    if not scored:
        return _EMPTY
    return "\n\n".join(_fmt_scored(s) for s in scored)


def _lookup_error(cfg, conn, args: dict) -> str:
    error_text = args.get("error_text") or ""
    project = args.get("project")
    rows = _active(index_db.search(conn, error_text, project=project, k=20))
    cosines = embed.cosines(conn, error_text, [r["id"] for r in rows])
    scored = rank.rank(rows, project, floor=cfg.floor, k=5, query_text=error_text,
                       cosines=cosines,
                       # A stack trace is a property of the runtime, not of the
                       # repo it fired in: no cross-project demotion here, same
                       # as `inject --mode error` (see rank.CROSS_PROJECT_FACTOR).
                       cross_project=1.0)
    if not scored:
        return _EMPTY
    # bias error_fix cards to the top (SPEC §9.1); non-error_fix still returned,
    # a fix recorded elsewhere may still match. Stable within each group.
    scored.sort(key=lambda s: s.type != "error_fix")
    return "\n\n".join(_fmt_scored(s) for s in scored)


def _history_rows(conn, card_id: str) -> list:
    """Predecessors of a card: everything it (transitively) superseded, newest
    first. These carry the 'we used X until Y' history (ADR: supersede chain)."""
    hist: list = []
    seen: set[str] = set()
    frontier = [card_id]
    while frontier:
        cur = frontier.pop()
        for r in conn.execute("SELECT * FROM cards WHERE superseded_by=?", (cur,)):
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            hist.append(r)
            frontier.append(r["id"])
    return hist


def _why(cfg, conn, args: dict) -> str:
    topic = args.get("topic") or ""
    project = args.get("project")
    # Score only active decision candidates; superseded decisions are attached
    # afterwards as lineage (not scored) so they never suppress the current one.
    rows = [r for r in _active(index_db.search(conn, topic, project=project, k=20))
            if r["type"] == "decision"]
    cosines = embed.cosines(conn, topic, [r["id"] for r in rows])
    scored = rank.rank(rows, project, floor=cfg.floor, k=5, query_text=topic,
                       cosines=cosines)
    if not scored:
        return _EMPTY
    blocks = []
    for s in scored:
        block = [_fmt_scored(s)]
        for h in _history_rows(conn, s.id):
            block.append(_fmt_row(h, prefix="  ↳ superseded: "))
        blocks.append("\n".join(block))
    return "\n\n".join(blocks)


def _for_file(cfg, conn, args: dict) -> str:
    rows = index_db.for_file(conn, args.get("path") or "", project=args.get("project"))
    if not rows:
        return _EMPTY
    return "\n\n".join(_fmt_row(r) for r in rows)


def _project_brief(cfg, conn, args: dict) -> str:
    project = args.get("project") or ""
    brief = cfg.wiki_root / "projects" / project / "brief.md"
    if brief.exists():
        return brief.read_text(encoding="utf-8").strip() or _EMPTY
    # synthesize: active-card counts by type + top gotchas by usefulness
    counts = conn.execute(
        "SELECT type, COUNT(*) n FROM cards WHERE project=? AND status='active' "
        "GROUP BY type ORDER BY n DESC", (project,),
    ).fetchall()
    if not counts:
        return _EMPTY
    gotchas = conn.execute(
        "SELECT title FROM cards WHERE project=? AND status='active' "
        "AND type IN ('gotcha','error_fix') ORDER BY usefulness DESC, updated DESC LIMIT 3",
        (project,),
    ).fetchall()
    parts = [f"{project}: " + ", ".join(f"{r['n']} {r['type']}s" for r in counts) + "."]
    if gotchas:
        parts.append("Top gotchas: " + "; ".join(r["title"] for r in gotchas) + ".")
    parts.append("Deep history: recall / why / lookup_error / for_file.")
    return "\n".join(parts)


def _related(cfg, conn, args: dict) -> str:
    card_id = args.get("card_id") or ""
    me = conn.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
    if me is None:
        return _EMPTY
    # Same edge semantics as ui._graph: wikilinks resolved by title slug (both
    # directions), supersede chain (both directions), curated related links
    # (ADR-045, both directions), shared-file coupling.
    card_rows = conn.execute("SELECT id, title, project FROM cards").fetchall()
    by_project_slug = {(r["project"], ui._slug(r["title"])): r["id"]
                       for r in card_rows}
    by_slug: dict[str, list[str]] = {}
    for r in card_rows:
        by_slug.setdefault(ui._slug(r["title"]), []).append(r["id"])
    my_files = set(json.loads(me["files"] or "[]"))
    neighbors: dict[str, str] = {}  # id -> edge kind (first wins)

    def add(nid: str | None, kind: str) -> None:
        if nid and nid != card_id:
            neighbors.setdefault(nid, kind)

    def resolve(slug: str, project: str) -> str | None:
        candidates = by_slug.get(slug, [])
        return (by_project_slug.get((project, slug))
                or by_project_slug.get(("global", slug))
                or (candidates[0] if len(candidates) == 1 else None))

    for r in conn.execute("SELECT dst FROM links WHERE src=? AND kind='wikilink'", (card_id,)):
        add(resolve(ui._slug(r["dst"]), me["project"]), "wikilink")
    for r in conn.execute("SELECT src, dst FROM links WHERE kind='wikilink'"):
        src = conn.execute("SELECT project FROM cards WHERE id=?", (r["src"],)).fetchone()
        if src and resolve(ui._slug(r["dst"]), src["project"]) == card_id:
            add(r["src"], "wikilink")
    if me["superseded_by"]:
        add(me["superseded_by"], "supersede")
    for r in conn.execute("SELECT id FROM cards WHERE superseded_by=?", (card_id,)):
        add(r["id"], "supersede")
    # curated links (ADR-045), both directions — ids, no resolution needed.
    # After wikilink/supersede (human-authored beats judged), before file
    # coupling (judged beats incidental).
    for r in conn.execute("SELECT dst FROM links WHERE src=? AND kind='related'", (card_id,)):
        add(r["dst"], "related")
    for r in conn.execute("SELECT src FROM links WHERE dst=? AND kind='related'", (card_id,)):
        add(r["src"], "related")
    if my_files:
        for r in conn.execute("SELECT id, project, files FROM cards WHERE files != '[]'"):
            if (r["id"] != card_id and r["project"] == me["project"]
                    and my_files & set(json.loads(r["files"] or "[]"))):
                add(r["id"], "file")

    if not neighbors:
        return _EMPTY
    out = []
    for nid, kind in neighbors.items():
        row = conn.execute("SELECT * FROM cards WHERE id=?", (nid,)).fetchone()
        if row:
            out.append(_fmt_row(row, prefix=f"[{kind}] "))
    return "\n\n".join(out) if out else _EMPTY


def _save_insight(cfg, conn, args: dict) -> str:
    body = args.get("body") or ""
    why = args.get("why")
    if why and "**Why:**" not in body:
        body = f"{body.rstrip()}\n\n**Why:** {why}"
    today = datetime.now(timezone.utc).date().isoformat()
    card = cards_mod.Card(
        id=cards_mod.new_ulid(),
        type=args.get("type", ""),
        title=args.get("title", ""),
        project=args.get("project", ""),
        status="active",
        body=body,
        # Agent proposals carry no transcript path; provenance is the live
        # session. Honest source keeps validate() happy; the human reviewer
        # sees it is agent-proposed and can attach real sources on approval.
        sources=[{"path": "agent://save_insight", "kind": "assistant"}],
        tags=[str(t) for t in (args.get("tags") or [])],
        files=[str(f) for f in (args.get("files") or [])],
        confidence=postprocess.AGENT_CONFIDENCE_CAP,  # capped — always queued (SPEC §9.1)
        verified=False,
        created=today,
        updated=today,
        # Stamped, not taken from `args`: an agent's claim about which model it is
        # cannot be verified here, and "agent" is the fact we do have (ADR-035).
        distilled_by="agent",
    )
    # Same anchor helper as distill and `afterwit queue` (ADR-020 D5): agent-proposed
    # capability cards used to reach the wiki with no commit anchor at all, so
    # drift for them fell back to the weak existence check forever.
    #
    # But an anchor is metadata, and losing a resolved insight to fetch metadata is
    # a worse outcome than an unanchored card that a human is about to review
    # anyway. This is not hypothetical: afterwit edits its own source while its MCP
    # servers are running, and a server that imported `afterwit.config` before a
    # function was added keeps that stale module in sys.modules forever — so the
    # lazy `from .config import project_dir_name` inside anchor() raises
    # `ImportError: cannot import name 'project_dir_name' from 'afterwit.config'`,
    # naming a file that has the function, for the life of that process. It took
    # out save_insight and nothing else, because it is the only tool that anchors
    # (2026-07-27, ADR-041).
    note = ""
    try:
        from . import gitmeta
        card.source_commit, card.repo_url = gitmeta.anchor(
            cfg.projects_root, card.project, aliases=cfg.project_aliases)
    except (ImportError, AttributeError) as e:
        # Only the stale-process class is swallowed. A real bug in anchoring
        # still raises, because silently unanchoring every card is how ADR-020 D5
        # got written in the first place.
        note = (f" NOTE: saved without a git anchor — this afterwit process is running "
                f"code older than the files on disk ({e}). Restart the harness to "
                f"reload it; the card is safe in the queue either way.")
    card.validate()
    ui.queue_insert(conn, card, "save_insight", cfg.wiki_root)
    return (f"Proposed to your review queue (id: {card.id}). Not trusted until "
            "you approve it in `afterwit ui`, so it will not recall yet." + note)


def _feedback(cfg, conn, args: dict) -> str:
    card_id = args.get("card_id") or ""
    verdict = args.get("verdict") or ""
    row = conn.execute("SELECT path, status FROM cards WHERE id=?", (card_id,)).fetchone()
    if row is None:
        return f"Unknown card id: {card_id}"
    now = datetime.now(timezone.utc).isoformat()
    if verdict == "helpful":
        conn.execute("UPDATE cards SET usefulness = usefulness + 1, last_used = ? WHERE id=?",
                     (now, card_id))
        msg = "Recorded helpful (+1 usefulness)."
    elif verdict == "wrong":
        conn.execute("UPDATE cards SET usefulness = usefulness - 1, status = 'quarantined' WHERE id=?",
                     (card_id,))
        _quarantine_wiki(cfg, conn, card_id, row["path"])
        msg = "Quarantined and penalized — it will no longer be served."
    elif verdict == "stale":
        conn.execute("UPDATE cards SET usefulness = usefulness - 0.5 WHERE id=?", (card_id,))
        msg = "Recorded stale (-0.5 usefulness)."
    else:
        return f"Unknown verdict: {verdict!r}"
    # log_serving-style audit row; outcome pre-set so usage mining (§10) skips it
    # (it is a rating, not a serving to be scored later).
    conn.execute(
        "INSERT INTO servings(ts,harness,session_id,mode,query,card_ids,outcome) "
        "VALUES(?,?,?,?,?,?,?)",
        (now, "mcp", "", "feedback", verdict, json.dumps([card_id]), f"feedback:{verdict}"),
    )
    conn.commit()
    return msg


def _quarantine_wiki(cfg, conn, card_id: str, path: str | None) -> None:
    """Persist quarantine into the wiki (source of truth) so it survives
    `afterwit index --rebuild` (P4). Quarantine, never delete (P5/P6); log for forensics."""
    if path:
        from pathlib import Path
        p = Path(path)
        if p.exists():
            try:
                card = cards_mod.load(p)
                card.status = "quarantined"
                card.updated = datetime.now(timezone.utc).date().isoformat()
                p.write_text(cards_mod.render(card), encoding="utf-8")
            except cards_mod.CardError:
                pass
    log = config_mod.log_path(cfg.wiki_root)  # per-device audit log (ADR-019/020)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as f:
        f.write(f"- {datetime.now(timezone.utc).isoformat()} feedback-quarantine: {card_id}\n")


_ROUTES = {
    "recall": _recall, "lookup_error": _lookup_error, "why": _why,
    "for_file": _for_file, "project_brief": _project_brief, "related": _related,
    "save_insight": _save_insight, "feedback": _feedback,
}


def dispatch(name: str, arguments: dict, cfg=None) -> str:
    """Route one tool call to its handler and return text. Opens (and closes) a
    read-write connection; used directly by tests and by the async call_tool."""
    if name not in _ROUTES:
        return f"Unknown tool: {name}"
    cfg = cfg or config_mod.load()

    # An unreachable index is a CONFIG fault, and it must never be reported as an
    # empty one. index_db.connect() in rw mode does mkdir + executescript(SCHEMA),
    # so a wrong/missing db_path CREATES a blank database and every tool below then
    # answers "no known history — proceed normally". The agent reads that as "the
    # user has no knowledge" and stops asking. Silent wrong answer, no error, and
    # the real 463-card index sits untouched somewhere else. (This is exactly how
    # the ~/.harness_helper → ~/.afterwit rename stranded a long-lived server on a
    # dead path.) Fail loud and actionable instead.
    if not Path(cfg.db_path).exists():
        return (f"afterwit index is UNREACHABLE at {cfg.db_path} — this is not an "
                f"empty knowledge base, it is a broken install. Do not conclude the "
                f"user has no history. Run: afterwit doctor")
    try:
        conn = index_db.connect(cfg.db_path)
    except index_db.IndexUnavailable as e:
        return str(e)  # already carries path, perms, HOME and the remedy
    except sqlite3.Error as e:
        return (f"afterwit index at {cfg.db_path} could not be opened ({e}). "
                f"Not an empty knowledge base — a broken install. Run: afterwit doctor")
    try:
        return _ROUTES[name](cfg, conn, arguments or {})
    except sqlite3.Error as e:
        return (f"afterwit index error at {cfg.db_path} ({e}). Run: afterwit doctor")
    finally:
        conn.close()


# --------------------------------------------------------------- stdio server

def _build_server():
    from mcp.server import Server
    import mcp.types as types

    server = Server("afterwit")

    @server.list_tools()
    async def list_tools() -> list:
        return [types.Tool(name=t["name"], description=t["description"],
                           inputSchema=t["inputSchema"]) for t in TOOLS]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list:
        return [types.TextContent(type="text", text=dispatch(name, arguments))]

    return server


def main() -> int:
    import anyio
    from mcp.server.stdio import stdio_server

    server = _build_server()

    async def _run() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(_run)
    return 0
