"""Post-distillation pipeline: dedupe → supersede → confidence gate. SPEC §7, ADR-004/006.

Deterministic code, not LLM. Input: candidate card dicts from the distiller.
Output: explicit actions the wiki writer executes — nothing is written here.

Actions:
  ("write", card)                 deterministic/trusted callers only
  ("merge", existing_id, card)    duplicate — union sources into existing, keep newer body
  ("supersede", existing_id, card) confirmed contradiction — retire old, write new
  ("queue", card, reason)         human review (low confidence / unconfirmed supersede)

P1 similarity is a lexical proxy (difflib); P4 swaps in embedding cosine via the
`similarity` parameter without touching this logic. Auto-supersede happens ONLY
with a confirming callable (LLM yes/no); without one, contradiction candidates
are queued — never auto-applied (Manifesto P5/P6).

Thresholds are calibrated per similarity function. Measured on real contradictory
decision pairs ("JSONB because churn" vs "typed columns because stabilized"):
lexical ratio ≈ 0.65, so the spec's embedding band (0.75, 0.92) misses them —
the lexical band starts at 0.55. A too-low floor only costs review-queue noise;
a too-high floor silently accumulates contradictions (the expensive failure).
P4 embedding callers pass SUPERSEDE_BAND_EMBEDDING.
"""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Callable

from .cards import Card

DUP_THRESHOLD = 0.92
# Paraphrase-dup signal the difflib body proxy misses. Audit 2026-07-06: eight
# rewordings of ONE preference (a multi-agent fan-out relayed the same human
# line into 8 subagent transcripts) all scored <0.55 lexically and were written
# as parallel active cards. Distinctive-title-token overlap catches rewordings.
TITLE_DUP_OVERLAP = 0.75
SUPERSEDE_BAND = (0.55, 0.92)            # lexical-proxy default (P1)
SUPERSEDE_BAND_EMBEDDING = (0.75, 0.92)  # per SPEC §7 / ADR-006, for P4 cosine
CONFIDENCE_GATE = 0.8
AGENT_CONFIDENCE_CAP = 0.79  # save_insight writes always queue (SPEC §9.1)

Action = tuple


def lexical_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def title_overlap(a: str, b: str) -> float:
    """Distinctive-token overlap of two titles, 0..1 (max-normalized)."""
    from .consolidate import distinctive_tokens

    ta, tb = distinctive_tokens(a), distinctive_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(len(ta), len(tb))


def _card_text(c: Card | dict) -> str:
    if isinstance(c, Card):
        return f"{c.title}\n{c.body}"
    return f"{c.get('title','')}\n{c.get('body','')}"


def process(
    candidates: list[Card],
    existing: list[Card],
    *,
    similarity: Callable[[str, str], float] | None = None,
    confirm_contradiction: Callable[[Card, Card], bool] | None = None,
    from_agent: bool = False,
    supersede_band: tuple[float, float] = SUPERSEDE_BAND,
) -> list[Action]:
    sim = similarity or lexical_similarity
    actions: list[Action] = []
    # candidates also dedupe against each other as they are accepted
    accepted: list[Card] = []

    for cand in candidates:
        try:
            cand.validate()
        except Exception as e:
            actions.append(("queue", cand, f"invalid: {e}"))
            continue

        if from_agent:
            cand.confidence = min(cand.confidence, AGENT_CONFIDENCE_CAP)

        pool = [c for c in existing if c.project in (cand.project, "global")] + accepted
        best, best_score = None, 0.0
        title_dup = None
        for ex in pool:
            if ex.type != cand.type:
                continue
            s = sim(_card_text(cand), _card_text(ex))
            if s > best_score:
                best, best_score = ex, s
            if title_dup is None and title_overlap(cand.title, ex.title) >= TITLE_DUP_OVERLAP:
                title_dup = ex

        if best is not None and best_score >= DUP_THRESHOLD:
            actions.append(("merge", best.id, cand))
            continue

        # Reworded duplicate: same claim, different phrasing. Merge — UNLESS the
        # body sim sits in the supersede band, where the contradiction path
        # below must decide instead of a silent merge.
        if title_dup is not None and best_score < supersede_band[0]:
            if title_dup.verified:
                # Unlike the >=0.92 path above, the bodies here differ BY DEFINITION —
                # the match is a heuristic on title tokens alone. Merging keeps the
                # approved body, which means silently DISCARDING this candidate's
                # claim. That is safe when the two say the same thing and wrong when
                # they do not, and title overlap cannot tell those apart: a live drain
                # matched "ML assets live under ~/Data/pdf-bake" to "libpdfium is
                # installed under Data/pdfium" — different facts, shared vocabulary.
                # Measured, the false positive (0.34 body sim) and a true reworded
                # preference (0.40) sit too close to separate with a threshold. So
                # ambiguity against an approved claim goes to the human, which is what
                # a reviewer is for. Exact dups — 114 of 116 in practice — still drain.
                actions.append(("queue", cand, f"possible-duplicate:{title_dup.id}"))
            else:
                actions.append(("merge", title_dup.id, cand))
            continue

        if best is not None and supersede_band[0] <= best_score < supersede_band[1]:
            if confirm_contradiction is not None and confirm_contradiction(cand, best):
                actions.append(("supersede", best.id, cand))
                accepted.append(cand)
            else:
                actions.append(("queue", cand, f"possible-supersede:{best.id}"))
            continue

        # Model confidence is not verification. Every novel distilled claim
        # crosses the review boundary; confidence only explains queue priority.
        reason = ("high-confidence-unverified" if cand.confidence >= CONFIDENCE_GATE
                  else "low-confidence")
        actions.append(("queue", cand, reason))

    return actions
