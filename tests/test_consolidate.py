import json
from datetime import datetime, timezone

from afterwit import cards, consolidate, index_db, inject
from tests.test_cards import make_card
from tests.test_inject import payload, setup_env

NOW = datetime(2026, 7, 6, tzinfo=timezone.utc)


def db_with(tmp_path, cardlist):
    wiki = tmp_path / "wiki"
    for c in cardlist:
        cards.save(c, wiki)
    conn = index_db.connect(tmp_path / "index.db")
    index_db.rebuild(conn, wiki)
    return conn


def serve(conn, card_ids, ts="2026-07-06T10:00:00+00:00"):
    index_db.log_serving(conn, ts=ts, harness="claude", session_id="s1",
                         mode="inject", query="q", card_ids=card_ids)


def later(after, before=""):
    """A session_text_lookup stub. The miner takes (before, after) from one parse:
    the pre-serving half is the control for the post-serving half."""
    return lambda hn, sid, ts: (before, after)


CARD = ("Prisma P1001 on WSL2 — use 127.0.0.1",
        "Fix: point DATABASE_URL at 127.0.0.1 instead of localhost; the WSL2 bridge "
        "refuses loopback from the Windows host.")


def test_used_vs_ignored_detection():
    applied = ("I'll apply the known fix: pointing DATABASE_URL at 127.0.0.1 "
               "instead of localhost — the WSL2 bridge refuses loopback from the "
               "Windows host, so P1001 should clear.")
    assert consolidate.card_was_used(*CARD, applied, "", "prisma P1001")
    assert not consolidate.card_was_used(*CARD, "Refactoring the React component tree now.",
                                         "", "prisma P1001")


def test_a_card_that_only_echoes_what_the_session_already_said_is_not_used():
    """THE control this signal never had.

    `card_was_used` decides the `usefulness` rank term AND the kill switch, and
    the version before 2026-07-29 scored 95.7% of real card-servings `used` — and
    54.0% of them `used` against a RANDOM UNRELATED session. It could not fail, so
    the gate reading it could not fire. Every case below is one the old rule got
    wrong, and each must stay a NO.
    """
    applied = ("pointing DATABASE_URL at 127.0.0.1 instead of localhost; the WSL2 "
               "bridge refuses loopback from the Windows host")

    # 1. the session had already worked it out before the card arrived
    assert not consolidate.card_was_used(*CARD, applied, prior_text=applied, query="p1001")

    # 2. the query itself carried the answer — the card only echoed it back
    assert not consolidate.card_was_used(*CARD, applied, prior_text="", query=applied)

    # 3. a generic title in a session that trivially contains its own topic. The
    #    old rule scored exactly this shape `used` on 67 of 69 real servings.
    assert not consolidate.card_was_used(
        "Epic 19 — Implementation Checklist", "Steps for the Epic 19 rollout.",
        "continuing the Epic 19 implementation, next item on the checklist",
        prior_text="working through the Epic 19 implementation checklist",
        query="epic 19 implementation checklist")

    # 4. positive control: same card, same session, nothing said beforehand. If
    #    this ever goes red the rule has stopped detecting real use at all, and
    #    the three NOs above would be passing vacuously.
    assert consolidate.card_was_used(*CARD, applied, prior_text="", query="p1001")


def test_mine_servings_updates_usefulness(tmp_path):
    c1 = make_card(title="Prisma P1001 use 127.0.0.1", type="error_fix",
                   body="Fix: point DATABASE_URL at the loopback address instead of "
                        "localhost; the WSL2 bridge refuses it.", verified=True)
    c2 = make_card(title="Smartsheet truncates cells silently", type="gotcha",
                   body="Chunk every write below four thousand characters.", verified=True)
    conn = db_with(tmp_path, [c1, c2])
    serve(conn, [c1.id, c2.id])
    counts = consolidate.mine_servings(conn, later(
        "pointed DATABASE_URL at the loopback address instead of localhost — "
        "the WSL2 bridge refuses it, so P1001 is gone"))
    assert counts == {"used": 1, "ignored": 1, "skipped": 0}
    u1 = conn.execute("SELECT usefulness FROM cards WHERE id=?", (c1.id,)).fetchone()[0]
    u2 = conn.execute("SELECT usefulness FROM cards WHERE id=?", (c2.id,)).fetchone()[0]
    assert u1 == 1.0 and u2 == -0.2
    assert conn.execute("SELECT COUNT(*) FROM servings WHERE outcome IS NULL").fetchone()[0] == 0


