"""Tests for the nightly runner (ADR-015): ledger skip/rehash, stage isolation,
lock behaviour, real mine_servings wiring, `afterwit install cron`, rebuild survival.

Events are duck-typed (SPEC §6) — a local dataclass matches the contract rather
than importing afterwit.events, same convention as test_distill.py."""
import pytest
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from afterwit import (adapters, cards, cli, config, consolidate, distill, index_db,
                install, relink, runner, wiki)
from tests.conftest import fake_home
from tests.test_cards import make_card


@dataclass
class Ev:
    text: str
    role: str = "assistant"
    kind: str = "assistant"
    source_path: str = "a.jsonl"
    lines: str = "L1"
    project: str = "acme_hr"
    ts: str = "2026-07-05T00:00:00Z"
    meta: dict = field(default_factory=lambda: {"harness": "claude", "model": "claude-opus-4-8"})


def _cfg(tmp_path):
    cfg = config.Config(wiki_root=tmp_path / "wiki", db_path=tmp_path / "index.db",
                        projects_root=tmp_path / "Projects")
    cfg.wiki_root.mkdir(parents=True, exist_ok=True)
    return cfg


def _conn(cfg):
    return index_db.connect(cfg.db_path)


def _driver(*responses):
    calls = {"n": 0}
    it = iter(responses)

    def run(prompt):
        calls["n"] += 1
        return next(it)
    run.calls = calls  # type: ignore[attr-defined]
    return run


# --- R3: ledger skip on unchanged, rehash re-eligibilizes -------------------

def test_ledger_skips_unchanged_and_rehash_reeligibilizes(tmp_path):
    cfg = _cfg(tmp_path)
    conn = _conn(cfg)
    sess = [Ev("prisma P1001 cannot reach db", role="user", kind="user"),
            Ev("set host to 127.0.0.1; migrate ok")]

    d1 = _driver("[]")
    distill.distill_sessions([sess], cfg, conn, driver=d1, use_ledger=True, budget=5)
    assert d1.calls["n"] == 1  # driven once (0 cards still records the ledger row)

    d2 = _driver("[]")  # would raise StopIteration if called
    stats = distill.distill_sessions([sess], cfg, conn, driver=d2, use_ledger=True, budget=5)
    assert d2.calls["n"] == 0 and stats["ledger_skipped"] == 1 and stats["sessions"] == 0

    grown = sess + [Ev("and the fix stuck across restart")]  # session grew → new hash
    d3 = _driver("[]")
    distill.distill_sessions([grown], cfg, conn, driver=d3, use_ledger=True, budget=5)
    assert d3.calls["n"] == 1


def test_driver_failure_records_no_ledger_row(tmp_path):
    cfg = _cfg(tmp_path)
    conn = _conn(cfg)
    sess = [Ev("boom")]
    d = _driver("not json", "still not json")  # both attempts fail parse
    stats = distill.distill_sessions([sess], cfg, conn, driver=d, use_ledger=True, budget=5)
    assert stats["skipped"] == 1
    assert conn.execute("SELECT COUNT(*) FROM distilled").fetchone()[0] == 0
    # still eligible next run
    d2 = _driver("[]")
    distill.distill_sessions([sess], cfg, conn, driver=d2, use_ledger=True, budget=5)
    assert d2.calls["n"] == 1


# --- R8: ledger survives a full rebuild -------------------------------------

def test_ledger_survives_rebuild(tmp_path):
    cfg = _cfg(tmp_path)
    conn = _conn(cfg)
    distill.ensure_distilled_ledger(conn)
    conn.execute("INSERT INTO distilled(source,content_hash,ts,cards) VALUES('a','h','t',2)")
    conn.commit()
    index_db.rebuild(conn, cfg.wiki_root)  # wipes cards/fts/links only
    row = conn.execute("SELECT source, cards FROM distilled").fetchone()
    assert row["source"] == "a" and row["cards"] == 2


# --- R6: mine_servings wired to the real session_text_lookup ----------------

