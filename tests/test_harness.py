"""Harness discovery: where each harness lives, and which models it offers."""

import json
from pathlib import Path

from afterwit import cards as cards_mod
from afterwit import harness


def _claude(home: Path, settings: dict, global_cfg: dict | None = None) -> None:
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    if global_cfg is not None:
        (home / ".claude.json").write_text(json.dumps(global_cfg), encoding="utf-8")


def _codex(home: Path, toml: str) -> None:
    (home / ".codex").mkdir(parents=True, exist_ok=True)
    (home / ".codex" / "config.toml").write_text(toml, encoding="utf-8")


def test_defaults_are_home_relative(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert harness.config_dir("claude") == tmp_path / ".claude"
    assert harness.config_dir("codex") == tmp_path / ".codex"
    assert harness.sessions_dir("claude") == tmp_path / ".claude" / "projects"
    assert harness.sessions_dir("codex") == tmp_path / ".codex" / "sessions"
    assert harness.claude_json_path() == tmp_path / ".claude.json"


def test_env_overrides_win(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "cdx"))
    assert harness.config_dir("claude") == tmp_path / "elsewhere"
    assert harness.config_dir("codex") == tmp_path / "cdx"
    # a relocated Claude keeps .claude.json INSIDE the config dir
    assert harness.claude_json_path() == tmp_path / "elsewhere" / ".claude.json"


