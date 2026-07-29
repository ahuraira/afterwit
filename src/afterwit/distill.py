"""Distillation driver: session events → candidate cards → postprocess → wiki.

SPEC §7, ADR-004/005/009/010. This module is faithful plumbing around frozen
judgment: the extraction judgment lives in `prompts/distill.md` (never edited
here), the dedupe/supersede/gate judgment lives in `postprocess.process()`.
distill.py composes the prompt, runs the driver, parses JSON, builds Cards with
provenance, and hands actions to `wiki.execute`. It makes no promotion decisions
of its own.

Driver A (default): shell out to `claude -p` (subscription, zero marginal cost).
Driver B (batch-api) is P4 backlog work (ADR-005) — not wired here.

Processing order (ADR Gotcha #3): compaction-summary turns are pre-distilled,
so they lead each session's transcript. Thinking turns are passed through
labeled `[thinking]` — the frozen prompt uses them only as rationale (a card
whose sole evidence is thinking fails the cite-or-drop rule; ADR-009).

Events are duck-typed against the SPEC §6 contract
(`source_path, lines, project, ts, role, kind, text, meta`) so this module never
imports `afterwit.events` — adapters build it in parallel; tests construct matching
objects locally.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import itertools
import json
import os
import re
import sqlite3
import shutil
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, NamedTuple, cast

from . import cards as cards_mod
from . import config as config_mod
from . import index_db, postprocess, wiki

# ponytail: whole-turn text is capped so a runaway tool_result can't blow the
# prompt budget; adapters already drop successful oversized results (SPEC §6.1),
# this is a second belt. Raise if real transcripts lose signal.
MAX_EVENT_CHARS = 2000
# Whole-session cap (~60k tokens): keep the head (compaction summaries lead the
# ordering — Gotcha #3) and the tail (latest turns hold the resolutions); drop
# the middle. Without this a long session overflows the claude -p prompt.
MAX_TRANSCRIPT_CHARS = 240_000

_SRC_KINDS = {"user", "assistant", "thinking", "doc", "schema"}


# Last resort only. Precedence: config's distill_model → the model in the user's
# own ~/.codex/config.toml (harness.default_model) → this. A hardcoded id goes
# stale every release, so it is reached only when Codex itself has no config.
DEFAULT_CODEX_MODEL = "gpt-5.6-terra"


def driver_executable(name: str) -> str:
    """Resolve a driver in the sparse PATH used by cron/systemd.

    Installers such as nvm put binaries outside a scheduler's default PATH.
    An explicit AFTERWIT_<NAME>_BIN wins; otherwise use PATH and finally the
    conventional nvm install location. Returning an absolute path also makes
    the preflight and the eventual subprocess execute the same program.
    """
    env_name = f"AFTERWIT_{name.upper().replace('-', '_')}_BIN"
    configured = os.environ.get(env_name)
    if configured:
        p = Path(configured).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
        raise RuntimeError(f"{env_name} is not executable: {p}")
    found = shutil.which(name)
    if found:
        return found
    candidates = sorted(glob.glob(str(Path.home() / ".nvm/versions/node/*/bin" / name)),
                        reverse=True)
    for candidate in candidates:
        if os.access(candidate, os.X_OK):
            return candidate
    raise RuntimeError(
        f"distill driver {name!r} is not executable; set {env_name} to its absolute path"
    )


def preflight_driver(name: str) -> str:
    binary = "claude" if name == "claude-p" else name
    return driver_executable(binary)


def _child_env() -> dict[str, str]:
    """Environment for an LLM child process.

    `AFTERWIT_INTERNAL` tells `afterwit inject` — which the user wired into their
    own UserPromptSubmit/SessionStart hooks — that this session is afterwit's
    distiller or reviewer, not a human. Without it the child inherits those hooks
    and afterwit injects into itself. That is not hypothetical: 56 of the 76
    card-servings that tripped the kill-switch on 2026-07-26 were auto-review
    prompts, every one scored `ignored`, because a reviewer emits a verdict about
    a *different* card and never echoes the injected one. The self-talk both
    faked a 19.7% hit rate (75% on real prompts) and charged the two cards it
    kept pulling -0.2 apiece, 28 times, to usefulness -5.6 (ADR-037, Gotcha #59).

    Merged onto `os.environ` — a bare `env={...}` would strip PATH/HOME and the
    driver would not spawn at all.
    """
    return {**os.environ, "AFTERWIT_INTERNAL": "1"}


def claude_p(prompt: str, timeout: int = 180, model: str | None = None,
             effort: str | None = None) -> str:
    """Driver A: `claude -p` reads the composed prompt on stdin, prints the answer."""
    return claude_p_resolved(prompt, timeout, model, effort)[0]


def claude_p_resolved(prompt: str, timeout: int = 180, model: str | None = None,
                      effort: str | None = None) -> tuple[str, str | None]:
    """`(answer, model-that-actually-ran)`.

    No `--model` means the Claude Code CLI uses the model from its own
    settings.json — inheriting the harness default is the point, not an omission.
    `--effort` is only passed when configured: older CLIs reject the flag.

    `--output-format json` is what makes the second half of that tuple possible.
    `--model opus` is an ALIAS the CLI resolves on its side, so stamping the
    configured string recorded `opus` — true today, ambiguous the day opus-6
    ships, and `distilled_by` claims to say which model wrote a card (ADR-035).
    The JSON reply names the concrete id under `modelUsage`. Parsing is
    fail-soft: an older CLI, or any shape we do not recognise, degrades to the
    raw text with an unresolved model. Distillation must never break over
    metadata (Gotcha #75 was exactly that lesson).
    """
    cmd = ([driver_executable("claude"), "-p", "--output-format", "json"]
           + (["--model", model] if model else [])
           + (["--effort", effort] if effort else []))
    r = subprocess.run(
        cmd, input=prompt, capture_output=True, text=True, timeout=timeout,
        # NOT the locale's encoding. `text=True` alone means cp1252 on Windows, and
        # prompts/distill.md carries `≤` at position 90 — so EVERY session died with
        # `'charmap' codec can't encode character '≤'`, the run ended
        # `distillation attempted but every session failed`, and afterwit distilled
        # nothing on Windows while doctor reported all good (Gotcha #75).
        encoding="utf-8", errors="replace",
        env=_child_env(),
    )
    if r.returncode != 0:
        raise RuntimeError(f"claude -p exit {r.returncode}: {r.stderr[:300]}")
    try:
        payload = json.loads(r.stdout)
    except ValueError:
        return r.stdout, None
    if not isinstance(payload, dict):  # a bare array is a card list, not an envelope
        return r.stdout, None
    text = str(payload.get("result") or "")
    used = [k for k in (payload.get("modelUsage") or {})]
    return (text or r.stdout), (used[0] if used else None)


def _codex_rollout_model(thread_id: str) -> str | None:
    """The model Codex actually ran, read from the rollout it just wrote.

    Codex's `--json` event stream carries usage but never the model, so unlike
    Claude there is nothing in the reply to read. The rollout transcript does
    carry it, on `turn_context` (the same record Gotcha #51 was about), and
    `thread.started` gives the id that names the file. Observed, not configured:
    `-m` states an intent, this states what answered.
    """
    if not thread_id:
        return None
    try:
        from . import harness
        root = harness.sessions_dir("codex")
        matches = sorted(root.rglob(f"*{thread_id}.jsonl")) if root.is_dir() else []
        if not matches:
            return None
        with matches[-1].open(encoding="utf-8", errors="replace") as f:
            for line in f:
                rec = json.loads(line)
                if rec.get("type") == "turn_context":
                    return str((rec.get("payload") or {}).get("model") or "") or None
    except (OSError, ValueError):
        return None
    return None


def codex_p(prompt: str, timeout: int = 600, model: str | None = None,
            effort: str | None = None) -> str:
    return codex_p_resolved(prompt, timeout, model, effort)[0]


def codex_p_resolved(prompt: str, timeout: int = 600, model: str | None = None,
                     effort: str | None = None) -> tuple[str, str | None]:
    """Driver B (ADR-013): `codex exec`, read-only sandbox — a separate quota pool
    from claude -p, used for bulk backlog campaigns. `-o` captures the final message
    clean of event noise. Model/effort come from config (distill_model /
    distill_effort); DEFAULT_CODEX_MODEL is only the fallback when neither is set."""
    import tempfile

    from . import harness

    fd, out = tempfile.mkstemp(suffix=".txt")
    Path(out).touch()
    try:
        cmd = [driver_executable("codex"), "exec", "--json",
               "-s", "read-only", "--skip-git-repo-check",
               "-m", model or harness.default_model("codex") or DEFAULT_CODEX_MODEL]
        if effort:
            cmd += ["-c", f'model_reasoning_effort="{effort}"']
        r = subprocess.run(
            cmd + ["-o", out, "-"],
            input=prompt, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",  # same as claude_p (Gotcha #75)
            env=_child_env(),  # codex has no hook today (ADR-003); set anyway —
                               # the guard must not depend on that staying true
        )
        if r.returncode != 0:
            raise RuntimeError(f"codex exec exit {r.returncode}: {r.stderr[:300]}")
        # `--json` streams events; the answer still comes from `-o`, clean of them.
        thread = ""
        for line in (r.stdout or "").splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict) and ev.get("type") == "thread.started":
                thread = str(ev.get("thread_id") or "")
                break
        return Path(out).read_text(encoding="utf-8"), _codex_rollout_model(thread)
    finally:
        import os
        os.close(fd)
        Path(out).unlink(missing_ok=True)


DRIVERS: dict[str, Callable[[str], str]] = {"claude-p": claude_p, "codex": codex_p}


def attribution(driver: str, model: str | None = None, effort: str | None = None) -> str:
    """`driver:model[:effort]` — the resolved identity of one LLM run (ADR-035).

    Resolved, not configured: an unset model means "whatever the harness is set
    to", and recording the literal `None` (or the driver name, as `reviewed_by`
    used to) answers none of "which model wrote this card, at what effort?".
    """
    from . import harness

    resolved = model or harness.default_model(harness.harness_of(driver)) or "default"
    return ":".join([driver, resolved] + ([effort] if effort else []))


class Driver:
    """A bound driver that knows who it is, so the card it produces can say so.
    Callable[[str], str] everywhere else — only `distill_sessions` reads `.label`.

    `label` starts as the CONFIGURED identity and is replaced, after each call,
    with the model that actually answered. That matters because the configured
    value is routinely an alias: `model = "opus"` in the user's own
    settings.json resolves to `claude-opus-5` today and to something else after
    the next release, so a stamp of `opus` cannot answer "which model wrote this
    card" — the question `distilled_by` exists for (ADR-035), and the one
    auto-review's separation-of-duties rule is checked against (ADR-021).
    A driver that cannot resolve its model keeps the configured label.
    """

    def __init__(self, fn: Callable[[str], tuple[str, str | None]], name: str,
                 model: str | None = None, effort: str | None = None) -> None:
        self.fn = fn
        self._name, self._model, self._effort = name, model, effort
        self.label = attribution(name, model, effort)

    def __call__(self, prompt: str) -> str:
        text, resolved = self.fn(prompt)
        if resolved:
            self.label = attribution(self._name, resolved, self._effort)
        return text


def make_driver(name: str, *, model: str | None = None,
                effort: str | None = None) -> Callable[[str], str]:
    """Bind config/CLI model+effort onto a named driver. Both drivers take both:
    `claude --effort` exists as of Claude Code 2.x, so a configured
    `distill_effort` is no longer silently dropped on the claude-p path."""
    if name == "codex":
        return Driver(lambda prompt: codex_p_resolved(prompt, model=model, effort=effort),
                      name, model, effort)
    if name == "claude-p":
        return Driver(lambda prompt: claude_p_resolved(prompt, model=model, effort=effort),
                      name, model, effort)
    return DRIVERS[name]


# --- transcript composition -------------------------------------------------

def _is_compaction(e) -> bool:
    if "compact" in (getattr(e, "kind", "") or "").lower():
        return True
    meta = getattr(e, "meta", None) or {}
    return bool(meta.get("compact") or meta.get("isCompactSummary"))


def _order(events: list) -> list:
    """Compaction summaries first (ADR Gotcha #3), everything else in order."""
    comp = [e for e in events if _is_compaction(e)]
    rest = [e for e in events if not _is_compaction(e)]
    return comp + rest


def _label(e) -> str:
    kind = getattr(e, "kind", "") or ""
    if kind == "thinking":
        return "thinking"
    if _is_compaction(e):
        return "summary"
    return getattr(e, "role", None) or kind or "note"


class Coverage(NamedTuple):
    """How much of a session actually reached the model.

    A truncated session and a fully-read one are indistinguishable afterwards —
    both leave one ledger row and some cards — so a card can silently rest on
    16% of the evidence. Measured here, reported by the caller.
    """

    raw_chars: int          # every turn, at full length
    sent_chars: int         # what went into the prompt
    events: int
    events_truncated: int   # turns clipped by MAX_EVENT_CHARS
    elided: bool            # session cap hit: the MIDDLE was dropped entirely

    @property
    def pct(self) -> int:
        """Whole-percent of the session's own text that reached the model."""
        return 100 if not self.raw_chars else round(100 * self.sent_chars / self.raw_chars)

    def describe(self) -> str:
        if not self.elided and not self.events_truncated:
            return "full"
        why = ["middle elided"] if self.elided else []
        if self.events_truncated:
            why.append(f"{self.events_truncated}/{self.events} turns clipped")
        return f"{self.pct}% ({', '.join(why)})"


def _render(ordered: list) -> str:
    return _render_cov(ordered)[0]


def _render_cov(ordered: list) -> tuple[str, Coverage]:
    lines = []
    raw = kept = clipped = 0
    for i, e in enumerate(ordered, 1):
        text = (getattr(e, "text", "") or "").replace("\n", " ").strip()
        raw += len(text)
        if len(text) > MAX_EVENT_CHARS:
            text = text[:MAX_EVENT_CHARS] + "…"
            clipped += 1
        kept += len(text)
        lines.append(f"L{i} [{_label(e)}] {text}")
    out = "\n".join(lines)
    elided = len(out) > MAX_TRANSCRIPT_CHARS
    if elided:
        before = len(out)
        head, tail = MAX_TRANSCRIPT_CHARS // 6, MAX_TRANSCRIPT_CHARS * 5 // 6
        out = (out[:head] + "\n… [transcript middle elided for length] …\n"
               + out[-tail:])
        # The session cap cuts the rendered string, not the turns, so scale the
        # per-turn total by what survived rather than counting characters twice.
        kept = round(kept * len(out) / before) if before else 0
    return out, Coverage(raw, min(kept, raw), len(ordered), clipped, elided)


# --- provenance -------------------------------------------------------------

def _src_kind(kind: str | None) -> str | None:
    if not kind:
        return None
    if kind in _SRC_KINDS:
        return kind
    return "assistant"  # tool_use/tool_result/etc. are assistant-side activity


def _origin(e) -> dict:
    """Source dict for one event, carrying harness/model/kind origin (ADR-010).
    Only `path` is mandatory; absent origin keys are dropped (validate() unchanged)."""
    meta = getattr(e, "meta", None) or {}
    o = {
        "path": getattr(e, "source_path", None) or "<session>",
        "lines": getattr(e, "lines", None),
        "harness": meta.get("harness"),
        "model": meta.get("model"),
        "effort": meta.get("effort"),
        "kind": _src_kind(getattr(e, "kind", None)),
    }
    return {k: v for k, v in o.items() if v is not None}


def _parse_line_range(spec: str) -> tuple[int, int] | None:
    """'L10-L20' | 'L10' -> (10, 20). Tolerates whitespace and missing 'L'."""
    if not spec:
        return None
    nums = [int(n) for n in re.findall(r"\d+", str(spec))]
    if not nums:
        return None
    return (nums[0], nums[-1])


def _sources_for(ordered: list, spec: str) -> list[dict]:
    """Map the model's cited composed line range back to raw event origins."""
    rng = _parse_line_range(spec)
    picked = ordered[rng[0] - 1 : rng[1]] if rng else []
    out: list[dict] = []
    seen: set = set()
    for e in picked:
        o = _origin(e)
        key = frozenset(o.items())
        if key not in seen:
            seen.add(key)
            out.append(o)
    return out


def _session_sources(events: list) -> list[dict]:
    """Fallback provenance when the model cites no usable range: one dict per
    distinct source file in the session. Guarantees the mandatory ≥1 source."""
    out: list[dict] = []
    seen: set = set()
    for e in events:
        p = getattr(e, "source_path", None) or "<session>"
        if p not in seen:
            seen.add(p)
            out.append(_origin(e))
    return out or [{"path": "<session>"}]


def _session_project(events: list) -> str:
    projs = [p for e in events if (p := getattr(e, "project", None))]
    return Counter(projs).most_common(1)[0][0] if projs else "global"


def _stamp_commit(candidates: list, projects_root: Path, project: str,
                  cache: dict[str, tuple[str | None, str | None]],
                  aliases: dict[str, str] | None = None) -> None:
    """Anchor each card to the project's HEAD + origin at extraction time
    (ADR-018/020), so drift is measurable as `git diff <source_commit>..HEAD` and
    the card resolves on another device by `repo_url`. Non-git projects and
    'global' stamp None and fall back to the dead-pointer check."""
    from . import gitmeta

    sha, url = gitmeta.anchor(projects_root, project, cache, aliases)
    for c in candidates:
        if sha:
            c.source_commit = sha
        if url:
            c.repo_url = url


# --- card construction ------------------------------------------------------

def _build_card(d: dict, ordered: list, events: list, project: str, today: str) -> cards_mod.Card:
    """Build a Card from one parsed candidate dict. Defensive: missing/garbage
    fields yield an invalid Card that postprocess routes to the review queue
    (never crash on model drift — CLAUDE.md adapter rule applies here too)."""
    body = (d.get("body") or "").strip()
    why = (d.get("why") or "").strip()
    if why and "**Why:**" not in body:
        body = f"{body}\n\n**Why:** {why}" if body else f"**Why:** {why}"
    sources = _sources_for(ordered, d.get("source_lines", "")) or _session_sources(events)
    if len(sources) > 8:
        # Provenance is a citation, not a firehose (audit 2026-07-06: cards
        # citing 600+ lines). Keep first 4 + last 4 — openings carry the claim,
        # endings carry the resolution.
        sources = sources[:4] + sources[-4:]
    try:
        conf = min(1.0, max(0.0, float(d.get("confidence", 0.0))))
    except (TypeError, ValueError):
        conf = 0.0
    return cards_mod.Card(
        id=cards_mod.new_ulid(),
        type=str(d.get("type", "")),
        title=str(d.get("title", "")).strip()[:80],
        project=project,
        status="active",
        body=body,
        sources=sources,
        tags=[str(t) for t in (d.get("tags") or [])],
        files=[str(f) for f in (d.get("files") or [])],
        confidence=conf,
        verified=False,  # nothing distilled is trusted until human review (ADR-011)
        created=today,
        updated=today,
    )


# --- driver + parse ---------------------------------------------------------

def _load_prompt() -> str:
    p = Path(__file__).resolve().parents[2] / "prompts" / "distill.md"
    return p.read_text(encoding="utf-8")


def _parse_cards(raw: str) -> list[dict]:
    """Strict JSON array parse; tolerate ```json fences / prose by slicing to the
    outermost brackets. Raises on anything that is not a JSON array."""
    s = raw.strip()
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        i, j = s.find("["), s.rfind("]")
        if i == -1 or j <= i:
            raise
        data = json.loads(s[i : j + 1])
    if not isinstance(data, list):
        raise ValueError("distiller output was not a JSON array")
    return [d for d in data if isinstance(d, dict)]


def _extract(prompt: str, driver: Callable[[str], str]) -> list[dict]:
    """Run the driver; on parse failure retry ONCE with the error appended."""
    err = None
    for attempt in (0, 1):
        raw = driver(prompt if attempt == 0 else prompt + _retry_note(err))
        try:
            return _parse_cards(raw)
        except (json.JSONDecodeError, ValueError) as e:
            err = e
    raise err  # type: ignore[misc]


def _retry_note(err) -> str:
    return (
        f"\n\nYOUR PREVIOUS OUTPUT FAILED JSON PARSING: {err}. "
        "Return ONLY a valid JSON array of card objects, nothing else."
    )


# --- orchestration ----------------------------------------------------------

def _pool_replace(pool: list, card_id: str, new: cards_mod.Card) -> None:
    for i, c in enumerate(pool):
        if c.id == card_id:
            pool[i] = new
            return


def _pool_remove(pool: list, card_id: str) -> None:
    pool[:] = [c for c in pool if c.id != card_id]


# --- per-session distill ledger (ADR-015) -----------------------------------
# Operational state, NOT wiki-derived: index_db.rebuild() wipes cards/fts/links
# only, so this table survives a --rebuild (R8). The DDL lives here (distill is
# its only writer) and is created against the existing connection — index_db.py
# stays frozen.

def ensure_distilled_ledger(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS distilled("
        "source TEXT PRIMARY KEY, content_hash TEXT, ts TEXT, cards INTEGER, "
        "source_mtime REAL, source_size INTEGER)"
    )
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(distilled)")}
    if "source_mtime" not in cols:
        conn.execute("ALTER TABLE distilled ADD COLUMN source_mtime REAL")
    if "source_size" not in cols:
        conn.execute("ALTER TABLE distilled ADD COLUMN source_size INTEGER")
    if "coverage" not in cols:
        # NULL on rows written before this existed — "unknown", not "full".
        conn.execute("ALTER TABLE distilled ADD COLUMN coverage INTEGER")
    conn.commit()


def _session_hash(rendered: str) -> str:
    """Content hash of the exact transcript fed to the driver. Changes iff the
    distillable input changes (a grown live session re-eligibilizes) — stronger
    and disk-independent versus hashing raw file bytes."""
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def distill_sessions(
    sessions: Iterable[list],
    cfg: config_mod.Config,
    conn: sqlite3.Connection,
    *,
    driver: Callable[[str], str] = claude_p,
    budget: int | None = None,
    from_agent: bool = False,
    use_ledger: bool = False,
) -> dict:
    """Distill up to `budget` sessions (caller orders them newest-first).

    Each session is a list of Event-shaped objects. Returns run stats.

    `from_agent` (default False) is spec-consistent: the batch distiller writes
    high-confidence cards as active+unverified (ADR-011 Context; SPEC §7 gate),
    low-confidence to the review queue. Set True only to force the always-queue
    AGENT_CONFIDENCE_CAP path used by MCP `save_insight` (SPEC §9.1).

    `use_ledger` (ADR-015): consult/record the `distilled` table so already-
    distilled sessions are skipped (0-card results count; a content-hash change
    re-eligibilizes). Budget then counts only sessions actually driven, never
    ledger-skips. The ledger row is written ONLY after a session's postprocess
    actions execute — a driver failure records nothing (charter risk row)."""
    prompt = _load_prompt()
    today = datetime.now(timezone.utc).date().isoformat()
    existing = [c for _, c in cards_mod.iter_cards(cfg.wiki_root) if c.status == "active"]
    head_cache: dict[str, tuple[str | None, str | None]] = {}  # one git probe per project
    stats = {"sessions": 0, "skipped": 0, "write": 0, "merge": 0,
             "supersede": 0, "queue": 0, "truncated": 0}
    worst: tuple[int, str] | None = None  # (coverage%, source) of the least-read session
    if use_ledger:
        ensure_distilled_ledger(conn)
        stats["ledger_skipped"] = 0

    # islice caps iteration only when the ledger is off; with the ledger on,
    # budget must count driven sessions (not skips), so we cap inside the loop.
    it = (itertools.islice(sessions, budget)
          if budget is not None and not use_ledger else sessions)
    budget_used = 0
    for events in it:
        if not events:
            continue
        if use_ledger and budget is not None and budget_used >= budget:
            break
        ordered = _order(events)
        rendered, coverage = _render_cov(ordered)
        source = getattr(events[0], "source_path", None) or "<session>"
        content_hash = _session_hash(rendered) if use_ledger else ""
        if use_ledger:
            prior = conn.execute(
                "SELECT content_hash FROM distilled WHERE source=?", (source,)
            ).fetchone()
            if prior is not None and prior[0] == content_hash:
                try:
                    st = Path(source).stat()
                    conn.execute(
                        "UPDATE distilled SET source_mtime=?, source_size=? WHERE source=?",
                        (st.st_mtime, st.st_size, source),
                    )
                    conn.commit()
                except OSError:
                    pass
                stats["ledger_skipped"] += 1
                continue

        composed = prompt + "\n\n---\nSESSION TRANSCRIPT (each line is one turn):\n" + rendered
        budget_used += 1
        try:
            dicts = _extract(composed, driver)
        except Exception as e:  # driver failure or JSON still bad after retry
            stats["skipped"] += 1
            _log_skip(cfg.wiki_root, source, e)
            continue  # no ledger row — a failed session stays eligible

        stats["sessions"] += 1
        # Say so, every time, in the log that syncs. A card built on 16% of a
        # session is weaker evidence than one built on all of it, and until now
        # nothing anywhere recorded which kind you were looking at.
        if coverage.elided:
            stats["truncated"] += 1
            if worst is None or coverage.pct < worst[0]:
                worst = (coverage.pct, source)
        if coverage.elided or coverage.events_truncated:
            _log_line(cfg.wiki_root, f"distill-partial {source}: read {coverage.describe()} "
                                     f"of {coverage.raw_chars:,} chars")
        project = _session_project(events)
        candidates = [_build_card(d, ordered, events, project, today) for d in dicts]
        _stamp_commit(candidates, cfg.projects_root, project, head_cache,
                      cfg.project_aliases)
        # Who extracted this (ADR-035). `Driver` carries its own resolved identity;
        # a plain callable (tests, custom drivers) simply leaves the field unset.
        label = getattr(driver, "label", None)
        if label:
            for c in candidates:
                c.distilled_by = label
        actions = postprocess.process(candidates, existing, from_agent=from_agent)
        # Every card this session produced, queued ones included. Counting only
        # the cards that landed active made a session that proposed ten cards
        # read as `cards: 0` in the ledger, and `stats` then reported it as a
        # zero-card session — while ten of its cards sat in the review queue.
        n_cards = len(actions)
        for a in actions:
            kind = a[0]
            written = wiki.execute(a, cfg, conn)
            stats[kind] += 1
            if kind != "queue":
                assert written is not None
            written_card = cast(cards_mod.Card, written)
            if kind == "write":
                existing.append(written_card)
            elif kind == "merge":
                _pool_replace(existing, a[1], written_card)
            elif kind == "supersede":
                _pool_remove(existing, a[1])
                existing.append(written_card)
        if use_ledger:
            try:
                source_stat = Path(source).stat()
                source_mtime, source_size = source_stat.st_mtime, source_stat.st_size
            except OSError:
                source_mtime, source_size = None, None
            conn.execute(
                "INSERT INTO distilled(source,content_hash,ts,cards,source_mtime,source_size,"
                "coverage) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(source) DO UPDATE SET "
                "content_hash=excluded.content_hash, ts=excluded.ts, cards=excluded.cards, "
                "source_mtime=excluded.source_mtime, source_size=excluded.source_size, "
                "coverage=excluded.coverage",
                (source, content_hash, datetime.now(timezone.utc).isoformat(), n_cards,
                 source_mtime, source_size, coverage.pct),
            )
        conn.commit()

    if worst:
        stats["worst_coverage"] = worst[0]
    wiki.regenerate(cfg, conn)
    return stats


def _log_line(wiki_root: Path, message: str) -> None:
    log = config_mod.log_path(wiki_root)  # per-device audit log (ADR-019/020)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as f:
        f.write(f"- {datetime.now(timezone.utc).isoformat()} {message}\n")


def _log_skip(wiki_root: Path, source: str, err) -> None:
    _log_line(wiki_root, f"distill-skip {source}: {err}")


def _iter_sessions(cfg: config_mod.Config, source: str, project: str | None = None,
                   conn: sqlite3.Connection | None = None):
    """Obtain sessions (lists of Events grouped by source file) from the adapters.

    Thin seam: expects `afterwit.adapters.iter_events(cfg, source) -> Iterable[Event]`,
    which this groups by `source_path` and orders newest-first by max ts.
    adapters is built in parallel (agent-adapters); until it lands this returns
    None and the caller defers the real-session run to integration."""
    try:
        from . import adapters  # type: ignore
        events: list = []
        if conn is not None:
            ensure_distilled_ledger(conn)
        for path in adapters._iter_paths(source, cfg.projects_root):  # type: ignore[attr-defined]
            if conn is not None:
                st = path.stat()
                row = conn.execute(
                    "SELECT source_mtime, source_size FROM distilled WHERE source=?",
                    (str(path),),
                ).fetchone()
                if row and row[0] == st.st_mtime and row[1] == st.st_size:
                    continue
            try:
                events.extend(adapters._parser(source)(path))  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001 — one corrupt session is not the corpus
                adapters.log.warning("distill session skip %s: %s", path, exc)
    except (ImportError, AttributeError):
        return None
    if project is not None:
        # Per-project worker discipline (ADR-013): parallelize ACROSS projects,
        # serialize WITHIN one — the dedupe pool is project-scoped, so two
        # workers on one project would race it and mint duplicates.
        events = [e for e in events if getattr(e, "project", None) == project]
    by_file: dict[str, list] = {}
    for e in events:
        by_file.setdefault(getattr(e, "source_path", "<session>"), []).append(e)
    return sorted(
        by_file.values(),
        key=lambda evs: max((getattr(e, "ts", "") or "") for e in evs),
        reverse=True,
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="afterwit distill")
    p.add_argument("--budget", type=int, default=None, help="max sessions this run (newest first)")
    p.add_argument("--source", default="claude", help="adapter source (claude|codex|...)")
    p.add_argument("--driver", choices=sorted(DRIVERS), default=None,
                   help="LLM driver (default: config distill_driver, else claude-p)")
    p.add_argument("--model", default=None,
                   help="model override (default: config distill_model, else driver default)")
    p.add_argument("--effort", default=None,
                   help="reasoning effort, codex driver only (default: config distill_effort)")
    p.add_argument("--project", default=None,
                   help="restrict to one project slug (one worker per project — ADR-013)")
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    cfg = config_mod.load()
    conn = index_db.connect(cfg.db_path)
    sessions = _iter_sessions(cfg, args.source, project=args.project, conn=conn)
    if sessions is None:
        conn.close()
        print("adapters not available yet — run `afterwit ingest` first (integration phase)",
              file=sys.stderr)
        return 0
    driver = make_driver(args.driver or cfg.distill_driver,
                         model=args.model or cfg.distill_model,
                         effort=args.effort or cfg.distill_effort)
    stats = distill_sessions(sessions, cfg, conn, budget=args.budget,
                             driver=driver, use_ledger=True)
    conn.close()
    print("distilled {sessions} sessions ({skipped} skipped): "
          "{write} written, {merge} merged, {supersede} superseded, "
          "{queue} queued".format(**stats))
    # Never let "distilled 11 sessions" stand alone when some of them were read
    # in part. The per-session detail is in the device log; this is the flag that
    # sends you there.
    if stats.get("truncated"):
        print("  {truncated} session(s) exceeded the {cap:,}-char prompt cap and were read "
              "in part (least: {worst}%) — see `distill-partial` in the device log"
              .format(cap=MAX_TRANSCRIPT_CHARS, worst=stats.get("worst_coverage", "?"),
                      truncated=stats["truncated"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
