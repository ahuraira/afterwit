"""Auto-review: an independent model clears cards for serving. ADR-021.

WHY THIS IS NOT A HOLE IN ADR-011
--------------------------------
ADR-011 said "human approval is the only path to `verified: true`". The property
that rule was buying is not *humanity* — it is **separation of duties**: the
agent that WROTE a card must not be the one that CLEARS it, or a bad extraction
launders itself into every future prompt. A human was simply the only reviewer
we had.

So auto-review keeps the property and drops the assumption:

1. **Opt-in.** `auto_review = true` in config. Default off.
2. **A different reviewer.** The reviewer driver defaults to the *other* driver
   from the distiller (codex distills -> claude reviews, and vice versa). It is
   handed the card and the rubric, never the distiller's reasoning.
3. **Abstain by default.** Unparseable output, a timeout, a hedge — all abstain.
   An abstained card stays in the queue for a human. Silence is never consent.
4. **Deterministic vetoes the model cannot overrule.** `preference` cards encode
   the user's own intent and only the user can confirm them. A card carrying a
   redaction marker means a credential reached the distiller; no model gets a
   vote on whether that ships.
5. **Attributable.** Every approval stamps `reviewed_by` into frontmatter and
   appends to the per-device audit log, so `grep 'auto-review approve' log-*.md`
   enumerates everything the machine ever cleared, by card id.

What this buys the user in CLAUDE.md's terms: the review gate stops being a
wall a non-developer never climbs, without becoming a door that swings open.
"""

from __future__ import annotations

import json
import itertools
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import cards as cards_mod
from . import config as config_mod
from . import distill, redact, ui

# A card the user asserted about themselves. A model cannot confirm the user's
# own preference on the user's behalf — that is the one thing only they know.
NEVER_AUTO: frozenset[str] = frozenset({"preference"})

MIN_CONFIDENCE = 0.5
MAX_BODY_CHARS = 1500  # longer than this and "the sources support it" is unverifiable
TIMEOUT_S = 120

RUBRIC = """\
You are reviewing ONE knowledge card extracted from a developer's AI coding session.
The card will be injected into that developer's future AI prompts if you approve it.
A wrong approval poisons every future session. A wrong rejection loses one card.
Reject and abstain are cheap. Approve is expensive. Act accordingly.

APPROVE only if ALL of these hold:
- It records a RESOLVED outcome: a fix that demonstrably worked, a decision that
  stuck, a gotcha that cost real time, or a fact the user explicitly confirmed.
- The claim is supported by the cited sources. No leaps beyond them.
- It is self-contained: understandable months later without the transcript.
- It is NOT something you could recover by grepping the codebase. Code structure
  is not knowledge; the reason behind it is.
- The title states the claim, with tokens someone would actually search for.
- It contains no credentials, no personal data, no pasted code bodies.

REJECT if: it is speculation, an unfinished attempt, a restatement of code, a
summary of what the user asked rather than what was learned, or it contains
anything sensitive.

ABSTAIN if you are not sure. A human will look at it.

Respond with ONE JSON object and nothing else:
{"verdict": "approve" | "reject" | "abstain", "reason": "<one sentence, max 200 chars>"}

--- CARD ---
type: %(type)s
title: %(title)s
project: %(project)s
confidence: %(confidence).2f
sources: %(sources)s

%(body)s
--- END CARD ---

--- SOURCE EVIDENCE ---
%(evidence)s
--- END SOURCE EVIDENCE ---
"""


@dataclass
class Verdict:
    verdict: str  # "approve" | "reject" | "abstain"
    reason: str
    model: str  # who decided; "gate" for the deterministic pre-checks


def _opposite(driver: str) -> str:
    return "claude-p" if driver == "codex" else "codex"


