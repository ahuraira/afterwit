import json

from afterwit import config, index_db, mcp_server, postprocess
from tests.test_cards import make_card
from tests.test_inject import setup_env


def test_missing_index_is_never_reported_as_empty_knowledge(tmp_path):
    """The dangerous failure: connect(rw) does mkdir + executescript(SCHEMA), so a
    wrong db_path CREATES a blank db and every tool answers "no known history —
    proceed normally". The agent concludes the user has no knowledge and stops
    asking, while the real index sits elsewhere. A broken install must be loud."""
    cfg = config.Config(wiki_root=tmp_path / "wiki",
                        db_path=tmp_path / "nonexistent" / "index.db",
                        projects_root=tmp_path / "projects")
    out = mcp_server.dispatch("recall", {"query": "anything"}, cfg=cfg)
    assert "UNREACHABLE" in out and "doctor" in out
    assert mcp_server._EMPTY not in out            # never the "proceed normally" text
    assert not cfg.db_path.exists()                # and it must NOT have created one


def test_recall_returns_card_with_id(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch, [
        make_card(title="Prisma P1001 fix use 127.0.0.1", type="error_fix",
                  body="Fix: use 127.0.0.1", verified=True, updated="2026-07-05"),
    ])
    out = mcp_server.dispatch("recall", {"query": "prisma P1001 cannot reach database",
                                         "project": "acme_hr"})
    assert "P1001" in out and "id: " in out


def test_recall_type_filter(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch, [
        make_card(title="Audit stores as JSONB", type="decision", verified=True),
        make_card(title="Audit truncates silently", type="gotcha", verified=True,
                  body="Surprising truncation."),
    ])
    out = mcp_server.dispatch("recall", {"query": "audit", "type": "decision"})
    assert "JSONB" in out and "truncates" not in out


def test_lookup_error_biases_error_fix_first(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch, [
        make_card(title="Playwright timeout decision", type="decision", verified=True,
                  body="We raised the timeout."),
        make_card(title="Playwright timeout fix", type="error_fix", verified=True,
                  body="Fix: bump expect timeout to 30s."),
    ])
    # floor=0 so both similar cards clear it and the type bias is observable
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(cfg_file.read_text() + "floor = 0.0\n")
    out = mcp_server.dispatch("lookup_error", {"error_text": "playwright timeout"})
    assert out.index("error_fix") < out.index("decision")  # fix listed first


def test_why_includes_superseded_history(tmp_path, monkeypatch):
    new = make_card(title="Audit storage uses typed columns", type="decision",
                    verified=True, body="Typed columns now. **Why:** schema stabilized.")
    old = make_card(title="Audit storage uses JSONB", type="decision", status="superseded",
                    verified=True, body="JSONB. **Why:** churned weekly.")
    old.superseded_by = new.id
    setup_env(tmp_path, monkeypatch, [new, old])
    out = mcp_server.dispatch("why", {"topic": "audit storage"})
    assert "typed columns" in out and "superseded" in out and "JSONB" in out


def test_for_file(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch, [
        make_card(title="Audit store gotcha", type="gotcha", verified=True,
                  files=["src/audit/store.ts"], body="Watch the write path."),
    ])
    out = mcp_server.dispatch("for_file", {"path": "audit/store.ts"})
    assert "Audit store gotcha" in out


def test_project_brief_from_file(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch, [make_card(verified=True)])
    cfg = config.load()
    brief = cfg.wiki_root / "projects" / "acme_hr" / "brief.md"
    brief.parent.mkdir(parents=True, exist_ok=True)
    brief.write_text("MY PROJECT BRIEF")
    assert mcp_server.dispatch("project_brief", {"project": "acme_hr"}) == "MY PROJECT BRIEF"


