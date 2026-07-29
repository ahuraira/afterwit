"""Curated links (ADR-045): kNN candidates, closed-set judge, validated write,
one-command strip. The property under test throughout: nothing the judge SAYS
can create an edge to a card it was not OFFERED — that intersection is what
makes the unreviewed write defensible, so it gets the adversarial cases."""

import json

from afterwit import cards, config, embed, index_db, relink, ui
from afterwit import mcp_server
from tests.conftest import toml_config
from tests.test_cards import make_card


def _card(title, vec_hint=None, **over):
    # Plain body + no files on purpose: make_card's defaults carry a wikilink
    # and a files entry, which would add edges these tests did not create.
    over.setdefault("body", f"Plain body about {title}.")
    over.setdefault("files", [])
    return make_card(title=title, **over)


def setup(tmp_path, monkeypatch, cardlist, vectors):
    wiki = tmp_path / "wiki"
    for c in cardlist:
        cards.save(c, wiki)
    db = tmp_path / "index.db"
    conn = index_db.connect(db)
    index_db.rebuild(conn, wiki)
    embed.ensure_schema(conn)
    for cid, vs in vectors.items():
        conn.execute("INSERT INTO vectors(id, body_hash, vec) VALUES(?,?,?)",
                     (cid, "h", embed._blob(vs)))
    conn.commit()
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(toml_config(wiki_root=wiki, db_path=db,
                                    projects_root=tmp_path / "Projects"))
    monkeypatch.setenv("AFTERWIT_CONFIG", str(cfg_file))
    return conn, wiki


def _corpus(tmp_path, monkeypatch):
    """One source, two related neighbours, one near-DUPLICATE, one distant card,
    one near-but-dead. Cosines against `a`: twin ≈ 1.00 (above DUP_CEILING),
    b ≈ 0.87, c ≈ 0.80, dead ≈ 0.85 (but superseded), far = 0 (below floor)."""
    # distinct `updated` on every card: eligibility orders by `updated DESC`, and
    # a date tie makes "which card is judged second" nondeterministic — the crash
    # test below depends on it being `b`
    a = _card("Prisma drops the partial index in every migration",
              updated="2026-07-06")
    b = _card("Partial index must be re-added after prisma migrate")  # 07-05 default
    c = _card("Prisma checksum drift on migration 0007", updated="2026-07-04")
    twin = _card("Prisma drops the partial index in every migration (dup)",
                 updated="2026-07-03T12:00:00")
    far = _card("Choose JSONB for audit payloads", updated="2026-07-03")
    dead = _card("Old prisma index gotcha", status="superseded", updated="2026-07-02")
    conn, wiki = setup(tmp_path, monkeypatch, [a, b, c, twin, far, dead], {
        a.id: (1.0, 0.0, 0.0),
        b.id: (0.9, 0.5, 0.0),
        c.id: (0.8, 0.6, 0.0),
        twin.id: (0.999, 0.02, 0.0),  # a near-duplicate, not a relation
        far.id: (0.0, 1.0, 0.0),      # cosine 0 with a — below COS_FLOOR
        dead.id: (0.85, 0.5, 0.2),
    })
    return conn, wiki, a, b, c, twin, far, dead


def test_candidates_are_nearest_active_only(tmp_path, monkeypatch):
    conn, _, a, b, c, twin, far, dead = _corpus(tmp_path, monkeypatch)
    got = [cid for cid, _ in relink.candidates(conn, a.id)]
    assert got[:2] == [b.id, c.id]  # nearest first
    assert far.id not in got     # below the candidate floor
    assert twin.id not in got    # above DUP_CEILING — a duplicate, not a relation
    assert dead.id not in got    # superseded — never offered to the judge
    assert a.id not in got       # never its own neighbour


def test_judge_is_closed_set_and_capped(tmp_path, monkeypatch):
    """A judge reply naming self, a fabricated ULID, a card that exists but was
    never offered, and — in prose OUTSIDE the JSON array — an offered candidate
    it rejected: all must be discarded. Only array-listed offered ids survive."""
    conn, _, a, b, c, twin, far, dead = _corpus(tmp_path, monkeypatch)
    fabricated = cards.new_ulid()
    reply = json.dumps([a.id, fabricated, far.id, b.id])  # far exists, not offered

    def driver(prompt):
        assert a.title in prompt  # {card} really was filled with the source
        assert b.id in prompt and c.id in prompt  # both really were offered
        # c is named only in the narration — a parser that regexes the whole
        # reply instead of slicing to the array would wrongly keep it
        return f"Keeping these:\n{reply}\nI rejected {c.id} as boilerplate overlap."

    cand_rows = [conn.execute("SELECT * FROM cards WHERE id=?", (i,)).fetchone()
                 for i, _ in relink.candidates(conn, a.id)]
    card_row = conn.execute("SELECT * FROM cards WHERE id=?", (a.id,)).fetchone()
    kept = relink.judge(driver, card_row, cand_rows, relink._load_prompt())
    assert kept == [b.id]
    assert relink.MAX_RELATED == 3  # the cap the slice below relies on
    assert len(relink.judge(lambda p: json.dumps([b.id, c.id] * 5), card_row,
                            cand_rows, "{card}{candidates}")) <= 3


def test_validate_refuses_dead_or_self_targets(tmp_path, monkeypatch):
    conn, _, a, b, c, twin, far, dead = _corpus(tmp_path, monkeypatch)
    assert relink.validate(conn, a.id,
                           [a.id, dead.id, cards.new_ulid(), b.id]) == [b.id]
    # validate caps independently of judge — defense in depth, not dead code
    assert len(relink.validate(conn, dead.id, [a.id, b.id, c.id, far.id])) == 3