def test_mine_servings_via_real_lookup(tmp_path, monkeypatch):
    fake_home(monkeypatch, tmp_path)
    monkeypatch.delenv("AFTERWIT_CONFIG", raising=False)
    sid = "11111111-2222-3333-4444-555555555555"
    proj = tmp_path / ".claude" / "projects" / "-home-x-Projects-acme_hr"
    proj.mkdir(parents=True)
    rec = {"type": "assistant", "timestamp": "2026-07-06T11:00:00Z", "uuid": "u1",
           "message": {"role": "assistant", "model": "claude-opus-4-8",
                       "content": [{"type": "text",
                                    "text": "applied the fix: prisma P1001 on WSL2 "
                                            "use 127.0.0.1 in DATABASE_URL"}]}}
    (proj / f"{sid}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")

    c = make_card(title="Prisma P1001 WSL2 127.0.0.1", type="error_fix",
                  body="Fix: use 127.0.0.1 in DATABASE_URL", verified=True)
    wiki = tmp_path / "wiki"
    cards.save(c, wiki)
    conn = index_db.connect(tmp_path / "index.db")
    index_db.rebuild(conn, wiki)
    index_db.log_serving(conn, ts="2026-07-06T10:00:00+00:00", harness="claude",
                         session_id=sid, mode="inject", query="q", card_ids=[c.id])

    counts = consolidate.mine_servings(conn, adapters.session_text_lookup)
    assert counts["used"] == 1 and counts["skipped"] == 0
    assert conn.execute("SELECT usefulness FROM cards WHERE id=?", (c.id,)).fetchone()[0] == 1.0

    # transcript absent → skipped, serving stays unmined for the next run
    index_db.log_serving(conn, ts="2026-07-06T10:00:00+00:00", harness="claude",
                         session_id="no-such-session", mode="inject", query="q", card_ids=[c.id])
    counts2 = consolidate.mine_servings(conn, adapters.session_text_lookup)
    assert counts2["skipped"] == 1
    assert conn.execute("SELECT COUNT(*) FROM servings WHERE outcome IS NULL").fetchone()[0] == 1


# --- R4/R5: runner stage isolation + lock -----------------------------------

def _seeded_cfg(tmp_path):
    cfg = _cfg(tmp_path)
    cards.save(make_card(title="Seed card", verified=True), cfg.wiki_root)
    conn = index_db.connect(cfg.db_path)
    index_db.rebuild(conn, cfg.wiki_root)
    conn.close()
    return cfg


def _neutralize(monkeypatch):
    """Keep the runner off the real ~/.claude and off git during lock/stage tests."""
    monkeypatch.setattr(adapters, "ingest", lambda src: [])
    monkeypatch.setattr(distill, "_iter_sessions", lambda *a, **k: None)
    # The distill stage resolves the driver binary BEFORE it looks for sessions, so
    # a machine without Claude Code installed — every CI runner, every new
    # contributor — failed the stage and returned rc=1 from an otherwise stubbed run.
    # Resolution is all that happens here; _iter_sessions returning None means the
    # driver is never invoked.
    monkeypatch.setattr(distill, "driver_executable", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(cli, "_cmd_sync", lambda *a: 0)
    # Stub the CHECK, not runner._doctor — the wiring stays real in every test, but
    # we don't read the real ~/.claude or spawn a subprocess per runner test.
    monkeypatch.setattr(cli, "_cmd_doctor", lambda args: 0)


def test_unreachable_afterwit_fails_the_nightly(tmp_path, monkeypatch):
    """The loop-closer. A healthy index that no agent can open must not produce a
    silently-green nightly — that is precisely how the last outage stayed invisible
    for days while `run` reported success every night."""
    cfg = _seeded_cfg(tmp_path)
    _neutralize(monkeypatch)
    monkeypatch.setattr(cli, "_cmd_doctor", lambda args: 1)  # agents cannot reach it

    rc = runner.run(cfg=cfg, budget=1)

    assert rc == 1
    log = wiki.log_path(cfg.wiki_root).read_text()
    assert "run-doctor FAIL" in log
    assert "run-stage-fail doctor" in log
    assert (cfg.wiki_root / "index.md").exists()  # and the run still did its work


def test_stage_failure_is_isolated(tmp_path, monkeypatch):
    cfg = _seeded_cfg(tmp_path)
    _neutralize(monkeypatch)
    monkeypatch.setattr(consolidate, "apply_decay",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("decay exploded")))
    ran_later = {"write_back": False}
    monkeypatch.setattr(consolidate, "write_back_usage",
                        lambda conn, root: ran_later.__setitem__("write_back", True) or 0)

    rc = runner.run(cfg=cfg, budget=1)

    assert rc == 1  # a stage failed → nonzero
    assert ran_later["write_back"] is True  # a stage AFTER the failure still ran
    assert "run-stage-fail apply_decay" in wiki.log_path(cfg.wiki_root).read_text()
    assert (cfg.wiki_root / "index.md").exists()  # regenerate ran too
    assert not cfg.db_path.with_name("run.lock").exists()  # lock released


