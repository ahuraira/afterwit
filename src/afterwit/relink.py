"""Curated card-to-card links: kNN candidates → LLM judge → validated write.
ADR-045.

89% of active cards have no edge at all; file coupling is capped at 23% by
construction (77% of cards cite no file) and the distiller cannot emit
wikilinks because it sees one session, never the corpus. The one signal every
card carries is its text, and vectors are already stored — so kNN over them
proposes, and an LLM judges. kNN output is NEVER surfaced raw: it is a
candidate generator only, and a candidate becomes an edge only when the judge
keeps it.

Why these links land without per-link review (the P6 carve-out ADR-045
records): the judge selects from a CLOSED SET of candidate ids — it cannot
invent a target — every pick is validated against the live index before
writing, the write goes to a machine-owned `related:` frontmatter key (never
into body prose), and `afterwit relink --strip` erases the entire class in one
command. Unreviewed because reversible-as-a-class; card CONTENT stays fully
review-gated.

Wiki markdown stays the source of truth (P4): `related:` lives in frontmatter,
`index_db.upsert_card` mirrors it into `links(kind='related')`, so
`afterwit index --rebuild` is lossless. The `relinked` table below is
operational memo state like `servings` — losing it costs re-judging, never
correctness.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Callable

from . import cards as cards_mod
from . import embed, index_db

MAX_RELATED = 3
# The judge sees more than it may keep: kNN recall is the ceiling on link
# recall, and widening the candidate set is the cheap way to raise it — the
# judge prunes for free, extra candidates cost prompt tokens only.
CANDIDATES_K = 8
# Candidate floor, not an edge floor: the judge is the precision layer, this
# only keeps obviously-unrelated vectors out of the prompt. Tune from the
# hand-judged sweep (`afterwit relink --dry-run`), not by taste.
COS_FLOOR = 0.35
# Above this the pair is a near-duplicate, not a relation. The 36-card sweep
# surfaced identical-text cards at cos 1.00/0.96/0.94 — the judge dutifully
# linked them, which papers over what is really dedupe/supersede work
# (postprocess owns that). 0.92 splits the sample's duplicate cluster (>=0.94)
# from its strongest real relation (0.89: a gotcha and the decision that
# softened it). Imperfect by nature — one curly-quote dup sat at 0.90 — but the
# judge rejected those anyway.
DUP_CEILING = 0.92

DDL = """
CREATE TABLE IF NOT EXISTS relinked(
  id TEXT PRIMARY KEY,
  ts TEXT
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(DDL)


def _load_prompt() -> str:
    p = Path(__file__).resolve().parents[2] / "prompts" / "relink.md"
    return p.read_text(encoding="utf-8")


def candidates(conn: sqlite3.Connection, card_id: str,
               k: int = CANDIDATES_K) -> list[tuple[str, float]]:
    """Top-k active neighbours by cosine over STORED vectors — no model load.

    This is the closed set the judge selects from. Excludes self, non-active
    cards, and ids the card already links as `related`."""
    me = conn.execute("SELECT vec FROM vectors WHERE id=?", (card_id,)).fetchone()
    if me is None:
        return []
    my_vec = embed._vec(me["vec"])
    linked = {r["dst"] for r in conn.execute(
        "SELECT dst FROM links WHERE src=? AND kind='related'", (card_id,))}
    out: list[tuple[str, float]] = []
    for r in conn.execute(
            """SELECT v.id, v.vec FROM vectors v JOIN cards c ON c.id = v.id
               WHERE c.status='active' AND v.id != ?""", (card_id,)):
        if r["id"] in linked:
            continue
        cos = embed._cos(my_vec, embed._vec(r["vec"]))
        if COS_FLOOR <= cos <= DUP_CEILING:
            out.append((r["id"], cos))
    out.sort(key=lambda t: t[1], reverse=True)
    return out[:k]


def _parse_ids(raw: str) -> list[str]:
    """Ids from the judge's reply, response order preserved.

    Slice to the outermost JSON array BEFORE extracting anything: a judge that
    narrates its rejections mentions rejected ids in prose, and a bare regex
    over the whole reply would keep them. Anything outside the brackets is
    ignored; no brackets means no links."""
    s = raw.strip()
    i, j = s.find("["), s.rfind("]")
    if i < 0 or j <= i:
        return []
    s = s[i:j + 1]
    try:
        arr = json.loads(s)
        if isinstance(arr, list):
            return [str(x) for x in arr]
    except ValueError:
        pass
    return re.findall(r"[0-9A-HJKMNP-TV-Z]{26}", s)


def _fmt(row: sqlite3.Row) -> str:
    body = " ".join((row["body"] or "").split())[:400]
    return f"[{row['id']}] ({row['type']}, {row['project']}) {row['title']}\n{body}"


def judge(driver: Callable[[str], str], card_row: sqlite3.Row,
          cand_rows: list[sqlite3.Row], template: str) -> list[str]:
    """Closed-set selection: whatever the reply says, only offered candidate
    ids survive, capped at MAX_RELATED. This intersection is the property that
    makes the unreviewed write safe — fabrication is structurally impossible."""
    offered = {r["id"] for r in cand_rows}
    prompt = (template
              .replace("{card}", _fmt(card_row))
              .replace("{candidates}", "\n\n".join(_fmt(r) for r in cand_rows)))
    picked = _parse_ids(driver(prompt))
    return [i for i in dict.fromkeys(picked) if i in offered][:MAX_RELATED]


def validate(conn: sqlite3.Connection, src: str, ids: list[str]) -> list[str]:
    """Last gate before the write: target exists, is active, is not the source.

    The judge already saw only live candidates, but a card can be superseded or
    quarantined between candidate query and write — never link to a dead id."""
    out: list[str] = []
    for i in dict.fromkeys(ids):
        if i == src:
            continue
        row = conn.execute("SELECT status FROM cards WHERE id=?", (i,)).fetchone()
        if row and row["status"] == "active":
            out.append(i)
    return out[:MAX_RELATED]


def _write(conn: sqlite3.Connection, card_id: str, ids: list[str]) -> None:
    """Rewrite the card file in place (never cards.save — a slug collision
    there would rename the file) and mirror into the index."""
    row = conn.execute("SELECT path FROM cards WHERE id=?", (card_id,)).fetchone()
    path = Path(row["path"])
    card = cards_mod.load(path)
    card.related = ids
    path.write_text(cards_mod.render(card), encoding="utf-8")
    index_db.upsert_card(conn, card, str(path))


def _mark(conn: sqlite3.Connection, card_id: str) -> None:
    conn.execute("INSERT OR REPLACE INTO relinked(id, ts) VALUES(?, datetime('now'))",
                 (card_id,))


def relink(conn: sqlite3.Connection, driver: Callable[[str], str], *,
           budget: int, dry_run: bool = False) -> dict:
    """Judge up to `budget` unjudged cards; write kept links; memo the rest.

    A card is memoed even when nothing is kept — "judged, no links" is a
    result, and without the memo the nightly would re-buy the same LLM call
    forever. dry_run is side-effect-free: no writes, no memo, so a later real
    run judges the same cards again."""
    ensure_schema(conn)
    template = _load_prompt()
    eligible = conn.execute(
        """SELECT c.id FROM cards c JOIN vectors v ON v.id = c.id
           WHERE c.status='active'
             AND c.id NOT IN (SELECT id FROM relinked)
             AND c.id NOT IN (SELECT DISTINCT src FROM links WHERE kind='related')
           ORDER BY c.updated DESC LIMIT ?""", (budget,)).fetchall()
    judged = linked = edges = 0
    proposals: list[dict] = []
    for r in eligible:
        cid = r["id"]
        cands = candidates(conn, cid)
        kept: list[str] = []
        if cands:
            card_row = conn.execute("SELECT * FROM cards WHERE id=?", (cid,)).fetchone()
            cand_rows = [conn.execute("SELECT * FROM cards WHERE id=?", (i,)).fetchone()
                         for i, _ in cands]
            kept = validate(conn, cid, judge(driver, card_row, cand_rows, template))
        judged += 1
        if dry_run:
            proposals.append({"id": cid, "kept": kept,
                              "candidates": [i for i, _ in cands]})
            continue
        if kept:
            _write(conn, cid, kept)
            linked += 1
            edges += len(kept)
        _mark(conn, cid)
        # per-card, not per-run: the judge is an LLM call that can die mid-batch,
        # and an end-of-loop commit would throw away every finished card's write
        # and memo — re-buying those judge calls on the next nightly.
        conn.commit()
    conn.commit()
    out: dict[str, object] = {"judged": judged, "linked": linked, "edges": edges}
    if dry_run:
        out["proposals"] = proposals
    return out


def strip(conn: sqlite3.Connection, wiki_root: Path) -> int:
    """One-command quarantine of the whole auto-link class (ADR-045): clear
    every `related:` key, re-index, forget the memo so a re-enabled relink
    starts from scratch."""
    n = 0
    for path, card in cards_mod.iter_cards(wiki_root):
        if card.related:
            card.related = []
            path.write_text(cards_mod.render(card), encoding="utf-8")
            index_db.upsert_card(conn, card, str(path))
            n += 1
    ensure_schema(conn)
    conn.execute("DELETE FROM relinked")
    conn.commit()
    return n


def run_stage(cfg, conn: sqlite3.Connection) -> dict:
    """Nightly entrypoint. Reuses the distill driver settings — the judge is a
    small prompt, not a second model to configure."""
    from . import distill

    driver = distill.make_driver(cfg.distill_driver, model=cfg.distill_model,
                                 effort=cfg.distill_effort)
    return relink(conn, driver, budget=cfg.relink_budget)
