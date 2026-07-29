"""Consolidation primitives: usage mining, decay, kill-switch, lint. SPEC §10.

This module holds the judgment; the nightly runner (P4 wave) just sequences:
ingest → distill → postprocess/wiki-write → index → mine_servings →
apply_decay → check_killswitch → lint → briefs.

Usage mining is deliberately simple (Manifesto P9/P10): a served card counts as
USED if what the card ADDED — its body tokens, less its title, less what the
query and the session already said before it arrived — turns up in the
assistant/tool activity afterwards. Cheap, explainable, tunable.

It is not simple in the way it was until 2026-07-29, which asked only whether
the title's words showed up later and answered yes for 95.7% of real servings
and 54.0% of unrelated ones. Every rule here is judged by that comparison —
own-session rate against held-out rate — and `tests/test_consolidate.py` keeps
the falsification cases. Do not replace with an LLM judge unless measured
precision on real transcripts demands it.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import cards as cards_mod
from .config import project_dir_name

USED_DELTA = 1.0
IGNORED_DELTA = -0.2
DECAY_AFTER_DAYS = 180
DECAY_FACTOR = 0.5          # rank penalty for long-unused cards; never deletion
KILLSWITCH_WINDOW_DAYS = 30
KILLSWITCH_MIN_SERVINGS = 20  # below this, not enough evidence to judge
KILLSWITCH_HIT_RATE = 0.20

_WORD = re.compile(r"[A-Za-z0-9_.]{3,}")
_COMMON = frozenset(
    "the and for with use not this that from into when fix error use using".split()
)


BODY_TOKEN_CAP = 25
MIN_NOVEL_TOKENS = 3
NOVEL_HIT_FRACTION = 0.7


def _norm(word: str) -> str:
    """`_WORD` has to admit `.` — `127.0.0.1` and `dom-splitter.ts` are exactly the
    tokens worth matching on. The cost is that a word ending a sentence comes out
    as `writes.`, which then matches nothing, so every card's LAST token per
    sentence was silently dead weight. Interior dots survive; edge dots do not."""
    return word.lower().strip(".")


def distinctive_tokens(title: str) -> set[str]:
    toks = {_norm(w) for w in _WORD.findall(title)}
    return {t for t in toks if len(t) >= 3 and t not in _COMMON}


def _body_tokens(body: str, cap: int = BODY_TOKEN_CAP) -> list[str]:
    out: list[str] = []
    for w in _WORD.findall(body):
        lw = _norm(w)
        if len(lw) >= 3 and lw not in _COMMON and lw not in out:
            out.append(lw)
        if len(out) >= cap:
            break
    return out


def card_was_used(title: str, body: str, later_text: str,
                  prior_text: str, query: str) -> bool:
    """Did this card TEACH the session something it did not already have?

    NOT "do the card's words appear later" — that question cannot be answered by
    matching, and the version that tried scored 95.7% of real card-servings `used`
    while scoring 54.0% of them `used` against a RANDOM UNRELATED session. A signal
    that fires on half the corpus by accident is not a signal, and it was the one
    feeding both the `usefulness` rank term and the kill switch.

    So the question is asked as a difference instead. The card's own body tokens,
    minus its title (the title is why it MATCHED — of course it echoes the query),
    minus everything already in the query, minus everything the session had already
    said BEFORE the card arrived. What is left is what the card alone could have
    contributed. If most of that shows up afterwards, the agent acted on it.

    Measured on 187 real card-servings, own-session rate against the same rule run
    on an unrelated session's text (2026-07-29):

        rule                     own    held-out   lift
        title >=60% (old)      95.7%      54.0%    1.8x
        body >=3                44.4%      11.8%    3.8x
        body >=3 & frac >=0.5   44.4%       9.6%    4.6x
        body >=3 & frac >=0.7   43.9%       5.9%    7.5x   <- chosen
        body >=4 & frac >=0.7   40.1%       5.3%    7.5x
        body >=5 & frac >=0.7   33.7%       4.3%    7.9x

    Every high-precision variant lands at 7.5-8.0x, so the choice is recall:
    `>=3 & 0.7` buys 10 points of it for 1.6 points of held-out noise. Also
    measured and REJECTED: gating on the title as well (`title >=60% AND body
    >=3`) removed no own-session positives at all and let MORE held-out ones
    through — the title genuinely carries no evidence here.

    (An earlier pass of this table read 12.6x. It was measured through the
    trailing-period bug `_norm` now fixes, which suppressed held-out matches more
    than own-session ones and flattered the result. 7.5x is the honest figure.)

    `prior_text` and `query` are required, not defaulted. A caller that silently
    omitted them would get the old, undiscriminating behaviour back with no test
    able to see it (ADR Gotcha #79).
    """
    lt = later_text.lower()
    already = f"{prior_text}\n{query}".lower()
    title_toks = distinctive_tokens(title)
    novel = [t for t in _body_tokens(body)
             if t not in title_toks and t not in already]
    if len(novel) >= MIN_NOVEL_TOKENS:
        hits = sum(1 for t in novel if t in lt)
        if hits / len(novel) >= NOVEL_HIT_FRACTION:
            return True
    # error_fix shape: the literal fix fragment turning up verbatim. Kept, but
    # novelty-gated like everything else — a fix already quoted in the failing
    # output is not evidence the card is what put it there.
    m = re.search(r"Fix:?\s*(.{12,80})", body, re.IGNORECASE)
    frag = m.group(1).strip().lower()[:40] if m else ""
    return bool(frag and frag in lt and frag not in already)


def explicit_outcome(conn: sqlite3.Connection, card_id: str, ts: str) -> bool | None:
    """The verdict the agent gave on purpose, if it gave one. None = it did not.

    `feedback` (MCP) writes a `mode='feedback'` row carrying `helpful`/`stale`/
    `wrong` (mcp_server._feedback). That is ground truth about the same card the
    token heuristic can only guess at, so where it exists it REPLACES the guess —
    scoped to the window between this serving and the next serving of the same
    card, so a rating always lands on the exposure that earned it.

    Substituted, never added. These rows are positive-only in practice (an agent
    calls `feedback` when a card helped, almost never when it did not: 28 of 28
    real rows are `helpful`), so counting them ON TOP of the mined outcomes would
    inflate the kill-switch hit rate — biasing a gate whose entire job is to
    notice that push has stopped earning its slot.
    """
    like = f"%{card_id}%"
    nxt = conn.execute(
        "SELECT MIN(ts) t FROM servings WHERE mode IN ('inject','error') "
        "AND ts > ? AND card_ids LIKE ?", (ts, like)).fetchone()["t"]
    sql = ("SELECT outcome FROM servings WHERE mode='feedback' AND ts > ? "
           "AND card_ids LIKE ?")
    params: tuple = (ts, like)
    if nxt:
        sql += " AND ts < ?"
        params += (nxt,)
    rows = conn.execute(sql + " ORDER BY ts", params).fetchall()
    if not rows:
        return None
    return rows[-1]["outcome"] == "feedback:helpful"  # `stale`/`wrong` are not uses


def mine_servings(conn: sqlite3.Connection, session_text_lookup) -> dict[str, int]:
    """Score unmined servings. `session_text_lookup(harness, session_id, ts)
    -> (before, after) | None` splits the session's assistant/tool text at the
    serving (adapter-provided, so this module stays transcript-format-agnostic).
    Returns counts.

    Both halves, because `card_was_used` judges what the card ADDED: the text
    before the serving is the control for the text after it.

    `harness` is passed through, not assumed: Codex servings resolve against a
    different transcript root and filename shape (ADR-040)."""
    counts = {"used": 0, "ignored": 0, "skipped": 0}
    rows = conn.execute(
        "SELECT id, ts, harness, session_id, query, card_ids FROM servings "
        "WHERE outcome IS NULL"
    ).fetchall()
    for r in rows:
        split = session_text_lookup(r["harness"], r["session_id"], r["ts"])
        if split is None:  # transcript gone/not yet ingested — retry next night
            counts["skipped"] += 1
            continue
        prior, later = split
        outcomes = []
        for cid in json.loads(r["card_ids"]):
            card = conn.execute(
                "SELECT title, body FROM cards WHERE id=?", (cid,)
            ).fetchone()
            if card is None:
                continue
            stated = explicit_outcome(conn, cid, r["ts"])
            used = (stated if stated is not None
                    else card_was_used(card["title"], card["body"], later,
                                       prior, r["query"] or ""))
            delta = USED_DELTA if used else IGNORED_DELTA
            conn.execute(
                "UPDATE cards SET usefulness = usefulness + ?, "
                "last_used = CASE WHEN ? THEN ? ELSE last_used END WHERE id=?",
                (delta, used, r["ts"], cid),
            )
            counts["used" if used else "ignored"] += 1
            outcomes.append("used" if used else "ignored")
        conn.execute("UPDATE servings SET outcome=? WHERE id=?",
                     (",".join(outcomes) or "empty", r["id"]))
    conn.commit()
    return counts


def reset_usage(conn: sqlite3.Connection) -> dict[str, int]:
    """Discard every MINED usage verdict so the next run re-judges from scratch.

    Required whenever `card_was_used` changes, because usefulness is cumulative
    (`usefulness = usefulness + ?`): a corrected miner otherwise stacks its
    verdicts on top of the broken miner's and the old error is permanent. On
    2026-07-29 a held-out control measured 67 of 69 `used` verdicts as false
    positives — those deltas had to go, not be diluted.

    Explicit `feedback` rows are KEPT: they are ground truth rather than
    inference, they carry their own pre-set outcome, and `explicit_outcome`
    replays them onto the right serving on the next mine. Frontmatter catches up
    on the next `write_back_usage` (the wiki is the source of truth, P4)."""
    n_cards = conn.execute("UPDATE cards SET usefulness=0, last_used=NULL").rowcount
    n_srv = conn.execute(
        "UPDATE servings SET outcome=NULL WHERE mode IN ('inject','error')").rowcount
    conn.commit()
    return {"cards": n_cards, "servings": n_srv}


def apply_decay(conn: sqlite3.Connection, now: datetime | None = None) -> int:
    """Cards active but unserved/unused for DECAY_AFTER_DAYS lose rank weight.
    Applied to positive usefulness only — never pushes below zero, never deletes."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=DECAY_AFTER_DAYS)).isoformat()
    cur = conn.execute(
        """UPDATE cards SET usefulness = usefulness * ?
           WHERE status='active' AND usefulness > 0
             AND (last_used IS NULL OR last_used < ?) AND updated < ?""",
        (DECAY_FACTOR, cutoff, cutoff),
    )
    conn.commit()
    return cur.rowcount


def write_back_usage(conn: sqlite3.Connection, wiki_root: Path) -> int:
    """Checkpoint DB usage counters (usefulness, last_used) into card frontmatter.

    Makes the wiki self-contained: `afterwit index --rebuild` and cross-device git
    sync carry earned scores instead of zeroing them (P4, ADR-008). Run after
    mine_servings/apply_decay and before `afterwit sync`. Skips unchanged cards to
    avoid git churn."""
    n = 0
    for r in conn.execute("SELECT path, usefulness, last_used FROM cards").fetchall():
        p = Path(r["path"])
        if not p.exists():
            continue
        try:
            card = cards_mod.load(p)
        except cards_mod.CardError:
            continue
        u = round(r["usefulness"] or 0.0, 2)
        if card.usefulness == u and card.last_used == r["last_used"]:
            continue
        card.usefulness, card.last_used = u, r["last_used"]
        # This is a card WRITE, so it is a sanitize boundary too (ADR-022). It
        # rewrites in place via render() rather than save() to preserve the path,
        # so sanitize explicitly — this is also the path that lazily scrubs legacy
        # cards (e.g. the ~ migration) as their usage counters change.
        cards_mod.sanitize(card)
        p.write_text(cards_mod.render(card), encoding="utf-8")
        n += 1
    return n


def killswitch_status(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """30-day injection hit-rate. Manifesto P9: if injection can't prove value,
    it turns itself off (flag file checked by inject.py)."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=KILLSWITCH_WINDOW_DAYS)).isoformat()
    rows = conn.execute(
        "SELECT outcome FROM servings WHERE mode='inject' AND ts >= ? AND outcome IS NOT NULL",
        (cutoff,),
    ).fetchall()
    served = used = 0
    for r in rows:
        for o in r["outcome"].split(","):
            if o in ("used", "ignored"):
                served += 1
                used += o == "used"
    rate = (used / served) if served else None
    return {
        "served": served, "used": used, "hit_rate": rate,
        "disable": served >= KILLSWITCH_MIN_SERVINGS and rate is not None
                   and rate < KILLSWITCH_HIT_RATE,
    }


def enforce_killswitch(conn: sqlite3.Connection, flag_path: Path,
                       now: datetime | None = None) -> bool:
    st = killswitch_status(conn, now)
    if st["disable"]:
        # `.1%`, and "card-servings", both load-bearing. The 2026-07-26 trip
        # rendered 15/76 = 19.74% as "hit rate 20% over 76 servings (threshold
        # 20%)" — a message that reads as "fired at exactly the threshold", i.e.
        # as a bug, when it was a real one-card miss. And `served` counts card
        # outcomes, not prompts: those 76 came from 40 injections. A gate whose
        # own report cannot be reconciled with the table is not auditable.
        flag_path.write_text(
            f"auto-disabled {datetime.now(timezone.utc).isoformat()}: "
            f"hit rate {st['hit_rate']:.1%} over {st['served']} card-servings "
            f"(threshold {KILLSWITCH_HIT_RATE:.0%}). Delete this file to re-enable.\n"
        )
        return True
    return False


def _resolve_project(projects_root: Path, slug: str, repo_url: str | None,
                     by_url: dict[str, Path],
                     aliases: dict[str, str] | None = None) -> Path | None:
    """Where this project lives ON THIS DEVICE (ADR-020).

    The card's `repo_url` wins over its `slug`: the folder name is whatever the
    machine that wrote the card happened to use, while the remote is the same
    everywhere. Falling back to the slug keeps non-git and remote-less projects
    working. (The ADR-018 version compared `path.name == slug`, which could only
    succeed when `projects_root/slug` already resolved — dead code.)"""
    if repo_url and repo_url in by_url:
        return by_url[repo_url]
    base = projects_root / project_dir_name(slug, aliases)
    return base if base.is_dir() else None


def _dead_pointer(base: Path, files: list[str]) -> bool:
    """True when ANY cited path is unusable: it escapes the project directory
    (absolute path, or `../`), or it does not exist (ADR-020).

    Escape matters because `Path('/proj') / '/tmp/x'` is `/tmp/x` — a live
    scratch file outside the repo would otherwise read as a valid pointer and,
    being untracked, never appear in a diff either. Any-not-all, because a card
    is only as good as its weakest pointer."""
    root = base.resolve()
    for f in files:
        try:
            target = (base / f).resolve()
        except (OSError, RuntimeError):
            return True
        if not target.is_relative_to(root) or not target.exists():
            return True
    return False


def mark_stale(conn: sqlite3.Connection, projects_root: Path,
               aliases: dict[str, str] | None = None) -> list[str]:
    """Recompute code drift from git and persist it to `cards.stale` (ADR-018).

    A card is stale when EITHER signal fires — they catch different failures and
    neither subsumes the other:

      * **moved**: a cited file changed between the commit the card's claims were
        true at (`source_commit`) and current HEAD. Only a diff sees this — the
        file still exists, it was rewritten underneath the card.
      * **dead pointer**: any cited path escapes the project or does not exist.
        Only an existence check sees this — a path that was never tracked (a
        scratch `/tmp` path, a typo, a file from another project) never appears
        in any diff.

    Fallbacks — never crash, never guess:
      * no source_commit, or git cannot resolve it (shallow clone, sha authored
        on another device) → the dead-pointer check alone.
      * project not checked out on this device → skip; absence here is not drift.

    This is the ONLY writer of `cards.stale`; it runs at lint time, never on the
    serving path. Returns the stale card ids.
    """
    from . import gitmeta

    rows = conn.execute(
        "SELECT id, project, files, source_commit, repo_url FROM cards "
        "WHERE status='active' AND project != 'global' AND files != '[]'"
    ).fetchall()

    by_url = gitmeta.discover(projects_root)  # repo_url -> local checkout (ADR-020)
    diff_cache: dict[tuple[str, str], set[str] | None] = {}
    drift: list[str] = []
    for r in rows:
        files = json.loads(r["files"] or "[]")
        if not files:
            continue
        base = _resolve_project(projects_root, r["project"], r["repo_url"], by_url,
                                aliases)
        if base is None:
            continue  # not on this device — silence, not drift
        sha = r["source_commit"]
        changed: set[str] | None = None
        if sha:
            key = (str(base), sha)
            if key not in diff_cache:
                diff_cache[key] = gitmeta.changed_files(base, sha)
            changed = diff_cache[key]
        moved = changed is not None and bool(set(files) & changed)
        if moved or _dead_pointer(base, files):
            drift.append(r["id"])

    conn.execute("UPDATE cards SET stale=0")
    conn.executemany("UPDATE cards SET stale=1 WHERE id=?", [(i,) for i in drift])
    conn.commit()
    return drift


def backfill_anchors(conn: sqlite3.Connection, projects_root: Path,
                     aliases: dict[str, str] | None = None) -> int:
    """Anchor cards written before ADR-018/020 with `source_commit` (the commit
    that was HEAD on their `created` date) and `repo_url`, so drift and
    cross-device resolution work retroactively rather than only for cards written
    from now on. Runs as a nightly stage — it is idempotent and cheap once the
    corpus is anchored (it only selects unanchored rows).

    Skips: `global` cards, cards citing no files, projects absent on this device,
    and cards predating the repo's first commit (those keep the dead-pointer
    fallback). Writes frontmatter first — the wiki is the source of truth (P4) —
    then the index. `updated` is deliberately NOT bumped: anchoring is metadata,
    not a knowledge change, and bumping it would perturb recency ranking."""
    from . import gitmeta

    rows = conn.execute(
        "SELECT id, project, created, path, source_commit, repo_url FROM cards "
        "WHERE ((source_commit IS NULL OR source_commit = '') "
        "       OR (repo_url IS NULL OR repo_url = '')) "
        "AND project != 'global' AND files != '[]'"
    ).fetchall()

    at_cache: dict[tuple[str, str], str | None] = {}
    url_cache: dict[str, str | None] = {}
    n = 0
    for r in rows:
        base = projects_root / project_dir_name(r["project"], aliases)
        if not base.is_dir() or not r["created"] or not r["path"]:
            continue
        if r["project"] not in url_cache:
            url_cache[r["project"]] = gitmeta.remote_url(base)
        key = (r["project"], r["created"])
        if key not in at_cache:
            at_cache[key] = gitmeta.commit_at(base, r["created"])
        sha = r["source_commit"] or at_cache[key]
        url = r["repo_url"] or url_cache[r["project"]]
        if not sha and not url:
            continue
        path = Path(r["path"])
        try:
            card = cards_mod.parse(path.read_text(encoding="utf-8"), path=str(path))
        except (OSError, cards_mod.CardError):
            continue  # unreadable/invalid card — lint reports it separately
        if card.source_commit == sha and card.repo_url == url:
            continue  # already anchored in frontmatter; nothing to write
        card.source_commit, card.repo_url = sha, url
        path.write_text(cards_mod.render(card), encoding="utf-8")
        conn.execute("UPDATE cards SET source_commit=?, repo_url=? WHERE id=?",
                     (sha, url, r["id"]))
        n += 1
    conn.commit()
    return n


def lint(conn: sqlite3.Connection, now: datetime | None = None,
         projects_root: Path | None = None,
         aliases: dict[str, str] | None = None) -> dict:
    """Wiki health: broken wikilinks, stale unverified cards, code drift.
    Returns findings; caller writes them to log.md / review queue.

    Passing `projects_root` recomputes code drift from git first (mark_stale);
    omitting it reports the flag as last persisted."""
    now = now or datetime.now(timezone.utc)
    if projects_root:
        mark_stale(conn, projects_root, aliases)
    known_titles = {
        r["title"].lower() for r in conn.execute("SELECT title FROM cards")
    }
    known_slugs = {
        re.sub(r"[^a-z0-9]+", "-", t).strip("-") for t in known_titles
    }
    broken = [
        (r["src"], r["dst"]) for r in conn.execute(
            "SELECT src, dst FROM links WHERE kind='wikilink'")
        if r["dst"].lower() not in known_titles
        and re.sub(r"[^a-z0-9]+", "-", r["dst"].lower()).strip("-") not in known_slugs
    ]
    stale_cutoff = (now - timedelta(days=30)).isoformat()
    stale = [r["id"] for r in conn.execute(
        "SELECT id FROM cards WHERE verified=0 AND status='active' AND created < ?",
        (stale_cutoff,),
    )]
    # code drift: computed from git by mark_stale() and persisted; flag, never delete.
    drift = [r["id"] for r in conn.execute(
        "SELECT id FROM cards WHERE stale=1 AND status='active'")]
    return {"broken_links": broken, "stale_unverified": stale, "code_drift": drift}