def reviewer_name(cfg: config_mod.Config) -> str:
    """Driver for the reviewer. NEVER the distiller — separation of duties is the
    property (module docstring). An explicit `auto_review_driver` equal to the
    distiller is a misconfiguration that would silently void the guarantee, so we
    override it with the opposite driver and warn, rather than honour it (audit
    claim 6). Model-level sameness is not distinguished — different driver is the
    line we enforce; two models behind one driver still share a family."""
    chosen = cfg.auto_review_driver or _opposite(cfg.distill_driver)
    if chosen == cfg.distill_driver:
        import sys
        print(f"warning: auto_review_driver == distill_driver ({chosen}); "
              f"overriding reviewer to {_opposite(chosen)} to keep the writer and "
              "reviewer distinct (ADR-021).", file=sys.stderr)
        chosen = _opposite(cfg.distill_driver)
    return chosen


def _gate(card: cards_mod.Card) -> Verdict | None:
    """Deterministic pre-checks. Returns a Verdict to short-circuit, or None to
    let the model decide. The model never sees a card that fails here."""
    if card.type in NEVER_AUTO:
        return Verdict("abstain", f"{card.type} cards encode user intent — human only", "gate")
    if redact.has_secret(card.title) or redact.has_secret(card.body):
        return Verdict("reject", "card carries a redaction marker — a credential reached it", "gate")
    if not card.sources:
        return Verdict("reject", "no sources — provenance is mandatory", "gate")
    if float(card.confidence) < MIN_CONFIDENCE:
        return Verdict("abstain", f"confidence {card.confidence:.2f} below {MIN_CONFIDENCE}", "gate")
    if len(card.body) > MAX_BODY_CHARS:
        return Verdict("abstain", f"body {len(card.body)} chars — too long to verify", "gate")
    return None


def _source_evidence(card: cards_mod.Card) -> tuple[str, str | None]:
    """Load bounded cited excerpts; auto-approval requires inspectable evidence."""
    excerpts: list[str] = []
    for source in card.sources[:8]:
        raw_path = str(source.get("path", ""))
        if not raw_path or "://" in raw_path:
            return "", f"source is not a readable local file: {raw_path or '?'}"
        path = Path(raw_path).expanduser()
        if not path.is_file():
            return "", f"source is unavailable: {raw_path}"
        locator = source.get("lines") or source.get("heading")
        if not locator:
            return "", f"source has no line or heading locator: {raw_path}"
        try:
            if source.get("lines"):
                nums = [int(n) for n in re.findall(r"\d+", str(source["lines"]))]
                if not nums:
                    return "", f"invalid line locator for {raw_path}"
                start, end = nums[0], min(nums[-1], nums[0] + 80)
                with path.open(encoding="utf-8", errors="replace") as f:
                    text = "".join(itertools.islice(f, start - 1, end))
            else:
                heading = str(source["heading"]).strip().lstrip("#").strip()
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                start = next((i for i, line in enumerate(lines)
                              if line.lstrip("#").strip() == heading), -1)
                if start < 0:
                    return "", f"heading not found in {raw_path}: {heading}"
                chosen: list[str] = []
                for line in lines[start:start + 81]:
                    if chosen and line.startswith("#"):
                        break
                    chosen.append(line)
                text = "\n".join(chosen)
        except OSError as e:
            return "", f"source unreadable: {raw_path}: {e}"
        text = redact.sanitize(text).strip()
        if not text:
            return "", f"source excerpt is empty: {raw_path}"
        excerpts.append(f"[{raw_path} @ {locator}]\n{text[:6000]}")
    return "\n\n".join(excerpts)[:16000], None


_JSON = re.compile(r"\{.*\}", re.DOTALL)


def _no_dupes(pairs: list[tuple[str, object]]) -> dict:
    """object_pairs_hook that rejects duplicate keys. `{"verdict":"reject",
    "verdict":"approve"}` would otherwise resolve to the LAST value under normal
    dict semantics — an ambiguous (or prompt-injected) reviewer response that
    silently approves (audit claim 5, critical). A duplicated key is not a clean
    verdict, so we raise and the caller abstains."""
    seen: dict = {}
    for k, val in pairs:
        if k in seen:
            raise ValueError(f"duplicate key {k!r}")
        seen[k] = val
    return seen