def test_relink_stage_runs_only_when_budget_is_set(tmp_path, monkeypatch):
    """Paired on/off through the REAL runner (Gotcha #79: a test of run_stage
    alone cannot see whether the runner still calls it, or still gates it)."""
    cfg = _seeded_cfg(tmp_path)
    _neutralize(monkeypatch)
    calls: list[object] = []
    monkeypatch.setattr(relink, "run_stage",
                        lambda c, conn: calls.append(c) or {"judged": 0})

    runner.run(cfg=cfg, budget=1)
    assert calls == []  # default relink_budget=0 — the LLM judge must not run

    cfg.relink_budget = 5
    runner.run(cfg=cfg, budget=1)
    assert calls == [cfg]  # on when configured, and handed the real cfg


def test_nightly_drains_the_queue_only_when_auto_review_is_on(tmp_path, monkeypatch):
    """The gap that let the queue grow to 257. The nightly distills INTO the review
    queue every night; without a review stage nothing ever drained it, so
    `auto_review = true` armed only the manual command and the UI button — never the
    automated path the user expected. Wire it in, gated on the flag.
    """
    from afterwit import review, ui

    # A queued card with real evidence so the (stubbed) reviewer can approve it.
    src = tmp_path / "ev.md"
    src.write_text("the fix that worked\n")

    def seed_queue(cfg):
        conn = index_db.connect(cfg.db_path)
        ui.queue_insert(conn, make_card(title="Queued finding",
                                        sources=[{"path": str(src), "lines": "L1-L1"}]), "x")
        conn.commit()
        return conn

    # auto_review OFF: the queued card must still be queued after a full run.
    cfg = _seeded_cfg(tmp_path)
    _neutralize(monkeypatch)
    cfg.auto_review = False
    seed_queue(cfg).close()
    monkeypatch.setattr(review, "review_one",
                        lambda *a, **k: pytest.fail("reviewer ran with auto_review OFF"))
    assert runner.run(cfg=cfg, budget=1) == 0
    conn = index_db.connect(cfg.db_path)
    assert conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0] == 1
    conn.close()

    # auto_review ON: the same card is judged and drained.
    cfg2 = _seeded_cfg(tmp_path / "b")
    cfg2.auto_review = True
    seed_queue(cfg2).close()
    monkeypatch.setattr(review, "review_one",
                        lambda cfg, card, **k: review.Verdict("approve", "ok", "test-model"))
    assert runner.run(cfg=cfg2, budget=1) == 0
    conn = index_db.connect(cfg2.db_path)
    assert conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0] == 0
    conn.close()


