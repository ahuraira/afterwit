"""Tests for distill.py. Events are duck-typed (SPEC §6) — we build local
Event objects rather than importing afterwit.events (built by agent-adapters)."""
import json
from dataclasses import dataclass, field
from pathlib import Path

from afterwit import cards, cli, config, distill, index_db


@dataclass
class Event:
    text: str
    role: str = "assistant"
    kind: str = "assistant"
    source_path: str = "~/.claude/projects/p/sess.jsonl"
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
    """Fake driver returning queued responses; tracks call count."""
    calls = {"n": 0}
    it = iter(responses)

    def run(prompt):
        calls["n"] += 1
        return next(it)
    run.calls = calls  # type: ignore[attr-defined]
    return run


def _card_json(**over):
    d = {"type": "error_fix", "title": "P1001 fix use 127.0.0.1",
         "body": "On WSL2 use 127.0.0.1 in DATABASE_URL.", "why": "",
         "tags": ["prisma"], "files": [".env"], "source_lines": "L1-L2",
         "confidence": 0.9}
    d.update(over)
    return d


def _session():
    return [Event(text="prisma P1001 cannot reach db", role="user", kind="user"),
            Event(text="set host to 127.0.0.1; migrate succeeded", role="assistant")]


def _active(cfg):
    return [c for _, c in cards.iter_cards(cfg.wiki_root)]


def test_happy_path_writes_card_with_origin_provenance(tmp_path):
    cfg = _cfg(tmp_path)
    conn = _conn(cfg)
    driver = _driver(json.dumps([_card_json()]))
    stats = distill.distill_sessions([_session()], cfg, conn, driver=driver)
    assert stats == {"sessions": 1, "skipped": 0, "write": 0, "merge": 0,
                     "supersede": 0, "queue": 1, "truncated": 0}
    data = json.loads(conn.execute("SELECT card_json FROM review_queue").fetchone()[0])
    c = cards.Card(**data)
    assert c.type == "error_fix" and c.verified is False
    # ADR-010: sources carry harness/model/kind mapped from the cited event range
    assert any(s.get("harness") == "claude" and s.get("model") == "claude-opus-4-8"
               for s in c.sources)
    assert {s.get("kind") for s in c.sources} == {"user", "assistant"}


def test_malformed_json_then_successful_retry(tmp_path):
    cfg = _cfg(tmp_path)
    conn = _conn(cfg)
    driver = _driver("sorry, here you go:", json.dumps([_card_json()]))
    stats = distill.distill_sessions([_session()], cfg, conn, driver=driver)
    assert driver.calls["n"] == 2  # retried once
    assert stats["queue"] == 1 and stats["skipped"] == 0


def test_retry_fails_skips_session_no_crash(tmp_path):
    cfg = _cfg(tmp_path)
    conn = _conn(cfg)
    driver = _driver("not json", "still not json")
    stats = distill.distill_sessions([_session()], cfg, conn, driver=driver)
    assert stats["skipped"] == 1 and stats["sessions"] == 0 and stats["write"] == 0
    assert _active(cfg) == []
    assert "distill-skip" in config.log_path(cfg.wiki_root).read_text()


def test_budget_caps_sessions(tmp_path):
    cfg = _cfg(tmp_path)
    conn = _conn(cfg)
    driver = _driver("[]", "[]", "[]")
    stats = distill.distill_sessions([_session(), _session(), _session()], cfg, conn,
                                     driver=driver, budget=1)
    assert stats["sessions"] == 1 and driver.calls["n"] == 1


def test_confidence_high_writes_low_queues(tmp_path):
    cfg = _cfg(tmp_path)
    conn = _conn(cfg)
    driver = _driver(json.dumps([_card_json(confidence=0.9),
                                 _card_json(title="weak hunch", confidence=0.6)]))
    stats = distill.distill_sessions([_session()], cfg, conn, driver=driver)
    assert stats["write"] == 0 and stats["queue"] == 2


def test_from_agent_caps_confidence_to_queue(tmp_path):
    # cap machinery: from_agent forces AGENT_CONFIDENCE_CAP -> always queues
    cfg = _cfg(tmp_path)
    conn = _conn(cfg)
    driver = _driver(json.dumps([_card_json(confidence=0.99)]))
    stats = distill.distill_sessions([_session()], cfg, conn, driver=driver, from_agent=True)
    assert stats["write"] == 0 and stats["queue"] == 1


def test_confidence_clamped_to_unit_range(tmp_path):
    cfg = _cfg(tmp_path)
    conn = _conn(cfg)
    driver = _driver(json.dumps([_card_json(confidence=1.5)]))
    distill.distill_sessions([_session()], cfg, conn, driver=driver)
    data = json.loads(conn.execute("SELECT card_json FROM review_queue").fetchone()[0])
    assert data["confidence"] == 1.0


