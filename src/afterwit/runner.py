"""Nightly runner — `afterwit run`. ADR-015 (binding), SPEC §10/§14 (P4 "Learn").

The system learns without a human driving it: this sequencer drains the distill
backlog incrementally (per-session ledger, ADR-015 §1), mines whether served
knowledge was actually used, decays stale cards, enforces the kill-switch,
lints, checkpoints usage into the wiki, and takes a local git snapshot.

Non-negotiables:
- Stage order + fail-soft + single lock EXACTLY per ADR-015 §2/§3.
- The runner NEVER flips serving posture (§5): it does not register
  UserPromptSubmit, reset the kill-switch, or approve cards. It produces;
  humans and gates decide. (enforce_killswitch only DISABLES, never re-enables.)
- Every stage is fail-soft: an exception logs to the wiki log.md + stderr and
  later stages still run; only distill depends on ingest. Exit is nonzero if
  any stage failed.
- Overall --timeout minutes (default 50): if exceeded before distill, the LLM
  stage is skipped (logged) — the cheap closing stages still run.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import adapters, cli, config as config_mod, consolidate, distill, index_db, relink, wiki

STALE_LOCK_HOURS = 6
DISTILL_SOURCES = ("claude", "codex")  # memory/docs were one-shot (Gotcha #11)
INGEST_SOURCES = ("claude", "codex", "memory", "docs")


def _log(wiki_root: Path, line: str) -> None:
    log = wiki.log_path(wiki_root)  # per-device (ADR-019)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as f:
        f.write(f"- {datetime.now(timezone.utc).isoformat()} {line}\n")


# --- single-instance lock (ADR-015 §3) --------------------------------------

def _pid_is_alive(pid: int) -> bool:
    """Is `pid` a running process? Unknown-or-inaccessible counts as alive.

    `os.kill(pid, 0)` is the POSIX idiom and is actively WRONG on Windows: signal
    0 IS `CTRL_C_EVENT` there, so `os.kill` routes to `GenerateConsoleCtrlEvent`
    instead of probing anything (CPython `Modules/posixmodule.c`, `os_kill_impl`).
    It never reports a dead pid — so a stale lock could never be broken — and it
    raises Ctrl+C across the console, which took down the whole Windows CI run.
    """
    if sys.platform == "win32":
        import ctypes

        if not 0 < pid < 2**32:  # a torn lock file must read as stale, not crash
            return False
        PROCESS_QUERY_LIMITED_INFORMATION, STILL_ACTIVE, ERROR_ACCESS_DENIED = 0x1000, 259, 5
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        # Declared, not defaulted: a HANDLE is a pointer, and ctypes' default c_int
        # restype would truncate it on 64-bit.
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
        if not handle:
            return ctypes.get_last_error() == ERROR_ACCESS_DENIED  # alive, another user
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True  # cannot tell — never break a lock on a guess
            return code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, owned by another user
    except OSError:
        return False
    return True


def _lock_is_live(lock_path: Path, now: datetime) -> bool:
    """A lock is live unless it is older than STALE_LOCK_HOURS or its pid is
    dead. Unparseable content is treated as stale (safe to break)."""
    try:
        lines = lock_path.read_text(encoding="utf-8").splitlines()
        pid = int(lines[0])
    except (OSError, ValueError, IndexError):
        return False
    ts = adapters._parse_ts(lines[1]) if len(lines) > 1 else None
    if ts and (now - ts) > timedelta(hours=STALE_LOCK_HOURS):
        return False
    return _pid_is_alive(pid)


# --- stages -----------------------------------------------------------------

def _ingest_all(failures: list[str], wiki_root: Path) -> dict:
    """Ingest every source; one bad source never blocks the others."""
    summary: dict[str, object] = {}
    for src in INGEST_SOURCES:
        try:
            rows = adapters.ingest(src)
            summary[src] = sum(n for _, n, skipped in rows if not skipped)
        except Exception as e:  # noqa: BLE001 — fail-soft per source
            failures.append(f"ingest:{src}")
            _log(wiki_root, f"run-stage-fail ingest:{src}: {e!r}")
            summary[src] = "ERR"
    return summary


def _distill(cfg: config_mod.Config, conn, budget: int | None, driver_name: str) -> dict:
    """Distill claude+codex, newest-first across both, ledger-gated (ADR-015)."""
    distill.preflight_driver(driver_name)
    sessions: list = []
    for src in DISTILL_SOURCES:
        s = distill._iter_sessions(cfg, src, conn=conn)
        if s:
            sessions.extend(s)
    sessions.sort(key=lambda evs: max((getattr(e, "ts", "") or "") for e in evs),
                  reverse=True)
    driver = distill.make_driver(driver_name, model=cfg.distill_model,
                                 effort=cfg.distill_effort)
    stats = distill.distill_sessions(
        sessions, cfg, conn, budget=budget, driver=driver, use_ledger=True,
    )
    if stats["skipped"] and not stats["sessions"]:
        raise RuntimeError(
            f"distillation attempted but every session failed ({stats['skipped']} skipped)"
        )
    return stats


def _review(cfg: config_mod.Config, conn, deadline: float) -> dict:
    """Drain the review queue with the independent auto-reviewer (ADR-021, ADR-033).

    Only runs when the user set `auto_review = true`. Without this stage the nightly
    distilled INTO the queue every night and nothing ever drained it — the flag armed
    the manual `afterwit review` and the UI button but never the automated path, so the
    queue only grew. Time-bounded by the run deadline; a large backlog clears over
    several nights rather than blowing one run's clock.
    """
    if not cfg.auto_review:
        return {"skipped": "auto_review off"}
    from . import review
    review.ui.enqueue_unverified(conn, cfg.wiki_root)
    return review.review_queue(cfg, conn, deadline=deadline)


def _sync_or_raise() -> str:
    rc = cli._cmd_sync(None)
    if rc:
        raise RuntimeError(f"afterwit sync failed (exit {rc}) — see stderr")
    return "snapshot"


def _record_projects(cfg: config_mod.Config, conn) -> dict:
    """Stamp each project's identity (origin URL) + position (HEAD) into the
    index (ADR-018). repo_url is the cross-device key; HEAD is what the next
    run measures drift against."""
    from . import gitmeta

    n = 0
    if cfg.projects_root.is_dir():
        for child in sorted(cfg.projects_root.iterdir()):
            if not child.is_dir():
                continue
            head = gitmeta.head_commit(child)
            if head is None:
                continue  # not a git checkout — nothing to anchor
            index_db.record_project(conn, child.name, gitmeta.remote_url(child), head)
            n += 1
    conn.commit()
    return {"projects": n}


def _lint_summary(cfg: config_mod.Config, conn) -> dict:
    findings = consolidate.lint(conn, projects_root=cfg.projects_root,
                            aliases=cfg.project_aliases)
    counts = {k: len(v) for k, v in findings.items()}
    if any(counts.values()):
        _log(cfg.wiki_root, f"run-lint {counts}")
    return counts


def _regenerate(cfg: config_mod.Config, conn) -> str:
    wiki.regenerate(cfg, conn)
    return "index+briefs"


# --- orchestration ----------------------------------------------------------

def _doctor(cfg: config_mod.Config) -> dict:
    """Nightly reachability check — the loop-closer.

    Every fault that has actually bitten us was a SILENT config fault: the index was
    healthy and simply unreachable, and nothing said so until a confused agent did,
    days later. The nightly is the only thing that runs unattended, so it is the only
    place that can notice first. Raising here makes the stage record a failure, which
    makes `afterwit run` exit nonzero, which makes systemd mark the unit failed —
    instead of quietly distilling into an index no agent can open.

    Known blind spot, and it is inherent (ADR-024): this CANNOT catch a moved
    checkout, because relocating the repo kills this systemd unit's ExecStart too, so
    the nightly never runs to complain. Detecting that needs something outside
    afterwit; `afterwit doctor --fix` remains the manual recovery.
    """
    import argparse

    from . import cli
    if cli._cmd_doctor(argparse.Namespace(fix=False)) != 0:
        _log(cfg.wiki_root, "run-doctor FAIL — agents cannot reach afterwit (see output above)")
        raise RuntimeError("afterwit is not reachable by agents — run: afterwit doctor")
    return {"reachable": True}


def run(*, budget: int | None = None, driver_name: str | None = None,
        timeout_min: int = 50, cfg: config_mod.Config | None = None,
        remine: bool = False) -> int:
    cfg = cfg or config_mod.load()
    driver_name = driver_name or cfg.distill_driver
    lock_path = cfg.db_path.with_name("run.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)

    if lock_path.exists():
        if _lock_is_live(lock_path, now):
            print("[run] already running (live lock) — nothing to do")
            return 0
        _log(cfg.wiki_root, f"run: breaking stale lock {lock_path}")
        print("[run] broke stale lock")
    lock_path.write_text(f"{os.getpid()}\n{now.isoformat()}\n", encoding="utf-8")

    failures: list[str] = []
    start = time.monotonic()
    deadline = start + timeout_min * 60

    def stage(name: str, fn) -> None:
        try:
            print(f"[run] {name}: {fn()}")
        except Exception as e:  # noqa: BLE001 — fail-soft; later stages still run
            failures.append(name)
            _log(cfg.wiki_root, f"run-stage-fail {name}: {e!r}")
            print(f"[run] {name}: FAILED {e!r}", file=sys.stderr)

    try:
        stage("ingest", lambda: _ingest_all(failures, cfg.wiki_root))

        conn = None
        try:
            conn = index_db.connect(cfg.db_path)
        except Exception as e:  # noqa: BLE001
            failures.append("connect")
            _log(cfg.wiki_root, f"run-stage-fail connect: {e!r}")
            print(f"[run] connect: FAILED {e!r}", file=sys.stderr)

        if conn is not None:
            stage("projects", lambda: _record_projects(cfg, conn))

        if time.monotonic() < deadline:
            if conn is not None:
                stage("distill", lambda: _distill(cfg, conn, budget, driver_name))
        else:
            _log(cfg.wiki_root, "run: distill SKIPPED (timeout exceeded)")
            print("[run] distill: SKIPPED (timeout exceeded)")

        if conn is not None:
            flag = cfg.db_path.with_name("inject.disabled")
            # Review before write_back/regenerate/sync so newly-approved cards are
            # indexed and pushed in this same run. LLM work like distill, so it is
            # deadline-bounded and skipped once the clock is spent.
            if time.monotonic() < deadline:
                stage("review", lambda: _review(cfg, conn, deadline))
            else:
                _log(cfg.wiki_root, "run: review SKIPPED (timeout exceeded)")
                print("[run] review: SKIPPED (timeout exceeded)")
            if remine:
                # Before mining, not instead of it: `usefulness` accumulates, so a
                # changed `card_was_used` would otherwise stack new verdicts on top
                # of the old miner's. Explicit `feedback` rows survive and replay.
                stage("reset_usage", lambda: consolidate.reset_usage(conn))
            stage("mine_servings",
                  lambda: consolidate.mine_servings(conn, adapters.session_text_lookup))
            stage("apply_decay", lambda: {"decayed": consolidate.apply_decay(conn)})
            stage("killswitch", lambda: {"disabled": consolidate.enforce_killswitch(conn, flag)})
            # Anchor any card still missing source_commit/repo_url BEFORE lint,
            # so drift is computed against a real commit rather than falling back
            # to the dead-pointer check forever (ADR-020 D3: this was dead code).
            stage("backfill",
                  lambda: {"anchored": consolidate.backfill_anchors(conn, cfg.projects_root,
                                                              cfg.project_aliases)})
            # LLM work like distill/review: deadline-bounded, and off (budget 0)
            # until the hand-judged precision sweep passes (ADR-045). Before
            # regenerate so the graph page sees tonight's links.
            if cfg.relink_budget:
                if time.monotonic() < deadline:
                    stage("relink", lambda: relink.run_stage(cfg, conn))
                else:
                    _log(cfg.wiki_root, "run: relink SKIPPED (timeout exceeded)")
                    print("[run] relink: SKIPPED (timeout exceeded)")
            stage("lint", lambda: _lint_summary(cfg, conn))
            stage("write_back", lambda: {"updated": consolidate.write_back_usage(conn, cfg.wiki_root)})
            stage("regenerate", lambda: _regenerate(cfg, conn))
            conn.close()

        # Last stage: git snapshot + pull/push. Reuses cli._cmd_sync's logic (its
        # own write-back is idempotent) rather than re-implementing git here; the
        # arg is unused by that function. A nonzero return means the pull/push
        # failed — raise so the stage records a failure instead of the old silent
        # "[run] sync: snapshot" success on a conflicted repo (ADR-019).
        stage("sync", _sync_or_raise)

        # LAST, deliberately: sync rebuilds the index, so this validates the state
        # the next session will actually meet — not the state we started from.
        stage("doctor", lambda: _doctor(cfg))
    finally:
        lock_path.unlink(missing_ok=True)

    dur = time.monotonic() - start
    print(f"[run] done in {dur:.1f}s, failures={failures or 'none'}")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="afterwit run")
    p.add_argument("--budget", type=int, default=None, help="max sessions driven this run")
    p.add_argument("--driver", choices=sorted(distill.DRIVERS), default=None,
                   help="LLM driver (default: config distill_driver, else claude-p)")
    p.add_argument("--timeout", type=int, default=50, help="overall soft cap in minutes")
    p.add_argument("--remine", action="store_true",
                   help="discard mined usage verdicts and re-judge every serving "
                        "from scratch (run this after changing the usage miner)")
    args = p.parse_args(argv if argv is not None else sys.argv[1:])
    return run(budget=args.budget, driver_name=args.driver, timeout_min=args.timeout,
               remine=args.remine)


if __name__ == "__main__":
    raise SystemExit(main())