def test_live_lock_blocks_second_run(tmp_path, monkeypatch):
    cfg = _seeded_cfg(tmp_path)
    _neutralize(monkeypatch)
    lock = cfg.db_path.with_name("run.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(f"{os.getpid()}\n{datetime.now(timezone.utc).isoformat()}\n")

    rc = runner.run(cfg=cfg, budget=1)

    assert rc == 0
    assert not (cfg.wiki_root / "index.md").exists()  # did nothing
    assert lock.exists()  # the holder's lock is untouched


def test_stale_lock_is_broken(tmp_path, monkeypatch):
    cfg = _seeded_cfg(tmp_path)
    _neutralize(monkeypatch)
    lock = cfg.db_path.with_name("run.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    old = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
    lock.write_text(f"999999\n{old}\n")  # dead pid + >6h old

    rc = runner.run(cfg=cfg, budget=1)

    assert rc == 0  # neutralized stages, no failures
    assert "breaking stale lock" in wiki.log_path(cfg.wiki_root).read_text()
    assert (cfg.wiki_root / "index.md").exists()  # it actually ran
    assert not lock.exists()  # released on exit


def test_fresh_lock_with_a_dead_pid_is_broken(tmp_path, monkeypatch):
    """The pid probe must answer, not just the 6h TTL. `os.kill(pid, 0)` did not on
    Windows — signal 0 IS CTRL_C_EVENT, so it never reported a dead pid (and raised
    Ctrl+C across the console). Every lock would have looked live for 6 hours."""
    assert runner._pid_is_alive(os.getpid()) is True
    assert runner._pid_is_alive(999_999_999) is False

    cfg = _seeded_cfg(tmp_path)
    _neutralize(monkeypatch)
    lock = cfg.db_path.with_name("run.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    fresh = datetime.now(timezone.utc).isoformat()
    lock.write_text(f"999999999\n{fresh}\n")  # dead pid, well inside the TTL

    assert runner.run(cfg=cfg, budget=1) == 0
    assert "breaking stale lock" in wiki.log_path(cfg.wiki_root).read_text()


# --- R7: `afterwit install cron` --------------------------------------------------

def test_install_cron_systemd_backup_first_idempotent(tmp_path):
    sd = tmp_path / "systemd"
    repo = install._repo_root()
    r1 = install.install_cron(systemd_dir=sd, repo=repo, activate=False, use_systemd=True)
    svc, tmr = sd / "afterwit.service", sd / "afterwit.timer"
    assert svc.exists() and tmr.exists()
    assert "afterwit run" in svc.read_text() and "Type=oneshot" in svc.read_text()
    assert "OnCalendar=*-*-* 02:30:00" in tmr.read_text()
    assert set(r1["changed"]) == {str(svc), str(tmr)} and r1["backed_up"] == []

    before = svc.read_text()
    r2 = install.install_cron(systemd_dir=sd, repo=repo, activate=False, use_systemd=True)
    assert r2["changed"] == [] and r2["backed_up"] == []  # byte-identical no-op
    assert svc.read_text() == before


def test_install_cron_crontab_fallback(tmp_path):
    store = {"txt": "# my existing crontab\n"}
    kw = dict(repo=install._repo_root(), use_systemd=False, activate=True,
              crontab_get=lambda: store["txt"],
              crontab_set=lambda t: store.__setitem__("txt", t))
    r = install.install_cron(**kw)
    assert r["mode"] == "cron" and r["changed"] == ["crontab"]
    assert "# my existing crontab" in store["txt"]  # untouched line preserved
    assert "30 2 * * *" in store["txt"] and install.TOML_BEGIN in store["txt"]
    r2 = install.install_cron(**kw)
    assert r2["changed"] == []  # idempotent


def test_sync_failure_is_reported_not_swallowed(monkeypatch):
    """A failed pull/push left the wiki mid-rebase while `afterwit run` printed
    '[run] sync: snapshot' as success (ADR-019). It must raise so stage() records
    a failure and the run exits nonzero."""
    monkeypatch.setattr(runner.cli, "_cmd_sync", lambda _a: 1)
    with pytest.raises(RuntimeError, match="afterwit sync failed"):
        runner._sync_or_raise()

    monkeypatch.setattr(runner.cli, "_cmd_sync", lambda _a: 0)
    assert runner._sync_or_raise() == "snapshot"


def test_distill_fails_when_every_attempt_is_skipped(tmp_path, monkeypatch):
    cfg = _seeded_cfg(tmp_path)
    conn = index_db.connect(cfg.db_path)
    monkeypatch.setattr(distill, "preflight_driver", lambda name: "/bin/driver")
    monkeypatch.setattr(distill, "_iter_sessions", lambda *a, **k: [[object()]])
    monkeypatch.setattr(distill, "distill_sessions", lambda *a, **k: {
        "sessions": 0, "skipped": 1, "write": 0, "merge": 0,
        "supersede": 0, "queue": 0,
    })
    with pytest.raises(RuntimeError, match="every session failed"):
        runner._distill(cfg, conn, 1, "codex")


def test_nightly_actually_calls_backfill_before_lint(tmp_path, monkeypatch):
    """D3: backfill_anchors() was correct, tested, and NEVER CALLED by production
    code. A feature no stage invokes passes every unit test it has. Pin the wiring,
    and pin the order — anchoring must precede lint or drift is computed against
    nothing."""
    cfg = _seeded_cfg(tmp_path)
    _neutralize(monkeypatch)
    calls = []
    real_backfill = consolidate.backfill_anchors
    monkeypatch.setattr(consolidate, "backfill_anchors",
                        lambda conn, root, *a: calls.append("backfill") or real_backfill(conn, root, *a))
    monkeypatch.setattr(consolidate, "lint",
                        lambda *a, **k: calls.append("lint") or
                        {"broken_links": [], "stale_unverified": [], "code_drift": []})

    runner.run(cfg=cfg, budget=1)

    assert "backfill" in calls, "backfill_anchors is dead code again"
    assert calls.index("backfill") < calls.index("lint")


def test_codex_servings_resolve_against_the_codex_transcript(tmp_path, monkeypatch):
    """Since ADR-040 the Codex hooks log servings too, and Codex stores its
    transcripts somewhere else under a different filename:
    `sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl`, not `<uuid>.jsonl`.

    Resolve those against ~/.claude/projects and every Codex serving comes back
    None -> "skipped" -> `outcome IS NULL` forever: never judged, never counted
    by the kill-switch, and re-scanned on every nightly run.
    """
    fake_home(monkeypatch, tmp_path)
    monkeypatch.delenv("AFTERWIT_CONFIG", raising=False)
    sid = "019fa329-d64c-7191-a8fa-7c17438194a4"
    day = tmp_path / ".codex" / "sessions" / "2026" / "07" / "27"
    day.mkdir(parents=True)
    (day / f"rollout-2026-07-27T14-40-55-{sid}.jsonl").write_text("\n".join([
        json.dumps({"type": "session_meta", "payload": {
            "cwd": "/home/x/Projects/acme_hr", "model_provider": "openai"}}),
        json.dumps({"type": "turn_context", "payload": {"model": "gpt-5.6-sol"}}),
        json.dumps({"type": "event_msg", "timestamp": "2026-07-27T15:00:00Z",
                    "payload": {"type": "agent_message",
                                "message": "applied it: pointed DATABASE_URL at the "
                                           "loopback address, the WSL2 bridge "
                                           "refuses localhost"}}),
    ]) + "\n", encoding="utf-8")

    # The body has to carry tokens the session can echo: since 2026-07-29 the
    # miner scores what the card ADDED, and the title is explicitly excluded.
    c = make_card(title="Prisma P1001 WSL2 127.0.0.1", type="error_fix",
                  body="Fix: point DATABASE_URL at the loopback address; the WSL2 "
                       "bridge refuses localhost.", verified=True)
    wiki = tmp_path / "wiki"
    cards.save(c, wiki)
    conn = index_db.connect(tmp_path / "index.db")
    index_db.rebuild(conn, wiki)
    index_db.log_serving(conn, ts="2026-07-27T14:41:00+00:00", harness="codex",
                         session_id=sid, mode="inject", query="q", card_ids=[c.id])

    counts = consolidate.mine_servings(conn, adapters.session_text_lookup)
    assert counts == {"used": 1, "ignored": 0, "skipped": 0}

    # control: the SAME session id filed under claude finds nothing — proving the
    # hit above came from the codex root, not from a glob that matches anything
    index_db.log_serving(conn, ts="2026-07-27T14:41:00+00:00", harness="claude",
                         session_id=sid, mode="inject", query="q", card_ids=[c.id])
    assert consolidate.mine_servings(conn, adapters.session_text_lookup)["skipped"] == 1