def _parse(raw: str, model: str) -> Verdict:
    """Anything we cannot read as a clean verdict is an abstain, never an approve."""
    m = _JSON.search(raw or "")
    if not m:
        return Verdict("abstain", "reviewer returned no JSON", model)
    try:
        obj = json.loads(m.group(0), object_pairs_hook=_no_dupes)
    except (json.JSONDecodeError, ValueError) as e:
        return Verdict("abstain", f"reviewer returned unreadable JSON: {e}"[:200], model)
    if not isinstance(obj, dict):
        return Verdict("abstain", "reviewer JSON was not an object", model)
    verdict = obj.get("verdict")
    # a nested / list / non-string verdict is ambiguous → abstain, never approve
    if not isinstance(verdict, str):
        return Verdict("abstain", f"verdict was not a string: {type(verdict).__name__}", model)
    v = verdict.strip().lower()
    if v not in {"approve", "reject", "abstain"}:
        return Verdict("abstain", f"reviewer returned unknown verdict {v!r}", model)
    return Verdict(v, str(obj.get("reason", ""))[:200], model)


def review_one(cfg: config_mod.Config, card: cards_mod.Card, driver=None) -> Verdict:
    """One card, one verdict. Never raises: an exception is an abstain."""
    gated = _gate(card)
    if gated is not None:
        return gated
    evidence, evidence_error = _source_evidence(card)
    if evidence_error:
        return Verdict("abstain", evidence_error[:200], "gate")
    name = reviewer_name(cfg)
    # The resolved identity, not the driver name: `reviewed_by: claude-p` (223 cards,
    # audit 2026-07-23) records which CLI ran, never which model approved — the one
    # fact separation of duties is about (ADR-035).
    model = distill.attribution(name, cfg.auto_review_model, cfg.auto_review_effort)
    if driver is None:
        driver = distill.make_driver(name, model=cfg.auto_review_model,
                                     effort=cfg.auto_review_effort)
    prompt = RUBRIC % {
        "type": card.type, "title": card.title, "project": card.project,
        "confidence": float(card.confidence),
        "sources": ", ".join(str(s.get("path", "?")) for s in card.sources[:5]),
        "body": card.body,
        "evidence": evidence,
    }
    try:
        return _parse(driver(prompt), model)
    except Exception as e:  # noqa: BLE001 — a broken reviewer must not approve
        return Verdict("abstain", f"reviewer failed: {e!r}"[:200], model)


