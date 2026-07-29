"""Hook entrypoint. SPEC §9.2. Latency- and safety-critical.

Contract (non-negotiable):
- FAIL OPEN: any internal error → empty stdout, exit 0. Exit 2 in a
  UserPromptSubmit hook blocks AND ERASES the user's prompt (ADR gotcha #5);
  this module must never do that.
- Hard caps: ≤ cfg.inject_max_cards cards, ≤ cfg.inject_max_tokens tokens.
  Emitting nothing is the common case (Manifesto P3).
- No heavy imports at module load — hook path must stay < 200ms p95.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone


def _flag(argv: list[str], name: str, default: str) -> str:
    """`--name X`, tolerating a trailing `--name` with nothing after it — the hook
    path fails open, so a malformed argv must degrade, never raise."""
    if name in argv:
        i = argv.index(name) + 1
        if i < len(argv):
            return argv[i]
    return default


def _mode(argv: list[str]) -> str:
    return _flag(argv, "--mode", "prompt")


def _harness(argv: list[str]) -> str:
    """Which CLI is calling. Passed explicitly by the installer rather than
    sniffed: both harnesses send the same payload keys and neither sets a
    distinguishing env var, so there is nothing to infer from (ADR-040)."""
    return _flag(argv, "--harness", "claude")


def _tokens(text: str) -> int:
    return max(len(text) // 4, 1)  # chars/4 approximation; good enough for budgeting


def _format_card(s, max_body_tokens: int = 150) -> str:
    body = " ".join(s.body.split())
    if _tokens(body) > max_body_tokens:
        body = body[: max_body_tokens * 4].rsplit(" ", 1)[0] + "…"
    date = (s.updated or "")[:10]
    flag = "" if s.verified else " UNVERIFIED — not human-reviewed"
    # The id is what makes the card actionable rather than merely readable: the
    # `feedback` MCP tool takes a card_id, and without one here the agent can
    # only rate cards it pulled itself. Push is the majority of exposures, so
    # omitting it starved the explicit-signal channel of exactly the turns where
    # a card had just proved (or failed to prove) useful. ~7 tokens; the mined
    # used/ignored outcome stays the automatic signal underneath (SPEC §10).
    return (f"• [{s.type}{flag}] {s.title} — {body} "
            f"({s.project}, {date}, id: {s.id})")


def _serve(query: str, payload: dict, cfg, conn, *, header: str, log_mode: str,
           harness: str = "claude", errors_first: bool = False) -> str:
    """Search → rank → cap → log. The ONE place the Manifesto P3 caps live.

    Shared by prompt and error mode deliberately: a hard cap that exists in two
    copies is a hard cap that will disagree with itself the first time one is
    edited.
    """
    from . import index_db, rank
    from .config import project_from_cwd

    project = project_from_cwd(payload.get("cwd") or ".", cfg.projects_root,
                               cfg.project_aliases)
    rows = index_db.search(conn, query, project=project, k=20)
    # Push is a zero-consent surface: only human-verified cards may be injected
    # (ADR-011). Unverified cards remain reachable via pull tools, where the
    # agent asked and ranking labels them. push_unverified=true is the explicit
    # high-risk opt-out (config.py disclosure); opted-in cards get labeled.
    if not cfg.push_unverified:
        rows = [r for r in rows if r["verified"]]
    # Push spends attention nobody asked for, so it is restricted to types that can
    # change the next action (config.push_types). Everything else stays reachable
    # via the pull tools. Applied BEFORE ranking, not after: filtering the top-k
    # would let three doc_refs win the slots and then leave the prompt silent.
    rows = [r for r in rows if r["type"] in cfg.push_types]
    scored = rank.rank(rows, project, floor=cfg.floor, k=cfg.inject_max_cards,
                       query_text=query,
                       # `errors_first` already means "this is an error lookup",
                       # and an error signature is project-independent — see the
                       # CROSS_PROJECT_FACTOR note in rank.py for the serving
                       # that proved it.
                       cross_project=1.0 if errors_first else rank.CROSS_PROJECT_FACTOR)
    if errors_first:
        # A recorded fix outranks a merely topical card when something just broke
        # (SPEC §9.1, same bias as the lookup_error pull tool). Stable within groups.
        scored.sort(key=lambda s: s.type != "error_fix")
    if not scored:
        return ""
    lines, budget = [], cfg.inject_max_tokens - 25  # header reserve
    for s in scored:
        line = _format_card(s)
        if _tokens(line) > budget:
            break
        lines.append(line)
        budget -= _tokens(line)
    if not lines:
        return ""
    try:
        index_db.log_serving(
            conn, ts=datetime.now(timezone.utc).isoformat(), harness=harness,
            session_id=payload.get("session_id", ""), mode=log_mode,
            query=query, card_ids=[s.id for s in scored[: len(lines)]],
        )
    except Exception:
        pass  # serving log is best-effort; never fail the hook over it
    return header + "\n" + "\n".join(lines)


def _prompt_mode(payload: dict, cfg, conn, harness: str = "claude") -> str:
    prompt = payload.get("prompt") or ""
    if len(prompt.strip()) < 12:  # trivial prompts ("yes", "continue") never inject
        return ""
    return _serve(prompt, payload, cfg, conn, log_mode="inject", harness=harness,
                  header="Relevant knowledge from your past sessions "
                         "(verify before relying):")


def _error_mode(payload: dict, cfg, conn, harness: str = "claude") -> str:
    """PostToolUseFailure/Bash. Fires only on red, which is the whole point:
    the cheapest moment to surface a recorded fix is the moment it broke.

    The event carries ONE flat `error` string, not an exit code and streams —
    the CLI builds it as ["Exit code N", <interrupt>, stderr, stdout] joined by
    newlines, so stderr leads and is the part worth matching on.
    """
    if payload.get("is_interrupt"):
        return ""  # the user pressed escape; nothing diagnosable happened
    err = (payload.get("error") or "").strip()
    # `error` also carries permission denials and classifier refusals ("Blocked:
    # …", "This command requires approval"). Those are not bugs and have no fix
    # to recall, so only a genuine non-zero exit gets looked up.
    if not err.startswith("Exit code "):
        return ""
    detail = err.split("\n", 1)[1].strip() if "\n" in err else ""
    if len(detail) < 12:
        return ""  # "Exit code 1" with no output — nothing to match against
    # The COMMAND is evidence too, and unlike the output it cannot be truncated
    # away: `| tail -3` or a pytest summary drops the signature and this hook
    # goes quiet on a failure it has a card for. It also rescues the case where
    # the output carries no signature at all — a real serving matched on
    # "Command timed out after 2m 0s" and returned an unrelated card, because
    # that string is all the failure said. PREPENDED because `rank._coverage`
    # scores only the first 12 distinct tokens: ahead of the output, the command's
    # tokens are always inside that window; behind a stack trace they never are.
    # It cuts both ways by design — a command the card does not mention dilutes
    # coverage and the result is silence, which beats a worse match (P3).
    cmd = str((payload.get("tool_input") or {}).get("command") or "").strip()
    query = f"{cmd[:200]}\n{detail}" if cmd else detail
    return _serve(query[:2000], payload, cfg, conn, log_mode="error",
                  harness=harness, errors_first=True,
                  header="afterwit has seen this error before "
                         "(verify before relying):")


def _session_mode(payload: dict, cfg, conn) -> str:
    from .config import project_from_cwd

    project = project_from_cwd(payload.get("cwd") or ".", cfg.projects_root,
                               cfg.project_aliases)
    if project == "global":
        return ""
    counts = conn.execute(
        "SELECT type, COUNT(*) n FROM cards WHERE project=? AND status='active' "
        "AND verified=1 GROUP BY type",  # push surface: verified only (ADR-011)
        (project,),
    ).fetchall()
    if not counts:
        return ""
    gotchas = conn.execute(
        """SELECT title FROM cards WHERE project=? AND status='active' AND verified=1
           AND type IN ('gotcha','error_fix') ORDER BY usefulness DESC, updated DESC LIMIT 3""",
        (project,),
    ).fetchall()
    parts = [f"afterwit knows this project ({project}): "
             + ", ".join(f"{r['n']} {r['type']}s" for r in counts) + "."]
    if gotchas:
        parts.append("Top gotchas: " + "; ".join(r["title"] for r in gotchas) + ".")
    parts.append("Deep history on demand: MCP tools recall / why / lookup_error / for_file.")
    text = "\n".join(parts)
    return text if _tokens(text) <= cfg.session_max_tokens else parts[0]


def run(argv: list[str], stdin_text: str) -> str:
    """Pure-ish core, separated for tests. Raises on error; main() fails open."""
    from . import config as config_mod
    from . import index_db

    # afterwit's own distiller/reviewer child (distill._child_env). It inherits
    # the user's hooks, so without this afterwit injects into itself — and a
    # reviewer can never "use" an injected card, so every such serving scores
    # `ignored`, poisons the kill-switch hit rate, and decays the served cards.
    # That is what disabled injection on 2026-07-26 (ADR-037, Gotcha #59).
    # Checked before anything else: cheapest possible exit on a p95<200ms path.
    if os.environ.get("AFTERWIT_INTERNAL"):
        return ""

    mode = _mode(argv)
    payload = json.loads(stdin_text) if stdin_text.strip() else {}
    cfg = config_mod.load()
    if not cfg.db_path.exists():
        return ""
    # Kill-switch (SPEC §10). Covers error mode too: when the gate has decided
    # push cannot prove its value, silence means silence on every push surface.
    # Error servings still log under mode='error', so the two are measured
    # separately — killswitch_status only ever counts mode='inject'.
    if mode in ("prompt", "error") and cfg.db_path.with_name("inject.disabled").exists():
        return ""
    conn = index_db.connect(cfg.db_path, readonly=(mode == "session"))
    try:
        if mode == "session":
            return _session_mode(payload, cfg, conn)
        harness = _harness(argv)
        if mode == "error":
            return _error_mode(payload, cfg, conn, harness)
        return _prompt_mode(payload, cfg, conn, harness)
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    try:
        out = run(argv, sys.stdin.read())
        if not out:
            return 0
        if _mode(argv) == "error":
            # Plain stdout is DISCARDED on PostToolUseFailure — only SessionStart
            # and UserPromptSubmit render it. This envelope is the ONLY way the
            # text reaches the model, and `hookEventName` must equal the firing
            # event exactly or the CLI rejects the output outright (ADR-038).
            #
            # Codex needs no branch of its own: verified 2026-07-27 that it
            # renders bare hook stdout as context on UserPromptSubmit exactly as
            # Claude does, so one print serves both harnesses (ADR-040).
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PostToolUseFailure", "additionalContext": out}}))
        else:
            print(out)
    except Exception:
        pass  # fail open — a broken index must never break the user's session
    return 0
