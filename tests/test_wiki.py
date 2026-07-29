from afterwit import cards, config, index_db, wiki
from tests.test_cards import make_card


def _cfg(tmp_path):
    return config.Config(
        wiki_root=tmp_path / "wiki",
        db_path=tmp_path / "index.db",
        projects_root=tmp_path / "Projects",
    )


def _conn(cfg):
    cfg.wiki_root.mkdir(parents=True, exist_ok=True)
    return index_db.connect(cfg.db_path)


def _seed(cfg, conn, card):
    path = cards.save(card, cfg.wiki_root)
    index_db.upsert_card(conn, card, str(path))
    conn.commit()
    return card


def test_write_action_saves_and_indexes(tmp_path):
    cfg = _cfg(tmp_path)
    conn = _conn(cfg)
    card = make_card()
    out = wiki.execute(("write", card), cfg, conn)
    assert out is card
    assert (cfg.wiki_root / card.relpath()).exists()
    assert conn.execute("SELECT COUNT(*) FROM cards WHERE id=?", (card.id,)).fetchone()[0] == 1
    assert "write" in wiki.log_path(cfg.wiki_root).read_text()


def test_merge_unions_sources_keeps_newer_body(tmp_path):
    cfg = _cfg(tmp_path)
    conn = _conn(cfg)
    ex = _seed(cfg, conn, make_card(
        body="old body", sources=[{"path": "a.jsonl", "lines": "L1-L2"}]))
    cand = make_card(body="NEW body", sources=[{"path": "b.jsonl", "lines": "L9"}])
    out = wiki.execute(("merge", ex.id, cand), cfg, conn)
    assert out.id == ex.id and out.body == "NEW body"
    assert {s["path"] for s in out.sources} == {"a.jsonl", "b.jsonl"}
    row = conn.execute("SELECT body FROM cards WHERE id=?", (ex.id,)).fetchone()
    assert row["body"] == "NEW body"


def test_merge_into_an_APPROVED_card_never_rewrites_the_approved_body(tmp_path):
    """The safety property that used to be bought by queuing, now enforced here.

    postprocess no longer queues duplicates of verified cards (they came back forever),
    so merge is now reachable on an approved card — and merge overwrote the body. A
    human read and approved that exact wording (ADR-011); replacing it with fresh model
    prose would edit an approved claim with nobody looking. Evidence accrues; the
    approved words stand.
    """
    cfg = _cfg(tmp_path)
    conn = _conn(cfg)
    ex = _seed(cfg, conn, make_card(
        body="Body a human read and approved.", verified=True,
        sources=[{"path": "a.jsonl", "lines": "L1-L2"}]))
    cand = make_card(body="Fresh model prose nobody reviewed.",
                     sources=[{"path": "b.jsonl", "lines": "L9"}])

    out = wiki.execute(("merge", ex.id, cand), cfg, conn)

    assert out.body == "Body a human read and approved."       # untouched
    assert out.verified is True                                 # still approved
    assert {s["path"] for s in out.sources} == {"a.jsonl", "b.jsonl"}   # evidence grew
    row = conn.execute("SELECT body FROM cards WHERE id=?", (ex.id,)).fetchone()
    assert row["body"] == "Body a human read and approved."


def test_supersede_retires_old_writes_new(tmp_path):
    cfg = _cfg(tmp_path)
    conn = _conn(cfg)
    ex = _seed(cfg, conn, make_card(title="Use JSONB for audit"))
    cand = make_card(title="Use typed columns for audit")
    out = wiki.execute(("supersede", ex.id, cand), cfg, conn)
    assert out.id == cand.id
    old = conn.execute("SELECT status, superseded_by FROM cards WHERE id=?", (ex.id,)).fetchone()
    assert old["status"] == "superseded" and old["superseded_by"] == cand.id
    assert conn.execute("SELECT status FROM cards WHERE id=?", (cand.id,)).fetchone()["status"] == "active"


def test_queue_action_inserts_review_row(tmp_path):
    cfg = _cfg(tmp_path)
    conn = _conn(cfg)
    card = make_card(confidence=0.6)
    assert wiki.execute(("queue", card, "low-confidence"), cfg, conn) is None
    row = conn.execute("SELECT reason FROM review_queue").fetchone()
    assert row["reason"] == "low-confidence"
    assert conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 0


def test_regenerate_writes_index_and_brief(tmp_path):
    cfg = _cfg(tmp_path)
    conn = _conn(cfg)
    _seed(cfg, conn, make_card(type="decision", title="JSONB for audit"))
    _seed(cfg, conn, make_card(type="gotcha", title="Smartsheet truncates cells"))
    wiki.regenerate(cfg, conn)
    idx = (cfg.wiki_root / "index.md").read_text()
    assert "acme_hr" in idx and "JSONB for audit" in idx
    brief = (cfg.wiki_root / "projects" / "acme_hr" / "brief.md").read_text()
    assert "Active decisions" in brief and "JSONB for audit" in brief
    assert "Smartsheet truncates cells" in brief


def test_managed_fence_preserves_user_bytes(tmp_path):
    p = tmp_path / "CLAUDE.md"
    original = "# My project\n\nUser rules above.\n\n" + \
        "<!-- afterwit:begin -->\nold auto content\n<!-- afterwit:end -->\n\nUser rules below.\n"
    p.write_text(original)
    above, below = original.split("<!-- afterwit:begin -->")[0], original.split("<!-- afterwit:end -->")[1]

    wiki.write_managed_block(p, "## fresh\n- decision: X")
    result = p.read_text()
    assert result.split("<!-- afterwit:begin -->")[0] == above
    assert result.split("<!-- afterwit:end -->")[1] == below
    assert "fresh" in result and "old auto content" not in result


def test_managed_fence_created_at_eof_when_absent(tmp_path):
    p = tmp_path / "CLAUDE.md"
    p.write_text("# Project\n\nExisting content, no fence.\n")
    wiki.write_managed_block(p, "auto line")
    result = p.read_text()
    assert result.startswith("# Project\n\nExisting content, no fence.\n")
    assert "<!-- afterwit:begin -->\nauto line\n<!-- afterwit:end -->" in result