def _log(cfg: config_mod.Config, line: str) -> None:
    path = config_mod.log_path(cfg.wiki_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(f"- {datetime.now(timezone.utc).isoformat()} {line}\n")


def _gate_rowid(conn: sqlite3.Connection, rowid: int) -> Verdict | None:
    """Re-run the deterministic gate against the queued card by rowid. Returns
    the veto Verdict if the gate refuses approval, else None. A row that has
    vanished (race) is itself a refusal to approve."""
    row = conn.execute("SELECT card_json FROM review_queue WHERE rowid=?",
                       (rowid,)).fetchone()
    if row is None:
        return Verdict("abstain", "queue row gone before approval", "gate")
    try:
        data = json.loads(row["card_json"])
        card = cards_mod.Card(**{k: v for k, v in data.items()
                                 if k in cards_mod.Card.__dataclass_fields__})
    except Exception as e:  # noqa: BLE001 — an unparseable row is not approvable
        return Verdict("reject", f"queued card unreadable: {e!r}"[:200], "gate")
    return _gate(card)


def apply_verdict(cfg: config_mod.Config, conn: sqlite3.Connection, rowid: int,
                  v: Verdict) -> str:
    """Enact a verdict. Abstain leaves the row in the queue for a human.

    Approval goes through the same `ui._approve` a human presses, so there is
    exactly one code path that can set `verified: true` — it just records who.
    """
    if v.verdict == "approve":
        # Re-gate at the point of persistence, not only where the verdict was
        # produced (audit claim 7). apply_verdict is the last step before a card
        # is written verified; a caller that hands us a forged/mismatched approve
        # (or a future code path that skips review_one) must still not slip a
        # `preference` card or a secret-bearing card past the deterministic vetoes.
        blocked = _gate_rowid(conn, rowid)
        if blocked is not None:
            _log(cfg, f"auto-review approve BLOCKED by re-gate [{v.model}] "
                      f"rowid={rowid}: {blocked.reason}")
            return apply_verdict(cfg, conn, rowid, blocked)
        res = ui._approve(cfg, conn, rowid, None, reviewed_by=v.model)
        _log(cfg, f"auto-review approve [{v.model}] {res['id']}: {v.reason}")
        return "approved"
    if v.verdict == "reject":
        ui._reject(cfg, conn, rowid)
        _log(cfg, f"auto-review reject [{v.model}] rowid={rowid}: {v.reason}")
        return "rejected"
    _log(cfg, f"auto-review abstain [{v.model}] rowid={rowid}: {v.reason}")
    return "abstained"


def _corroborate_known(cfg: config_mod.Config, conn: sqlite3.Connection, rowid: int,
                       card: cards_mod.Card, approved: list[cards_mod.Card],
                       *, dry_run: bool = False) -> bool:
    """Fold a queued duplicate of an ALREADY-APPROVED card into it. No model call.

    postprocess used to queue any duplicate of a verified card, so every approval became
    a permanent generator of queue noise — 114 of 371 queued cards were knowledge the
    user had already signed off on. That is fixed at the source (ADR-031), but cards
    already sitting in the queue are inert and will never be re-evaluated by it.

    Asking a model to re-judge a claim its owner already approved is the wrong question
    at any price. This is deterministic: it is a duplicate or it is not, and it costs
    nothing. wiki.execute keeps the approved body — evidence accrues, wording stands.
    """
    from . import postprocess, wiki

    actions = postprocess.process([card], approved)
    if not actions or actions[0][0] != "merge":
        return False
    if dry_run:
        return True
    wiki.execute(actions[0], cfg, conn)
    conn.execute("DELETE FROM review_queue WHERE rowid=?", (rowid,))
    (cfg.wiki_root / "review" / f"{card.id}.md").unlink(missing_ok=True)
    conn.commit()
    return True


def review_queue(cfg: config_mod.Config, conn: sqlite3.Connection, *,
                 limit: int | None = None, dry_run: bool = False,
                 driver=None, on_verdict=None, deadline: float | None = None) -> dict:
    """Drain the review queue with the auto-reviewer. Returns outcome counts.

    `dry_run` prints verdicts and changes nothing — the only honest way to decide
    whether you trust this before you let it touch 180 cards.

    `deadline` (a `time.monotonic()` value) stops the drain when reached, leaving the
    rest queued. The nightly is time-budgeted (--timeout), not count-budgeted: a big
    backlog drains across several nights instead of blowing one run's clock or spending
    an unbounded pile of reviewer tokens in a single sitting. `stopped` counts what was
    left untouched by the cutoff.
    """
    counts = {"approved": 0, "rejected": 0, "abstained": 0, "errors": 0,
              "corroborated": 0, "stopped": 0}
    rows = conn.execute(
        "SELECT rowid, card_json FROM review_queue ORDER BY created"
        + (f" LIMIT {int(limit)}" if limit else "")
    ).fetchall()
    approved_pool = [c for _, c in cards_mod.iter_cards(cfg.wiki_root)
                     if c.status == "active" and c.verified]
    for i, row in enumerate(rows):
        if deadline is not None and time.monotonic() >= deadline:
            counts["stopped"] = len(rows) - i
            break
        try:
            data = json.loads(row["card_json"])
            card = cards_mod.Card(**{k: v for k, v in data.items()
                                     if k in cards_mod.Card.__dataclass_fields__})
            if _corroborate_known(cfg, conn, row["rowid"], card, approved_pool,
                                  dry_run=dry_run):
                counts["corroborated"] += 1
                continue
            v = review_one(cfg, card, driver=driver)
        except Exception as e:  # noqa: BLE001 — one bad row never stops the drain
            counts["errors"] += 1
            _log(cfg, f"auto-review error rowid={row['rowid']}: {e!r}")
            continue
        if on_verdict:
            on_verdict(row["rowid"], card, v)
        if dry_run:
            counts[{"approve": "approved", "reject": "rejected",
                    "abstain": "abstained"}[v.verdict]] += 1
            continue
        counts[apply_verdict(cfg, conn, row["rowid"], v)] += 1
    return counts