def test_missing_transcript_retries_later(tmp_path):
    c = make_card(verified=True)
    conn = db_with(tmp_path, [c])
    serve(conn, [c.id])
    counts = consolidate.mine_servings(conn, lambda hn, sid, ts: None)
    assert counts["skipped"] == 1
    assert conn.execute("SELECT COUNT(*) FROM servings WHERE outcome IS NULL").fetchone()[0] == 1


def test_decay_only_old_unused_positive(tmp_path):
    old_unused = make_card(title="Old lesson", verified=True, updated="2025-11-01")
    fresh = make_card(title="Fresh lesson", verified=True, updated="2026-07-01")
    conn = db_with(tmp_path, [old_unused, fresh])
    conn.execute("UPDATE cards SET usefulness=4.0")
    conn.commit()
    n = consolidate.apply_decay(conn, now=NOW)
    assert n == 1
    assert conn.execute("SELECT usefulness FROM cards WHERE id=?",
                        (old_unused.id,)).fetchone()[0] == 2.0
    assert conn.execute("SELECT usefulness FROM cards WHERE id=?",
                        (fresh.id,)).fetchone()[0] == 4.0


def test_write_back_makes_rebuild_lossless(tmp_path):
    c = make_card(title="Prisma P1001 use 127.0.0.1", type="error_fix",
                  body="Fix: point DATABASE_URL at the loopback address instead of "
                       "localhost; the WSL2 bridge refuses it.", verified=True)
    conn = db_with(tmp_path, [c])
    serve(conn, [c.id])
    consolidate.mine_servings(conn, later(
        "pointed DATABASE_URL at the loopback address instead of localhost — "
        "the WSL2 bridge refuses it, so P1001 is gone"))
    wiki = tmp_path / "wiki"
    assert consolidate.write_back_usage(conn, wiki) == 1
    assert consolidate.write_back_usage(conn, wiki) == 0  # idempotent, no git churn
    # rebuild (or a fresh device cloning the wiki) restores earned scores
    index_db.rebuild(conn, wiki)
    row = conn.execute("SELECT usefulness, last_used FROM cards WHERE id=?",
                       (c.id,)).fetchone()
    assert row["usefulness"] == 1.0 and row["last_used"] is not None


def test_killswitch_trips_only_with_evidence(tmp_path):
    c = make_card(verified=True)
    conn = db_with(tmp_path, [c])
    # 5 servings, all ignored → too few to judge
    for i in range(5):
        serve(conn, [c.id], ts=f"2026-07-0{i%5+1}T10:00:00+00:00")
    consolidate.mine_servings(conn, later("unrelated text"))
    assert consolidate.killswitch_status(conn, now=NOW)["disable"] is False
    for i in range(20):
        serve(conn, [c.id], ts="2026-07-05T10:00:00+00:00")
    consolidate.mine_servings(conn, later("unrelated text"))
    st = consolidate.killswitch_status(conn, now=NOW)
    assert st["disable"] is True and st["hit_rate"] == 0.0
    flag = tmp_path / "inject.disabled"
    assert consolidate.enforce_killswitch(conn, flag, now=NOW) is True
    assert "hit rate" in flag.read_text()


def test_killswitch_silences_inject(tmp_path, monkeypatch):
    root = setup_env(tmp_path, monkeypatch, [
        make_card(title="Prisma P1001 fix", type="error_fix",
                  body="use 127.0.0.1", verified=True, updated="2026-07-05")])
    from afterwit import config
    flag = config.load().db_path.with_name("inject.disabled")
    flag.write_text("off")
    assert inject.run(["--mode", "prompt"],
                      payload("prisma P1001 error", root / "acme_hr")) == ""


def test_lint_flags_code_drift(tmp_path):
    alive = make_card(title="Live file card", verified=True, files=["src/a.ts"])
    dead = make_card(title="Refactored-away card", verified=True, files=["src/gone.ts"])
    conn = db_with(tmp_path, [alive, dead])
    proj = tmp_path / "Projects" / "acme_hr"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "a.ts").write_text("x")
    result = consolidate.lint(conn, now=NOW, projects_root=tmp_path / "Projects")
    assert result["code_drift"] == [dead.id]


