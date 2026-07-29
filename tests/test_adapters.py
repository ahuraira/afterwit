import json
import logging
import sqlite3
from pathlib import Path

from afterwit import cards, config, index_db
from afterwit.adapters import _materialize, guarded_events, ingest, write_checkpoint
from afterwit.adapters.db_schema import _sqlite_events
from afterwit.adapters.claude_jsonl import iter_events as iter_claude
from afterwit.adapters.codex_jsonl import iter_events as iter_codex
from afterwit.adapters.docs_md import iter_events as iter_docs
from afterwit.adapters.memory_md import iter_events as iter_memory
from afterwit.redact import redact


FIXTURES = Path(__file__).parent / "fixtures"


def test_claude_fixture_drops_bloat_excludes_synthetic_and_keeps_thinking(caplog):
    caplog.set_level(logging.WARNING)
    events = list(iter_claude(FIXTURES / "claude_sample.jsonl"))
    texts = "\n".join(e.text for e in events)
    assert "<local-command-caveat>" not in texts
    assert "a" * 520 not in texts
    assert any(e.kind == "thinking" for e in events)
    assert any(e.meta.get("compaction") for e in events)
    assert "[REDACTED:generic_secret]" in texts
    assert any("unknown record type" in rec.message for rec in caplog.records)
    assert all(e.meta["harness"] == "claude" and e.meta["kind"] == e.kind for e in events)


def test_codex_fixture_keeps_reasoning_compaction_and_logs_unknown(caplog, monkeypatch):
    caplog.set_level(logging.WARNING)
    # The project slug is derived by checking the record's cwd against
    # cfg.projects_root. This assertion used to pass only because the fixture's cwd
    # hardcoded ONE developer's home directory, which happened to match the default
    # ~/Desktop/Projects. On any other machine — a contributor's, or CI's — the slug
    # fell back to "global" and this went red. Pin the root to the fixture instead of
    # depending on whose laptop is running.
    cfg = config.load()
    cfg.projects_root = Path("/home/user/Desktop/Projects")   # the fixture's root
    monkeypatch.setattr(config, "load", lambda *a, **k: cfg)
    events = list(iter_codex(FIXTURES / "codex_sample.jsonl"))
    texts = "\n".join(e.text for e in events)
    assert any(e.kind == "thinking" for e in events)
    assert any(e.meta.get("compaction") for e in events)
    assert "[REDACTED:bearer_token]" in texts
    assert "[REDACTED:generic_secret]" in texts
    assert any("unknown record type" in rec.message for rec in caplog.records)
    assert {e.project for e in events} == {"acme_flow"}


def test_known_plumbing_is_silent_but_a_NEW_type_still_raises_the_alarm(tmp_path, caplog):
    """Codex shipped `world_state` and `inter_agent_communication_metadata` on 2026-07-09.
    The adapter warned on every session that had them — 31% of the corpus and 100% of
    recent ones — burying the one signal that log exists to carry.

    They are dropped because they were READ and hold no knowledge (world_state is an
    AGENTS.md snapshot the docs adapter already ingests; the other is `{trigger_turn}`).
    The alarm itself must survive: the next unannounced record type has to be as loud as
    these were, or schema drift lands silently and we quietly stop learning from Codex.
    """
    caplog.set_level(logging.WARNING)
    src = tmp_path / "s.jsonl"
    src.write_text(
        '{"type":"world_state","payload":{"full":true,"state":{}}}\n'
        '{"type":"inter_agent_communication_metadata","payload":{"trigger_turn":true}}\n'
        '{"type":"event_msg","payload":{"type":"user_message","message":"real content"}}\n',
        encoding="utf-8",
    )
    events = list(iter_codex(src))
    assert [e.text for e in events] == ["real content"]          # plumbing yields nothing
    assert not [r for r in caplog.records if "unknown record type" in r.message]

    src.write_text('{"type":"quantum_telemetry_v9","payload":{}}\n', encoding="utf-8")
    assert list(iter_codex(src)) == []
    assert any("quantum_telemetry_v9" in r.message for r in caplog.records), (
        "a genuinely unknown type must still warn — that alarm is the drift detector"
    )