def test_project_brief_synthesized(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch, [
        make_card(title="A decision", type="decision", verified=True),
        make_card(title="Truncation gotcha", type="gotcha", verified=True, body="x"),
    ])
    out = mcp_server.dispatch("project_brief", {"project": "acme_hr"})
    assert "acme_hr" in out and "recall" in out and "gotcha" in out.lower()


def test_related_edges(tmp_path, monkeypatch):
    linked = make_card(title="Audit refactor", verified=True, body="Plain.", files=[])
    linker = make_card(title="JSONB decision", verified=True, files=["src/audit/store.ts"],
                       body="See [[audit-refactor]].")
    filemate = make_card(title="Audit store gotcha", type="gotcha", verified=True,
                         files=["src/audit/store.ts"], body="Plain.")
    old = make_card(title="Old audit decision", status="superseded", verified=True,
                    body="Plain.", files=[])
    old.superseded_by = linker.id
    setup_env(tmp_path, monkeypatch, [linked, linker, filemate, old])
    out = mcp_server.dispatch("related", {"card_id": linker.id})
    assert "Audit refactor" in out and "[wikilink]" in out
    assert "Audit store gotcha" in out and "[file]" in out
    assert "Old audit decision" in out and "[supersede]" in out


def test_save_insight_queues_capped_and_not_trusted(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch, [])
    out = mcp_server.dispatch("save_insight", {
        "type": "gotcha", "title": "New gotcha", "project": "acme_hr",
        "body": "Turns out X.", "why": "because Y",
    })
    assert "review queue" in out
    conn = index_db.connect(config.load().db_path)
    rows = conn.execute("SELECT card_json FROM review_queue").fetchall()
    assert len(rows) == 1
    card = json.loads(rows[0]["card_json"])
    assert card["confidence"] <= postprocess.AGENT_CONFIDENCE_CAP
    assert card["verified"] is False
    assert "**Why:** because Y" in card["body"]
    # never written to the wiki / cards table
    assert conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 0
    conn.close()


def test_feedback_helpful_bumps_usefulness(tmp_path, monkeypatch):
    c = make_card(verified=True)
    setup_env(tmp_path, monkeypatch, [c])
    mcp_server.dispatch("feedback", {"card_id": c.id, "verdict": "helpful"})
    conn = index_db.connect(config.load().db_path, readonly=True)
    row = conn.execute("SELECT usefulness, last_used FROM cards WHERE id=?", (c.id,)).fetchone()
    assert row["usefulness"] == 1.0 and row["last_used"]
    audit = conn.execute("SELECT outcome FROM servings WHERE mode='feedback'").fetchone()
    assert audit["outcome"] == "feedback:helpful"
    conn.close()


def test_feedback_wrong_quarantines_db_and_wiki(tmp_path, monkeypatch):
    c = make_card(verified=True)
    setup_env(tmp_path, monkeypatch, [c])
    cfg = config.load()
    msg = mcp_server.dispatch("feedback", {"card_id": c.id, "verdict": "wrong"}, cfg=cfg)
    assert "quarantined" in msg.lower()
    conn = index_db.connect(cfg.db_path, readonly=True)
    row = conn.execute("SELECT status, usefulness FROM cards WHERE id=?", (c.id,)).fetchone()
    assert row["status"] == "quarantined" and row["usefulness"] == -1.0
    conn.close()
    # persisted to the wiki (survives rebuild) + logged for forensics (P4/P6)
    from afterwit import cards as cards_mod
    reloaded = cards_mod.load(cfg.wiki_root / c.relpath())
    assert reloaded.status == "quarantined"
    assert "feedback-quarantine" in config.log_path(cfg.wiki_root).read_text()


def test_feedback_unknown_card(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch, [])
    assert "Unknown card" in mcp_server.dispatch("feedback", {"card_id": "NOPE", "verdict": "helpful"})


def test_empty_result_is_clear_not_error(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch, [make_card(verified=True)])
    out = mcp_server.dispatch("recall", {"query": "kubernetes ingress haiku zzznomatch"})
    assert out == mcp_server._EMPTY and "proceed normally" in out