def test_lint_finds_broken_links_and_stale(tmp_path):
    linked = make_card(title="Audit refactor", verified=True)
    linker = make_card(title="JSONB decision", verified=True,
                       body="See [[audit-refactor]] and [[ghost-page]].")
    stale = make_card(title="Unreviewed old claim", verified=False,
                      created="2026-05-01", updated="2026-05-01")
    conn = db_with(tmp_path, [linked, linker, stale])
    result = consolidate.lint(conn, now=NOW)
    assert ("ghost-page" in [d for _, d in result["broken_links"]])
    assert all(d != "audit-refactor" for _, d in result["broken_links"])
    assert stale.id in result["stale_unverified"]


def test_killswitch_message_reports_a_near_miss_as_a_near_miss(tmp_path):
    """15/76 = 19.74%. Rendered `.0%` that became "hit rate 20% over 76 servings
    (threshold 20%)" — which reads as a gate that fired at exactly its threshold,
    i.e. as a bug, when it was a genuine miss by ONE card hit. And `served` counts
    card outcomes, not prompts: those 76 came from 40 injections, so the message
    could not be reconciled against the servings table either. This is the real
    2026-07-26 trip, reproduced."""
    # Bodies, not titles: since 2026-07-29 the miner scores what the card ADDED,
    # and a two-word body carries nothing to add.
    used = make_card(title="Alpha zebra quasar",
                     body="Route the export through the dom-splitter before rasterising.",
                     verified=True)
    ignored = make_card(title="Beta narwhal pulsar",
                        body="Chunk every smartsheet write below four thousand characters.",
                        verified=True)
    conn = db_with(tmp_path, [used, ignored])
    for _ in range(15):
        serve(conn, [used.id])
    for _ in range(61):
        serve(conn, [ignored.id])
    consolidate.mine_servings(conn, later(
        "routed the export through the dom splitter before rasterising"))

    st = consolidate.killswitch_status(conn, now=NOW)
    assert (st["served"], st["used"]) == (76, 15) and st["disable"] is True
    flag = tmp_path / "inject.disabled"
    assert consolidate.enforce_killswitch(conn, flag, now=NOW) is True
    text = flag.read_text()
    assert "19.7%" in text and "76 card-servings" in text
    assert "hit rate 20%" not in text  # the rendering that hid the near-miss


def test_the_killswitch_fires_and_actually_silences_push(tmp_path, monkeypatch):
    """The two halves the other kill-switch tests leave out.

    `test_killswitch_trips_only_with_evidence` already watches it fire, and
    `test_killswitch_silences_inject` already watches push go quiet — but the
    second hand-writes the flag, so nothing joins them, and the only negative
    case in the suite is "too few servings to judge". A healthy system staying on
    for the RIGHT reason (enough evidence, good rate) was never asserted at all.

    That gap mattered: the metric behind this gate is the one a held-out control
    caught on 2026-07-29 scoring 95.7% of real card-servings `used` while scoring
    54.0% of unrelated ones the same way. The assertion below is on `inject.run`,
    not on the flag file — a switch that writes a note nobody reads is no switch.
    """
    from afterwit import config
    root = setup_env(tmp_path, monkeypatch, [
        make_card(title="Prisma P1001 fix use 127.0.0.1", type="error_fix",
                  body="Fix: use 127.0.0.1 in DATABASE_URL", verified=True,
                  updated="2026-07-05"),
    ])
    cfg = config.load()
    conn = index_db.connect(cfg.db_path)
    flag = cfg.db_path.with_name("inject.disabled")
    now = datetime.now(timezone.utc).isoformat()  # must land inside the 30-day window
    prompt = payload("prisma P1001 cannot reach database", root / "acme_hr")

    # 21 card-servings clears KILLSWITCH_MIN_SERVINGS; 19/21 = 90% is healthy.
    for i in range(21):
        index_db.log_serving(conn, ts=now, harness="claude", session_id=f"s{i}",
                             mode="inject", query="q", card_ids=["x"])
    conn.execute("UPDATE servings SET outcome='used'")
    conn.execute("UPDATE servings SET outcome='ignored' WHERE id <= 2")
    conn.commit()
    assert consolidate.enforce_killswitch(conn, flag) is False
    assert not flag.exists()
    assert "P1001" in inject.run(["--mode", "prompt"], prompt)

    # Same volume, 2/21 = 9.5% — under KILLSWITCH_HIT_RATE.
    conn.execute("UPDATE servings SET outcome='ignored'")
    conn.execute("UPDATE servings SET outcome='used' WHERE id <= 2")
    conn.commit()
    assert consolidate.enforce_killswitch(conn, flag) is True
    assert "hit rate" in flag.read_text()
    assert inject.run(["--mode", "prompt"], prompt) == ""
    conn.close()