def test_workflow_journal_is_skipped_but_subagent_transcripts_are_not(tmp_path, monkeypatch):
    """`journal.jsonl` is the workflow runner's ledger (`started`/`result`), not a
    transcript — parsing it warned "unknown record type" on every workflow session.

    The lazy fix (glob only <slug>/<uuid>.jsonl) would ALSO have thrown away 554
    subagents/**/agent-*.jsonl files, which are real transcripts full of real work. So
    pin both directions: the ledger is skipped, the subagent transcripts are kept.
    """
    from afterwit.adapters import _iter_paths

    proj = tmp_path / ".claude" / "projects" / "demo"
    wf = proj / "1a2b" / "subagents" / "workflows" / "wf_x"
    wf.mkdir(parents=True)
    (proj / "session.jsonl").write_text("{}\n", encoding="utf-8")
    (wf / "agent-7.jsonl").write_text("{}\n", encoding="utf-8")
    (wf / "journal.jsonl").write_text('{"type":"result","key":"v2:abc"}\n', encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    found = {p.name for p in _iter_paths("claude", tmp_path)}
    assert "journal.jsonl" not in found          # the ledger: wrong schema, never parse it
    assert {"session.jsonl", "agent-7.jsonl"} <= found   # both are genuine transcripts


def test_memory_and_docs_emit_doc_events():
    mem = list(iter_memory(FIXTURES / "memory_sample.md"))
    docs = list(iter_docs(FIXTURES / "doc_sample.md"))
    assert mem[0].kind == "doc"
    assert mem[0].meta["card_type"] == "fact"
    assert [e.meta["heading"] for e in docs] == ["Fixture Doc", "Decision"]


def test_memory_links_use_sibling_card_title(tmp_path):
    (tmp_path / "target.md").write_text(
        "---\nname: Human Target\ntype: project\n---\nTarget body.\n", encoding="utf-8"
    )
    source = tmp_path / "source.md"
    source.write_text(
        "---\nname: Source\ntype: project\n---\nSee [[target]] and `[[literal]]`.\n",
        encoding="utf-8",
    )

    event = next(iter_memory(source))
    assert "[[Human Target]]" in event.text
    assert "`[[literal]]`" in event.text


def test_redact_patterns():
    text = (
        "AWS AKIAABCDEFGHIJKLMNOP and Bearer abcdefghijklmnopqrstuvwxyz "
        "password=hunter2 postgres://user:secret@example.com"
    )
    out = redact(text)
    assert "[REDACTED:aws_key]" in out
    assert "[REDACTED:bearer_token]" in out
    assert "[REDACTED:generic_secret]" in out
    assert "[REDACTED:url_password]" in out


def test_checkpoint_noop_on_rerun(tmp_path):
    db = index_db.connect(tmp_path / "index.db")
    fixture = FIXTURES / "claude_sample.jsonl"
    first, skipped_first = guarded_events(db, fixture, iter_claude)
    second, skipped_second = guarded_events(db, fixture, iter_claude)
    assert first
    assert not skipped_first
    assert second == []
    assert skipped_second


def test_docs_materialize_one_pointer_card_per_document(tmp_path, monkeypatch):
    cfg = config.Config(wiki_root=tmp_path / "wiki", db_path=tmp_path / "index.db",
                        projects_root=tmp_path)
    monkeypatch.setattr(config, "load", lambda *a, **k: cfg)
    conn = index_db.connect(cfg.db_path)
    events = list(iter_docs(FIXTURES / "doc_sample.md"))
    assert _materialize(cfg, conn, "docs", FIXTURES / "doc_sample.md", events) == 1
    row = conn.execute("SELECT type, verified FROM cards").fetchone()
    assert row["type"] == "doc_ref" and row["verified"] == 1


def test_docs_materialize_only_named_decisions_and_prune_old_subsections(
    tmp_path, monkeypatch
):
    cfg = config.Config(wiki_root=tmp_path / "wiki", db_path=tmp_path / "index.db",
                        projects_root=tmp_path)
    monkeypatch.setattr(config, "load", lambda *a, **k: cfg)
    source = tmp_path / "demo" / "docs" / "ADR.md"
    source.parent.mkdir(parents=True)
    source.write_text(
        "# Architecture Decisions\n\n## How to use\nIgnore.\n\n"
        "## ADR-001: Keep the wiki canonical\n\n### Context\nOld context.\n\n"
        "### Decision\nUse markdown.\n\n## DD-002 — Design decision\nShip it.\n\n"
        "## Gotchas Reference\n\n### 1. Not a decision\nNo.\n",
        encoding="utf-8",
    )
    conn = index_db.connect(cfg.db_path)
    obsolete = cards.Card(
        id="OLD-CONTEXT", type="decision", title="Context", project="demo",
        status="active", body="Old context.",
        sources=[{"path": str(source), "lines": "L7-L8"}], tags=["docs"],
        verified=True, reviewed_by="deterministic-import",
    )
    old_path = cards.save(obsolete, cfg.wiki_root)
    index_db.upsert_card(conn, obsolete, str(old_path))

    events = list(iter_docs(source))
    assert _materialize(cfg, conn, "docs", source, events) == 2
    rows = conn.execute("SELECT title, type FROM cards ORDER BY title").fetchall()
    assert [(row["title"], row["type"]) for row in rows] == [
        ("ADR-001: Keep the wiki canonical", "decision"),
        ("DD-002 — Design decision", "decision"),
    ]
    saved = {card.title: card for _, card in cards.iter_cards(cfg.wiki_root)}
    assert saved["ADR-001: Keep the wiki canonical"].sources[0]["heading"] == (
        "ADR-001: Keep the wiki canonical"
    )
    assert not old_path.exists()


def test_docs_ingest_upgrades_checkpointed_decision_materialization(tmp_path, monkeypatch):
    cfg = config.Config(wiki_root=tmp_path / "wiki", db_path=tmp_path / "index.db",
                        projects_root=tmp_path)
    monkeypatch.setattr(config, "load", lambda *a, **k: cfg)
    source = tmp_path / "demo" / "docs" / "ADR.md"
    source.parent.mkdir(parents=True)
    source.write_text("# ADRs\n\n## ADR-001: One entry\n\n### Context\nNested.\n",
                      encoding="utf-8")
    conn = index_db.connect(cfg.db_path)
    conn.execute("CREATE TABLE materialized_sources(source TEXT PRIMARY KEY, imported TEXT)")
    conn.execute("INSERT INTO materialized_sources VALUES(?, datetime('now'))", (str(source),))
    write_checkpoint(conn, source)
    conn.close()

    rows = ingest("docs")
    assert rows == [(source, 3, False)]
    conn = index_db.connect(cfg.db_path)
    assert conn.execute("SELECT title FROM cards").fetchone()["title"] == "ADR-001: One entry"
    assert conn.execute(
        "SELECT imported FROM materialized_sources WHERE source=?", (str(source),)
    ).fetchone()["imported"] == "decision-headings-v2"


def test_sqlite_schema_adapter_reads_metadata_not_rows(tmp_path):
    db = tmp_path / "app.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE users(id INTEGER PRIMARY KEY, email TEXT NOT NULL)")
    conn.execute("INSERT INTO users(email) VALUES('private@example.com')")
    conn.commit()
    conn.close()
    events = _sqlite_events("demo", str(db))
    assert len(events) == 1 and "email TEXT NOT NULL" in events[0].text
    assert "private@example.com" not in events[0].text