def test_tools_registered_verbatim():
    # the descriptions are load-bearing — the server must not mutate them
    server = mcp_server._build_server()
    assert server.name == "afterwit"
    from afterwit.toolspecs import TOOLS
    assert {t["name"] for t in TOOLS} == set(mcp_server._ROUTES)


def test_save_insight_survives_a_server_running_stale_code(tmp_path, monkeypatch):
    """The failure reported from a live session, reproduced exactly.

    afterwit edits its own source while its MCP servers keep running. A server
    that imported `afterwit.config` before a function existed holds that stale
    module in sys.modules for the life of the process, so the lazy
    `from .config import project_dir_name` inside gitmeta.anchor() raises
    `ImportError: cannot import name 'project_dir_name' from 'afterwit.config'`
    — naming a file that does have it. save_insight was the only tool affected,
    because it is the only one that anchors.

    An anchor is metadata. Losing a resolved insight to fetch it is the worse
    outcome, and the card is going to a human review queue regardless.
    """
    from afterwit import gitmeta
    setup_env(tmp_path, monkeypatch, [])

    def stale(*a, **k):
        raise ImportError("cannot import name 'project_dir_name' from 'afterwit.config'")

    monkeypatch.setattr(gitmeta, "anchor", stale)
    out = mcp_server.dispatch("save_insight", {
        "type": "gotcha", "title": "Survives a stale process", "project": "acme_hr",
        "body": "The insight must reach the queue anyway.",
    })
    assert "review queue" in out                 # the insight was NOT lost
    assert "Restart the harness" in out          # and the human is told the real cause
    conn = index_db.connect(config.load().db_path)
    card = json.loads(conn.execute("SELECT card_json FROM review_queue").fetchone()["card_json"])
    assert card["title"] == "Survives a stale process"
    assert card.get("source_commit") in (None, "")   # unanchored, and honestly so
    conn.close()


def test_save_insight_still_raises_on_a_real_anchoring_bug(tmp_path, monkeypatch):
    """The escape hatch is scoped to the stale-process class only. Swallowing
    everything is how agent-proposed cards silently shipped unanchored in the
    first place (ADR-020 D5)."""
    import pytest

    from afterwit import gitmeta
    setup_env(tmp_path, monkeypatch, [])

    def boom(*a, **k):
        raise ValueError("a genuine bug in anchoring")

    monkeypatch.setattr(gitmeta, "anchor", boom)
    with pytest.raises(ValueError):
        mcp_server.dispatch("save_insight", {
            "type": "gotcha", "title": "x", "project": "acme_hr", "body": "y"})


def test_lookup_error_does_not_demote_a_foreign_project_but_recall_does(tmp_path,
                                                                       monkeypatch):
    """Same seam as `inject --mode error`, other surface: a stack trace is a
    property of the runtime, not of the repo it fired in, so `lookup_error` passes
    `cross_project=1.0` and `recall` does not. Asserted here because a rank-level
    test cannot see whether the caller still passes it."""
    from tests.test_inject import _ESM_CMD, _ESM_ERR

    setup_env(tmp_path, monkeypatch, [
        make_card(title="ERR_MODULE_NOT_FOUND node esm loader ignores NODE_PATH",
                  type="error_fix", project="reader-app", verified=True,
                  body="Fix: pass --import or use explicit relative specifiers."),
    ])
    cfg_file = tmp_path / "config.toml"          # straddles: 0.2425 exempt, 0.1818 demoted
    cfg_file.write_text(cfg_file.read_text() + "floor = 0.2\n")
    text = f"{_ESM_CMD}\n{_ESM_ERR}"
    assert "ERR_MODULE_NOT_FOUND" in mcp_server.dispatch(
        "lookup_error", {"error_text": text, "project": "acme_hr"})
    assert mcp_server.dispatch(
        "recall", {"query": text, "project": "acme_hr"}) == mcp_server._EMPTY
