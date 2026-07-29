from datetime import datetime, timezone

from afterwit import cards, index_db, rank
from tests.test_cards import make_card

NOW = datetime(2026, 7, 6, tzinfo=timezone.utc)


def build_db(tmp_path, cardlist):
    wiki = tmp_path / "wiki"
    for c in cardlist:
        cards.save(c, wiki)
    conn = index_db.connect(tmp_path / "index.db")
    n = index_db.rebuild(conn, wiki)
    assert n == len(cardlist)
    return conn


def test_search_and_rank_order(tmp_path):
    conn = build_db(tmp_path, [
        make_card(title="Prisma P1001 postgres unreachable on WSL2", type="error_fix",
                  body="Error P1001. Fix: use 127.0.0.1 not localhost in DATABASE_URL.",
                  verified=True, updated="2026-07-01"),
        make_card(title="Choose pnpm over npm", type="decision",
                  body="pnpm chosen for workspace speed.", verified=True, updated="2026-07-01"),
    ])
    rows = index_db.search(conn, "prisma cannot reach postgres P1001")
    scored = rank.rank(rows, "acme_hr", k=5, now=NOW)
    assert scored and "P1001" in scored[0].title
    assert all(scored[i].score >= scored[i + 1].score for i in range(len(scored) - 1))


def test_floor_returns_nothing_for_irrelevant(tmp_path):
    conn = build_db(tmp_path, [make_card(verified=True)])
    rows = index_db.search(conn, "kubernetes ingress helm chart rollout")
    assert rank.rank(rows, None, k=5, now=NOW) == [] or not rows


def test_non_active_dropped_unverified_halved(tmp_path):
    old = make_card(title="Use JSONB for audit", status="superseded", verified=True)
    new_unverified = make_card(title="Use typed columns for audit", verified=False,
                               updated="2026-07-05")
    conn = build_db(tmp_path, [old, new_unverified])
    rows = index_db.search(conn, "audit storage typed columns JSONB")
    scored = rank.rank(rows, None, floor=0.0, k=5, now=NOW)
    assert [s.title for s in scored] == ["Use typed columns for audit"]
    assert scored[0].score <= 0.5 * 1.3 + 0.2  # unverified ×0.5 bound sanity


def test_project_boost(tmp_path):
    a = make_card(title="Vitest isolate true always", project="breeze", verified=True)
    b = make_card(title="Vitest isolate true forever", project="portfolio", verified=True)
    conn = build_db(tmp_path, [a, b])
    rows = index_db.search(conn, "vitest isolate setting")
    scored = rank.rank(rows, "breeze", floor=0.0, k=5, now=NOW)
    assert scored[0].project == "breeze"


def test_match_query_sanitizes_operators():
    q = index_db.build_match_query('fix the "auth" OR (NOT) middleware NEAR/2 token*')
    assert "(" not in q and "*" not in q.replace('"', "")
    assert "auth" in q and "middleware" in q
    assert '"fix"' not in q  # generic verbs are stopwords — zero retrieval signal


def test_for_file(tmp_path):
    conn = build_db(tmp_path, [make_card(files=["src/audit/store.ts"], verified=True)])
    rows = index_db.for_file(conn, "audit/store")
    assert len(rows) == 1


def test_a_foreign_project_card_is_demoted_but_a_global_one_is_not(tmp_path):
    """The +0.15 boost could not do this job. `floor` is absolute, so lifting the
    home project never pushes a foreign card BELOW it — measured on real servings,
    foreign cards were used 3/7 = 43% against same-project 32/43 = 74%, and two
    that survived the floor on "make the public one commit" came from two unrelated
    projects. `global` is exempt: cross-cutting is what that project means."""
    home = make_card(title="Vitest isolate true always", project="breeze", verified=True)
    foreign = make_card(title="Vitest isolate true always", project="portfolio",
                        verified=True)
    shared = make_card(title="Vitest isolate true always", project="global",
                       verified=True)
    conn = build_db(tmp_path, [home, foreign, shared])
    rows = index_db.search(conn, "vitest isolate setting")
    by = {s.project: s.score for s in rank.rank(rows, "breeze", floor=0.0, k=5, now=NOW)}
    assert by["portfolio"] < by["global"], "a foreign project's card was not demoted"
    assert by["global"] < by["breeze"], "the home project must still outrank global"
    # and the demotion is what the factor says it is, not merely "some number lower"
    # 1e-4, not 1e-9: rank() returns round(s, 4), so the product of an exact
    # factor and a rounded score is off by up to 5e-5 on arithmetic alone.
    assert abs(by["portfolio"] - by["global"] * rank.CROSS_PROJECT_FACTOR) < 1e-4


def test_an_error_lookup_does_not_demote_a_foreign_project(tmp_path):
    """A stack trace is a property of the runtime, not of the repo it fired in.

    This seam is load-bearing and was found by replay, not by reasoning: at every
    factor <= 0.9 the penalty killed one card that had been MINED AS USED — a
    `reader-app` card about the Node ESM loader ignoring NODE_PATH, matched from
    a different project on an ERR_MODULE_NOT_FOUND trace. Demoting that is
    deleting the reason the store spans projects at all. Callers that pass 1.0:
    `inject._error_mode` and `mcp_server._lookup_error`.
    """
    foreign = make_card(title="Node ESM loader ignores NODE_PATH ERR_MODULE_NOT_FOUND",
                        type="error_fix", project="reader-app", verified=True)
    conn = build_db(tmp_path, [foreign])
    rows = index_db.search(conn, "ERR_MODULE_NOT_FOUND node esm loader NODE_PATH")
    demoted = rank.rank(rows, "acme_flow", floor=0.0, k=5, now=NOW)
    exempt = rank.rank(rows, "acme_flow", floor=0.0, k=5, now=NOW, cross_project=1.0)
    assert exempt[0].score > demoted[0].score
    assert abs(demoted[0].score - exempt[0].score * rank.CROSS_PROJECT_FACTOR) < 1e-4
