"""Canonical ranking. SPEC §8, ADR-006. The ONLY scoring path — MCP tools and
hook injection both call rank(); no second formula may exist.

score = 0.5·bm25 + 0.3·cosine + 0.1·recency + 0.1·usefulness
        (+0.15 same-project) ×0 non-active ×0.5 unverified ×0.5 stale
When no vectors exist yet (P1), the cosine weight folds into bm25.
Results below `floor` are dropped — returning nothing is a first-class result
(Manifesto P3: silence beats noise).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

W_BM25, W_COS, W_REC, W_USE = 0.5, 0.3, 0.1, 0.1
PROJECT_BOOST = 0.15
# A card from ANOTHER project is worth less, and the boost alone could not say so:
# `floor` is absolute, so lifting the home project never pushes a foreign card
# below it. Foreign is worth less, not worthless — cross-project transfer is the
# point of a single store — so this demotes rather than filters, and `global`
# (deliberately cross-cutting) is exempt.
#
# The 74%-vs-43% split this comment used to cite is RETRACTED (ADR-043): it came
# from `card_was_used`, which was then measured at 1.8x separation against
# unrelated sessions. The factor now rests on inspected servings, not on a rate.
# Re-derive it after `afterwit run --remine` before tuning the value.
#
# ERROR LOOKUPS PASS 1.0. Replaying the real servings showed this penalty killing
# exactly one card that had been mined as USED: a `reader-app` card about the
# Node ESM loader ignoring NODE_PATH, matched from another project on an
# `ERR_MODULE_NOT_FOUND` stack trace. That is not leakage, it is the whole reason
# one store spans projects — a stack trace is a property of the runtime, not of
# the repo it fired in. What the penalty should catch is the vague prompt ("make
# the public one commit" pulled cards from two unrelated projects, both ignored),
# and prompts are where project context actually constrains the answer.
CROSS_PROJECT_FACTOR = 0.75
UNVERIFIED_FACTOR = 0.5
# A capability card IS a pointer to living code (ADR-014): if the code moved, the
# pointer may be wrong, so demote it (independent of supersede — no replacement
# card need exist; ADR-018). Demotion, never deletion: it still records that the
# capability existed. Other types are knowledge ABOUT code — editing a cited file
# does not falsify "we chose JSONB because the schema churns", so they are flagged
# by lint for human review but never demoted (measured: 20 of 23 drifted cards).
STALE_FACTOR = 0.5
DEMOTE_STALE_TYPES = frozenset({"capability"})
RECENCY_TAU_DAYS = 90.0
DEFAULT_FLOOR = 0.35  # provisional; tune on eval/golden.yaml before P3 injection (SPEC §12)


@dataclass
class Scored:
    id: str
    title: str
    body: str
    type: str
    project: str
    updated: str
    score: float
    verified: bool = True
    stale: bool = False


def _stale(row) -> bool:
    """Defensive: a readonly connection to a pre-ADR-018 index has no `stale`
    column and cannot be migrated. Absent column → not stale (never demote on
    ignorance)."""
    try:
        return bool(row["stale"])
    except (IndexError, KeyError):
        return False


def _recency(updated_iso: str, now: datetime) -> float:
    try:
        dt = datetime.fromisoformat(updated_iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return 0.0
    age_days = max((now - dt).total_seconds() / 86400.0, 0.0)
    return math.exp(-age_days / RECENCY_TAU_DAYS)


def _normalize_bm25(raws: list[float]) -> list[float]:
    """FTS5 bm25() is smaller-is-better. Min-max onto 0..1, best → 1.

    Min-max alone is relative-only: in a small pool a lone weak match becomes
    1.0 and leaks past the floor (caught by golden no-answer traps 2026-07-06).
    Raw |bm25| can't anchor it either — idf collapses to 0.0 on small corpora.
    The absolute anchor is `_coverage` (query-token overlap), applied in rank()
    when the caller passes query_text. Retune only with `afterwit eval` (SPEC §12)."""
    if not raws:
        return []
    inv = [-r for r in raws]
    lo, hi = min(inv), max(inv)
    if hi - lo < 1e-9:
        return [1.0] * len(inv)
    return [(x - lo) / (hi - lo) for x in inv]


def _coverage(query_text: str, title: str, body: str) -> float:
    """Fraction of distinctive query tokens present in the card (prefix-lenient:
    'truncation' matches 'truncates'). Corpus-size independent — this is what
    separates 'best of good matches' from 'best of incidental matches'."""
    from . import index_db  # shares the stopword list; no cycle (index_db ← cards only)

    toks: list[str] = []
    for w in index_db._WORD.findall(query_text):
        lw = w.lower()
        if lw not in index_db._STOP and lw not in toks:
            toks.append(lw)
        if len(toks) >= 12:
            break
    if not toks:
        return 1.0
    text = f"{title} {body}".lower()
    hit = sum(1 for t in toks if t[:6] in text)
    return hit / len(toks)


def rank(rows, query_project: str | None, *, cosines: dict[str, float] | None = None,
         floor: float = DEFAULT_FLOOR, k: int = 5,
         now: datetime | None = None, query_text: str | None = None,
         cross_project: float = CROSS_PROJECT_FACTOR) -> list[Scored]:
    """rows: sqlite Rows from index_db.search() (need bm25_raw + card columns).

    Pass query_text whenever you have it (all serving paths do): it anchors the
    relative bm25 with absolute token coverage so lone incidental matches can't
    leak past the floor. Omitting it keeps pure min-max (legacy/aggregate use)."""
    now = now or datetime.now(timezone.utc)
    rows = list(rows)
    bm25 = _normalize_bm25([r["bm25_raw"] for r in rows])
    out: list[Scored] = []
    for r, b in zip(rows, bm25):
        if r["status"] != "active":
            continue
        if query_text is not None:
            coverage = _coverage(query_text, r["title"], r["body"])
            # Quadratic anchoring makes a one-of-two incidental match weak
            # enough to stay silent, while multi-token project queries can still
            # pass via two strong terms plus the explicit project boost.
            b *= coverage * coverage
        cos = (cosines or {}).get(r["id"])
        if cos is None:
            base = (W_BM25 + W_COS) * b  # fold cosine weight into bm25 (P1)
        else:
            base = W_BM25 * b + W_COS * max(cos, 0.0)
        s = (base
             + W_REC * _recency(r["updated"], now)
             + W_USE * min(max(r["usefulness"] or 0.0, 0.0) / 5.0, 1.0))
        if query_project and r["project"] == query_project:
            s += PROJECT_BOOST
        elif query_project and r["project"] != "global":
            s *= cross_project
        if not r["verified"]:
            s *= UNVERIFIED_FACTOR
        drifted = _stale(r)
        if drifted and r["type"] in DEMOTE_STALE_TYPES:
            s *= STALE_FACTOR
        if s >= floor:
            out.append(Scored(r["id"], r["title"], r["body"], r["type"],
                              r["project"], r["updated"], round(s, 4),
                              bool(r["verified"]), drifted))
    out.sort(key=lambda x: x.score, reverse=True)
    return out[:k]