def test_compaction_ordered_first():
    normal = Event(text="regular turn", kind="assistant")
    summary = Event(text="pre-distilled decisions", kind="compaction_summary")
    ordered = distill._order([normal, summary])
    assert ordered[0] is summary
    rendered = distill._render(ordered)
    assert rendered.splitlines()[0].startswith("L1 [summary]")


def test_make_driver_binds_model_and_effort(monkeypatch, tmp_path):
    calls = {}
    # An empty home, so "no model configured" really means none: the fallback now
    # reads the user's OWN ~/.codex/config.toml, and the developer running these
    # tests has one.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    def fake_run(cmd, **kw):
        calls["cmd"] = cmd

        class R:
            returncode = 0
            stdout = "[]"
            stderr = ""
        return R()

    monkeypatch.setattr(distill.subprocess, "run", fake_run)
    # Resolve the binary too: asserting on the argv never needed a real `claude`
    # on PATH, and requiring one made this test pass only on the author's machine.
    monkeypatch.setattr(distill, "driver_executable", lambda name: f"/usr/bin/{name}")
    distill.make_driver("claude-p", model="opus")("hi")
    assert Path(calls["cmd"][0]).name == "claude"
    # --output-format json is not cosmetic: it is how the run learns that `opus`
    # resolved to claude-opus-5, which is what `distilled_by` then records.
    assert calls["cmd"][1:] == ["-p", "--output-format", "json", "--model", "opus"]

    distill.make_driver("codex", model="gpt-5.5", effort="high")("hi")
    cmd = calls["cmd"]
    assert Path(cmd[0]).name == "codex" and cmd[1] == "exec"
    assert ["-m", "gpt-5.5"] == cmd[cmd.index("-m"):cmd.index("-m") + 2]
    assert 'model_reasoning_effort="high"' in cmd

    # defaults: no flags at all for claude-p (the CLI then uses its own settings.json),
    # and for codex the constant — reached only because this home has no codex config.
    # Assert the constant, not a literal — a model id is a value that moves, and a test
    # that hardcodes it just has to be edited every time it does.
    distill.make_driver("claude-p")("hi")
    # No --model/--effort: the CLI falls back to its own settings.json. The
    # json output format is always present — it is how the model is resolved.
    assert Path(calls["cmd"][0]).name == "claude"
    assert calls["cmd"][1:] == ["-p", "--output-format", "json"]
    distill.make_driver("codex")("hi")
    assert distill.DEFAULT_CODEX_MODEL in calls["cmd"] and "-c" not in calls["cmd"]

    # ...and with a codex config present, THAT model wins over the constant.
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text('model = "gpt-5.6-sol"\n', encoding="utf-8")
    distill.make_driver("codex")("hi")
    assert "gpt-5.6-sol" in calls["cmd"] and distill.DEFAULT_CODEX_MODEL not in calls["cmd"]


def test_cli_distill_does_not_shadow_the_configured_driver(monkeypatch):
    """Gotcha #22, a second time. `afterwit distill` declared --driver with default
    "claude-p" and forwarded it unconditionally, so `distill_driver = "codex"` in
    config.toml was dead: every manual distill silently ran the wrong model, on the
    wrong quota pool, at the wrong price. A flag that shadows a config key must default
    to None and only be forwarded when the user actually passed it.
    """
    seen: dict = {}
    monkeypatch.setattr(distill, "main", lambda argv: seen.setdefault("argv", argv) or 0)

    cli.main(["distill", "--source", "claude"])
    assert "--driver" not in seen["argv"], (
        f"config's distill_driver is shadowed by a flag default: {seen['argv']}"
    )

    seen.clear()
    cli.main(["distill", "--source", "claude", "--driver", "codex", "--effort", "high"])
    assert seen["argv"][seen["argv"].index("--driver") + 1] == "codex"   # explicit still wins
    assert seen["argv"][seen["argv"].index("--effort") + 1] == "high"


def test_codex_driver_sends_configured_model_and_effort(monkeypatch):
    """The model the user pays for must be the model that runs. Pin the argv."""
    calls: dict = {}

    class R:
        returncode, stdout, stderr = 0, "", ""

    def fake_run(cmd, **kw):
        calls["cmd"] = cmd
        return R()

    monkeypatch.setattr(distill.subprocess, "run", fake_run)
    monkeypatch.setattr(distill, "driver_executable", lambda n: "/usr/bin/" + n)
    distill.make_driver("codex", model="gpt-5.6-terra", effort="high")("prompt")

    cmd = calls["cmd"]
    assert cmd[cmd.index("-m") + 1] == "gpt-5.6-terra"
    assert 'model_reasoning_effort="high"' in cmd