def test_codex_model_and_effort_come_from_turn_context(tmp_path, monkeypatch):
    """Codex puts the model in `turn_context`, not `session_meta` — the latter
    carries only `model_provider`. Reading session_meta alone left every codex
    card with `model: null` (audit 2026-07-23: 79 codex cards, 0 models) while
    claude cards had one, so provenance silently depended on the harness."""
    src = tmp_path / "rollout-x.jsonl"
    src.write_text("\n".join([
        json.dumps({"type": "session_meta", "payload": {
            "cwd": "/home/user/Desktop/Projects/demo", "model_provider": "openai"}}),
        json.dumps({"type": "turn_context", "payload": {
            "model": "gpt-5.6-sol", "effort": "high"}}),
        json.dumps({"type": "event_msg", "payload": {
            "type": "user_message", "message": "does the model land on the card?"}}),
        # a later turn may switch model/effort; the newest wins
        json.dumps({"type": "turn_context", "payload": {
            "model": "gpt-5.6-terra", "effort": "low"}}),
        json.dumps({"type": "event_msg", "payload": {
            "type": "agent_message", "message": "yes"}}),
    ]) + "\n", encoding="utf-8")
    events = list(iter_codex(src))
    assert [(e.meta["model"], e.meta["effort"]) for e in events] == [
        ("gpt-5.6-sol", "high"), ("gpt-5.6-terra", "low")]
    assert all(e.meta["harness"] == "codex" for e in events)