def test_relink_writes_frontmatter_links_memo_and_never_rejudges(tmp_path, monkeypatch):
    conn, wiki, a, b, *_ = _corpus(tmp_path, monkeypatch)
    out = relink.relink(conn, lambda p: json.dumps([b.id]), budget=1)
    assert out["judged"] == 1 and out["linked"] == 1 and out["edges"] == 1

    linked_src = conn.execute(
        "SELECT src, dst FROM links WHERE kind='related'").fetchone()
    src, dst = linked_src["src"], linked_src["dst"]
    # frontmatter is the source of truth — the on-disk card must carry the id
    path = conn.execute("SELECT path FROM cards WHERE id=?", (src,)).fetchone()["path"]
    assert cards.load(__import__("pathlib").Path(path)).related == [dst]

    # judged cards are memoed: a second run judges the REMAINING cards (b, c,
    # far — legitimate) but never re-consults the judge about `a`, and a's kept
    # link survives the second run's empty verdicts untouched
    out2 = relink.relink(conn, lambda p: "[]", budget=9)
    assert out2["judged"] == 4
    assert cards.load(__import__("pathlib").Path(path)).related == [dst]


def test_a_judged_card_with_no_kept_links_is_still_memoed(tmp_path, monkeypatch):
    """'Judged, nothing kept' is a result. Without the memo the nightly re-buys
    the same rejection every night forever."""
    conn, wiki, a, *_ = _corpus(tmp_path, monkeypatch)
    out = relink.relink(conn, lambda p: "[]", budget=9)
    assert out["judged"] == 5  # a, b, c, twin, far are active with vectors; dead is not
    assert out["linked"] == 0
    assert conn.execute("SELECT COUNT(*) FROM relinked").fetchone()[0] == 5
    assert conn.execute("SELECT COUNT(*) FROM links WHERE kind='related'").fetchone()[0] == 0


def test_dry_run_is_side_effect_free(tmp_path, monkeypatch):
    conn, wiki, a, b, *_ = _corpus(tmp_path, monkeypatch)
    out = relink.relink(conn, lambda p: json.dumps([b.id]), budget=9, dry_run=True)
    assert out["proposals"]  # it did propose something to hand-judge
    assert conn.execute("SELECT COUNT(*) FROM links WHERE kind='related'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM relinked").fetchone()[0] == 0
    # and a later REAL run still judges — dry-run left no memo behind
    assert relink.relink(conn, lambda p: "[]", budget=9)["judged"] == 5


def test_rebuild_is_lossless_for_related_links(tmp_path, monkeypatch):
    """P4: the edge must come back from frontmatter alone after the cache is wiped."""
    conn, wiki, a, b, *_ = _corpus(tmp_path, monkeypatch)
    relink.relink(conn, lambda p: json.dumps([b.id]), budget=1)
    index_db.rebuild(conn, wiki)
    rows = conn.execute("SELECT src, dst FROM links WHERE kind='related'").fetchall()
    assert len(rows) == 1 and rows[0]["dst"] == b.id


def test_strip_erases_the_whole_class_in_one_command(tmp_path, monkeypatch):
    """The reversibility that justifies skipping per-link review: one command,
    zero related links left in frontmatter, index, or memo."""
    conn, wiki, a, b, *_ = _corpus(tmp_path, monkeypatch)
    relink.relink(conn, lambda p: json.dumps([b.id]), budget=4)
    assert conn.execute("SELECT COUNT(*) FROM links WHERE kind='related'").fetchone()[0] > 0

    n = relink.strip(conn, wiki)
    assert n >= 1
    assert conn.execute("SELECT COUNT(*) FROM links WHERE kind='related'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM relinked").fetchone()[0] == 0
    for _, card in cards.iter_cards(wiki):
        assert card.related == []


def test_a_judge_crash_mid_batch_keeps_the_finished_cards(tmp_path, monkeypatch):
    """The judge is an LLM subprocess; it WILL die mid-batch eventually. Cards
    already judged must keep their write and memo — otherwise every crash
    re-buys every call. Budget-order is `updated DESC`, so `a` (newest) is
    judged first and the crash lands on the second card, `b`."""
    conn, wiki, a, b, *_ = _corpus(tmp_path, monkeypatch)
    calls = {"n": 0}

    def dies_on_second(prompt):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("driver died")
        return json.dumps([b.id])

    try:
        relink.relink(conn, dies_on_second, budget=9)
    except RuntimeError:
        pass
    # reopen: only COMMITTED state survives a process death
    conn.close()
    conn2 = index_db.connect(config.load().db_path)
    assert conn2.execute("SELECT COUNT(*) FROM links WHERE kind='related'").fetchone()[0] == 1
    assert conn2.execute("SELECT COUNT(*) FROM relinked").fetchone()[0] == 1


def test_prompt_template_still_has_its_placeholders():
    """judge() fills {card}/{candidates} by literal replace — an edit to
    prompts/relink.md that drops one would silently send the template verbatim
    and judge nothing real."""
    t = relink._load_prompt()
    assert "{card}" in t and "{candidates}" in t


def test_graph_and_related_tool_surface_the_edge(tmp_path, monkeypatch):
    conn, wiki, a, b, *_ = _corpus(tmp_path, monkeypatch)
    relink.relink(conn, lambda p: json.dumps([b.id]), budget=1)

    edges = [e for e in ui._graph(conn)["edges"] if e["kind"] == "related"]
    assert len(edges) == 1

    cfg = config.load()
    src = edges[0]["source"]
    out = mcp_server._related(cfg, conn, {"card_id": src})
    assert "[related]" in out
    # and the reverse direction: asking from the target finds the source
    back = mcp_server._related(cfg, conn, {"card_id": edges[0]["target"]})
    assert "[related]" in back