def test_explicit_feedback_replaces_the_mined_guess_on_the_serving_it_rates(tmp_path):
    """`feedback` is the agent stating an outcome; the miner is inferring one.
    Where both exist the statement wins — and it is SUBSTITUTED, not added, so a
    positive-only channel cannot inflate a safety gate.

    The control is the second card: same session text, no feedback row, so it
    still takes the mined verdict. Without it this would pass against a miner
    that simply marked everything used.
    """
    rated = make_card(title="Smartsheet cells truncate silently", type="gotcha",
                      body="Chunk below 4000 chars.", verified=True)
    control = make_card(title="Prisma P1001 needs 127.0.0.1", type="error_fix",
                        body="Fix: use 127.0.0.1 in DATABASE_URL", verified=True)
    conn = db_with(tmp_path, [rated, control])
    serve(conn, [rated.id, control.id], ts="2026-07-06T10:00:00+00:00")
    conn.execute(
        "INSERT INTO servings(ts,harness,session_id,mode,query,card_ids,outcome) "
        "VALUES(?,?,?,?,?,?,?)",
        ("2026-07-06T10:05:00+00:00", "mcp", "", "feedback", "helpful",
         json.dumps([rated.id]), "feedback:helpful"))
    conn.commit()

    # Session text mentions NEITHER card: the miner alone would score both ignored.
    counts = consolidate.mine_servings(conn, later("refactoring the router"))
    assert counts == {"used": 1, "ignored": 1, "skipped": 0}
    u = dict(conn.execute("SELECT id, usefulness FROM cards").fetchall())
    assert u[rated.id] == 1.0, "the explicit helpful verdict must win"
    assert u[control.id] == -0.2, "the unrated card must still take the mined verdict"


def test_mine_servings_hands_the_miner_the_pre_serving_text_and_the_query(tmp_path):
    """The caller, not the rule. `card_was_used` is proven to use `prior_text` and
    `query` by its own tests — and `mine_servings` calling it with `("", "")`
    instead still left all 362 tests green. That is ADR Gotcha #79 exactly: a test
    of the scoring function cannot see whether its caller still passes the inputs.

    Paired, because the first half alone would pass against a miner that scored
    nothing used, and the second alone against one that scored everything used.
    """
    body = ("Fix: point DATABASE_URL at the loopback address instead of localhost; "
            "the WSL2 bridge refuses it.")
    echo = ("pointed DATABASE_URL at the loopback address instead of localhost — "
            "the WSL2 bridge refuses it")
    c = make_card(title="Prisma P1001", type="error_fix", body=body, verified=True)
    conn = db_with(tmp_path, [c])

    # The session had already said it BEFORE the card arrived → taught nothing.
    serve(conn, [c.id])
    assert consolidate.mine_servings(conn, later(echo, before=echo))["used"] == 0

    # Same card, same later text, nothing said beforehand → a real use.
    conn.execute("UPDATE cards SET usefulness=0")
    serve(conn, [c.id], ts="2026-07-06T11:00:00+00:00")
    assert consolidate.mine_servings(conn, later(echo))["used"] == 1


def test_too_few_novel_tokens_is_not_evidence(tmp_path):
    """A card whose body adds two words cannot prove it was used by having those
    two words turn up — that is the coincidence rate, not a signal. Pins
    MIN_NOVEL_TOKENS: dropping it to 0 left every other test green."""
    thin = make_card(title="Smartsheet limits", type="gotcha",
                     body="Chunk writes.", verified=True)
    conn = db_with(tmp_path, [thin])
    serve(conn, [thin.id])
    assert consolidate.mine_servings(conn, later("I will chunk writes here"))["used"] == 0