def test_llm_children_are_marked_internal_so_the_users_hooks_skip_them(monkeypatch, tmp_path):
    """Both drivers must spawn with AFTERWIT_INTERNAL=1 — that marker is the only
    thing stopping `afterwit inject` (wired into the user's UserPromptSubmit hook,
    which a `claude -p` child inherits) from injecting afterwit into afterwit.

    The PATH assertion is the real trap: `env={"AFTERWIT_INTERNAL": "1"}` sets the
    marker and passes the first half of this test while stripping PATH/HOME, so
    the driver never spawns at all. Merge, don't replace."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin")
    calls = {}

    def fake_run(cmd, **kw):
        calls["env"] = kw.get("env")

        class R:
            returncode = 0
            stdout = "[]"
            stderr = ""
        return R()

    monkeypatch.setattr(distill.subprocess, "run", fake_run)
    monkeypatch.setattr(distill, "driver_executable", lambda name: f"/usr/bin/{name}")

    for driver in ("claude-p", "codex"):
        calls.clear()
        distill.make_driver(driver)("hi")
        env = calls["env"]
        assert env is not None, f"{driver} spawns with the user's raw environment"
        assert env["AFTERWIT_INTERNAL"] == "1", driver
        assert env["PATH"] == "/usr/bin", f"{driver} replaced the environment instead of merging"


def test_drivers_pipe_the_prompt_as_utf8(monkeypatch):
    """`text=True` alone means cp1252 on Windows, and prompts/distill.md carries `≤`.

    Every session died with `'charmap' codec can't encode character '\u2264'`, the
    run ended `distillation attempted but every session failed`, and afterwit
    distilled nothing on Windows for as long as it ran there — while doctor
    reported all good. Asserting the kwarg is what makes that unrepeatable on a
    Linux CI box, where the locale hides it (Gotcha #75).
    """
    seen = {}

    class _R:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def fake_run(cmd, **kw):
        seen.update(kw)
        return _R()

    monkeypatch.setattr(distill.subprocess, "run", fake_run)
    monkeypatch.setattr(distill, "driver_executable", lambda name: name)
    distill.claude_p("body with \u2264 in it")
    assert seen.get("encoding") == "utf-8"

    seen.clear()
    monkeypatch.setattr(distill.Path, "read_text", lambda self, **kw: "{}")
    distill.codex_p("body with \u2264 in it", model="m")
    assert seen.get("encoding") == "utf-8"


def test_stamp_records_the_model_that_ran_not_the_alias(monkeypatch, tmp_path):
    """`--model opus` is an ALIAS. Stamping it recorded `claude-p:opus`, which is
    true today and ambiguous the day opus-6 ships — while `distilled_by` claims to
    answer "which model wrote this card" (ADR-035) and separation-of-duties is
    checked against it (ADR-021). The JSON reply names the concrete id."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(distill, "driver_executable", lambda name: f"/usr/bin/{name}")

    class R:
        returncode = 0
        stdout = json.dumps({"result": "[]", "modelUsage": {"claude-opus-5": {}}})
        stderr = ""

    monkeypatch.setattr(distill.subprocess, "run", lambda cmd, **kw: R())
    driver = distill.make_driver("claude-p", model="opus", effort="high")
    assert driver.label == "claude-p:opus:high"      # configured, before any call
    driver("hi")
    assert driver.label == "claude-p:claude-opus-5:high"   # resolved, after it


def test_driver_keeps_the_configured_label_when_it_cannot_resolve(monkeypatch, tmp_path):
    """An older CLI, or any reply shape we do not recognise, must degrade to the
    configured name — never break the run over metadata (Gotcha #75)."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(distill, "driver_executable", lambda name: f"/usr/bin/{name}")

    class R:
        returncode = 0
        stdout = "[]"      # a bare card array: not the JSON envelope
        stderr = ""

    monkeypatch.setattr(distill.subprocess, "run", lambda cmd, **kw: R())
    driver = distill.make_driver("claude-p", model="opus")
    assert driver("hi") == "[]"        # the text still comes through
    assert driver.label == "claude-p:opus"


def _ev(text):
    return Event(text=text, role="user", kind="user")


def test_coverage_reports_a_fully_read_session_as_full():
    _, cov = distill._render_cov([_ev("short turn"), _ev("another")])
    assert cov.elided is False and cov.events_truncated == 0
    assert cov.pct == 100 and cov.describe() == "full"


def test_coverage_reports_how_much_of_a_huge_session_was_read():
    """A truncated session and a fully-read one leave identical traces afterwards
    -- one ledger row and some cards -- so a card can rest on a fraction of the
    evidence with nothing recording that. Measure it."""
    events = [_ev("x" * 5000) for _ in range(200)]     # 1M chars, way over both caps
    rendered, cov = distill._render_cov(events)
    assert cov.elided is True
    assert cov.events_truncated == 200                  # every turn hit MAX_EVENT_CHARS
    assert len(rendered) <= distill.MAX_TRANSCRIPT_CHARS + 200
    assert 0 < cov.pct < 100
    assert "middle elided" in cov.describe() and "turns clipped" in cov.describe()