def test_claude_effort_is_captured_from_the_record(tmp_path):
    """Claude Code stamps `effort` on the record, beside `message`, not inside it."""
    src = tmp_path / "s.jsonl"
    src.write_text(json.dumps({
        "type": "assistant", "timestamp": "2026-07-23T10:00:00Z", "cwd": "/tmp",
        "effort": "max",
        "message": {"model": "claude-opus-4-8",
                    "content": [{"type": "text", "text": "an answer worth keeping"}]},
    }) + "\n", encoding="utf-8")
    e = list(iter_claude(src))[0]
    assert (e.meta["harness"], e.meta["model"], e.meta["effort"]) == (
        "claude", "claude-opus-4-8", "max")


def test_adapters_stamp_the_aliased_slug_not_the_folder_name(tmp_path, monkeypatch):
    """The guard that makes a project rename STICK (ADR-039).

    Events carry the project that ends up on every distilled card. If the adapters
    resolved the folder name instead of the slug, the first nightly after a rename
    would mint a fresh batch of cards under the old name and the rename would quietly
    undo itself — with both slugs then live at once.

    Covers the cwd-less path too: Claude Code omits `cwd` on some records, and that
    fallback recovers the slug from the flattened transcript directory name. If only
    one of the two paths were aliased, a single session would split across two projects.
    """
    from tests.conftest import toml_config
    root = tmp_path / "Projects"
    (root / "harness_helper").mkdir(parents=True)
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(toml_config(wiki_root=tmp_path / "w", db_path=tmp_path / "i.db",
                                    projects_root=root)
                        + '\n[project_aliases]\nharness_helper = "afterwit"\n')
    monkeypatch.setenv("AFTERWIT_CONFIG", str(cfg_file))

    rec = {"type": "assistant", "timestamp": "2026-07-27T10:00:00Z",
           "cwd": str(root / "harness_helper"),
           "message": {"model": "claude-opus-5",
                       "content": [{"type": "text", "text": "an answer worth keeping"}]}}
    src = tmp_path / "s.jsonl"
    src.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    assert list(iter_claude(src))[0].project == "afterwit"

    # codex resolves the same way, from its own state
    csrc = tmp_path / "c.jsonl"
    csrc.write_text("\n".join(json.dumps(r) for r in (
        {"type": "turn_context", "payload": {"cwd": str(root / "harness_helper")}},
        {"type": "event_msg", "timestamp": "2026-07-27T10:00:00Z",
         "payload": {"type": "agent_message", "message": "an answer worth keeping"}},
    )) + "\n", encoding="utf-8")
    codex_projects = {e.project for e in iter_codex(csrc)}
    assert codex_projects == {"afterwit"}, codex_projects

    # and the cwd-less fallback, which reads the flattened transcript dir name
    flat = tmp_path / "-home-user-Desktop-Projects-harness-helper"
    flat.mkdir()
    nocwd = flat / "t.jsonl"
    del rec["cwd"]
    nocwd.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    assert list(iter_claude(nocwd))[0].project == "afterwit"


def test_a_flood_of_one_unknown_type_warns_once_and_the_undo_journal_is_dropped(
        tmp_path, caplog):
    """Two failures with one cause, both measured on 2026-07-29.

    `file-history-delta` is Claude Code's per-edit undo journal — the sibling of
    `file-history-snapshot`, which was already dropped — and it was reaching the
    unknown-type alarm. One 5k-line transcript produced 585 KB of warnings and
    buried the stdout of the command being run. Dropping the type is the root fix;
    `warn_once` is the bound that stops the NEXT unannounced type doing the same.

    The alarm must stay loud once per cause, so the last assertion is the control:
    a genuinely new type still warns, and a second file still gets its own warning.
    """
    from afterwit.adapters import _warned
    _warned.clear()
    caplog.set_level(logging.WARNING)
    src = tmp_path / "a.jsonl"
    src.write_text(
        ('{"type":"file-history-delta","patch":{"x":1}}\n' * 50)
        + ('{"type":"quantum_telemetry_v9","payload":{}}\n' * 50),
        encoding="utf-8")
    assert list(iter_claude(src)) == []                      # neither type is knowledge
    unknown = [r for r in caplog.records if "unknown record type" in r.message]
    assert len(unknown) == 1, f"one cause, one warning — got {len(unknown)}"
    assert "quantum_telemetry_v9" in unknown[0].message      # and not the undo journal

    other = tmp_path / "b.jsonl"
    other.write_text('{"type":"quantum_telemetry_v9","payload":{}}\n', encoding="utf-8")
    list(iter_claude(other))
    assert len([r for r in caplog.records if "unknown record type" in r.message]) == 2, (
        "dedupe is per (file, type) — a second file must still raise its own alarm")