def test_claude_models_come_from_its_own_config(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    _claude(tmp_path, {"model": "opus[1m]", "effortLevel": "max"},
            {"additionalModelOptionsCache": [{"value": "claude-fable-5[1m]", "label": "Fable"}]})
    models = harness.models("claude")
    assert models[0] == "opus[1m]"            # what it is set to, first
    assert "claude-fable-5[1m]" in models     # what the harness cached as available
    assert "sonnet" in models                 # alias hints still offered
    assert harness.default_model("claude") == "opus[1m]"
    assert harness.efforts("claude")[0] == "max"


def test_codex_models_come_from_its_own_config(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    _codex(tmp_path, 'model = "gpt-5.6-sol"\n'
                     'model_reasoning_effort = "high"\n'
                     '[profiles.cheap]\nmodel = "gpt-5.6-mini"\n'
                     '[tui.model_availability_nux]\n"gpt-5.5" = 4\n'
                     '[notice.model_migrations]\n"gpt-5.3-codex" = "gpt-5.4"\n')
    models = harness.models("codex")
    assert models[0] == "gpt-5.6-sol"
    for expected in ("gpt-5.6-mini", "gpt-5.5", "gpt-5.4"):
        assert expected in models
    assert harness.efforts("codex")[0] == "high"
    assert harness.default_model("codex") == "gpt-5.6-sol"


def test_missing_or_corrupt_config_degrades(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert harness.default_model("codex") is None
    assert harness.models("claude") == list(harness._CLAUDE_ALIASES)
    _codex(tmp_path, "model = 'unterminated\n[[[")
    assert harness.settings("codex") == {}      # never raises on a half-written file
    assert harness.models("codex") == []
    assert harness.info("codex")["present"] is True


def test_distiller_falls_back_to_the_codex_config_model(tmp_path, monkeypatch):
    """`codex_p` with no configured model must inherit ~/.codex/config.toml, not
    the hardcoded constant — that constant is stale the day a model ships."""
    from afterwit import distill

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    _codex(tmp_path, 'model = "gpt-5.6-sol"\n')
    seen = {}

    class _Done:
        returncode = 0
        stdout = ""   # `codex exec --json` streams events here; empty is a valid run
        stderr = ""

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        Path(cmd[cmd.index("-o") + 1]).write_text("[]", encoding="utf-8")
        return _Done()

    monkeypatch.setattr(distill, "driver_executable", lambda name: "/bin/" + name)
    monkeypatch.setattr(distill.subprocess, "run", fake_run)
    distill.codex_p("prompt")
    assert seen["cmd"][seen["cmd"].index("-m") + 1] == "gpt-5.6-sol"


def test_claude_driver_passes_effort_when_configured(tmp_path, monkeypatch):
    from afterwit import distill

    class _Done:
        returncode = 0
        stdout = "[]"
        stderr = ""

    seen = {}
    monkeypatch.setattr(distill, "driver_executable", lambda name: "/bin/" + name)
    monkeypatch.setattr(distill.subprocess, "run", lambda cmd, **kw: (seen.update(cmd=cmd), _Done())[1])
    distill.make_driver("claude-p", model="opus", effort="high")("prompt")
    assert seen["cmd"][seen["cmd"].index("--effort") + 1] == "high"
    seen.clear()
    distill.make_driver("claude-p")("prompt")
    assert "--effort" not in seen["cmd"] and "--model" not in seen["cmd"]


def test_attribution_resolves_the_model_it_will_actually_run(tmp_path, monkeypatch):
    """`driver:model:effort`, resolved. Recording the driver name (what
    `reviewed_by` did) or a literal None answers neither "which model wrote this"
    nor "at what effort" — the two questions provenance exists for."""
    from afterwit import distill

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    _codex(tmp_path, 'model = "gpt-5.6-sol"\n')
    _claude(tmp_path, {"model": "opus[1m]"})
    assert distill.attribution("codex") == "codex:gpt-5.6-sol"
    assert distill.attribution("claude-p", None, "high") == "claude-p:opus[1m]:high"
    assert distill.attribution("codex", "gpt-6", "xhigh") == "codex:gpt-6:xhigh"
    # nothing configured anywhere: say so, never invent a model id
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "empty"))
    assert distill.attribution("codex") == "codex:default"


def test_distilled_cards_record_who_extracted_them(tmp_path, monkeypatch):
    from afterwit import config, distill, index_db

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    _codex(tmp_path, 'model = "gpt-5.6-sol"\n')
    cfg = config.Config(wiki_root=tmp_path / "wiki", db_path=tmp_path / "i.db",
                        projects_root=tmp_path / "P")
    cfg.wiki_root.mkdir(parents=True)
    conn = index_db.connect(cfg.db_path)

    class Ev:
        source_path = str(tmp_path / "s.jsonl")
        lines = (1, 1)
        project = "demo"
        ts = "2026-07-23T00:00:00Z"
        role = "assistant"
        kind = "assistant"
        text = "the fix was to pin the port"
        meta = {"harness": "codex", "model": "gpt-5.6-sol", "effort": "high"}

    card = [{"type": "gotcha", "title": "Pin the port", "body": "Pin it.",
             "source_lines": "L1", "confidence": 0.9}]
    driver = distill.make_driver("codex", effort="high")
    # make_driver binds the *_resolved primitive, which returns (text, model)
    monkeypatch.setattr(distill, "codex_p_resolved",
                        lambda *a, **k: (json.dumps(card), None))
    stats = distill.distill_sessions([[Ev()]], cfg, conn, driver=driver)
    assert stats["queue"] == 1  # every novel distilled claim crosses the review gate

    from afterwit import ui

    rowid = conn.execute("SELECT rowid FROM review_queue").fetchone()[0]
    ui._approve(cfg, conn, rowid, None)
    saved = [c for _, c in cards_mod.iter_cards(cfg.wiki_root)]
    assert len(saved) == 1
    # survives queue -> approve -> frontmatter -> reload
    assert saved[0].distilled_by == "codex:gpt-5.6-sol:high"
    assert saved[0].reviewed_by == "human"
    # ...and the session's own model/effort/harness ride along in the provenance
    assert saved[0].sources[0]["harness"] == "codex"
    assert saved[0].sources[0]["model"] == "gpt-5.6-sol"
    assert saved[0].sources[0]["effort"] == "high"
    conn.close()
