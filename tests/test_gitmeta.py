"""Commit-anchored staleness (ADR-018): git layer, drift detection, demotion.

The load-bearing case is `test_rewritten_file_is_stale` — a file that still
exists but was rewritten under the card. The pre-ADR-018 existence check could
not see it, so the stale card ranked as fresh.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone

import pytest

from afterwit import cards, consolidate, gitmeta, index_db, rank
from tests.conftest import fake_home
from tests.test_cards import make_card


def _run(repo, *args, date=None):
    """`git rev-list --before` filters on the COMMITTER date, so a test that
    backdates a commit must set both dates — `--date` alone moves only the author."""
    env = None
    if date:
        env = {**os.environ, "GIT_COMMITTER_DATE": date, "GIT_AUTHOR_DATE": date}
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True, env=env)


def _repo(root, name="proj", content="original body"):
    """A real git repo with one committed file. Returns (path, first_sha)."""
    r = root / name
    r.mkdir(parents=True)
    _run(r, "init", "-q")
    _run(r, "config", "user.email", "t@t.t")
    _run(r, "config", "user.name", "t")
    (r / "a.py").write_text(content)
    _run(r, "add", "-A")
    _run(r, "commit", "-qm", "one")
    return r, gitmeta.head_commit(r)


def _conn_with(tmp_path, card):
    conn = index_db.connect(tmp_path / "index.db")
    index_db.upsert_card(conn, card, str(tmp_path / "c.md"))
    conn.commit()
    return conn


# --- gitmeta primitives -----------------------------------------------------

def test_normalize_url_collapses_protocols():
    assert gitmeta.normalize_url("git@github.com:o/r.git") == "https://github.com/o/r"
    assert gitmeta.normalize_url("https://github.com/o/r.git") == "https://github.com/o/r"
    assert gitmeta.normalize_url("https://github.com/o/r") == "https://github.com/o/r"


def test_non_repo_degrades_to_none(tmp_path):
    plain = tmp_path / "not_a_repo"
    plain.mkdir()
    assert gitmeta.head_commit(plain) is None
    assert gitmeta.remote_url(plain) is None
    assert gitmeta.is_repo(plain) is False
    assert gitmeta.changed_files(plain, "deadbeef") is None


def test_changed_files_between_commits(tmp_path):
    repo, sha0 = _repo(tmp_path)
    (repo / "a.py").write_text("rewritten entirely")
    (repo / "b.py").write_text("new file")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-qm", "two")
    assert gitmeta.changed_files(repo, sha0) == {"a.py", "b.py"}
    # HEAD vs itself: no drift
    assert gitmeta.changed_files(repo, gitmeta.head_commit(repo)) == set()


def test_unknown_commit_returns_none_not_empty(tmp_path):
    """None ("cannot tell") must not be confused with set() ("nothing changed")
    — a sha from another device would otherwise read as 'no drift'."""
    repo, _ = _repo(tmp_path)
    assert gitmeta.changed_files(repo, "0" * 40) is None


def test_discover_maps_remote_url_to_local_path(tmp_path):
    repo, _ = _repo(tmp_path, name="weird-local-name")
    _run(repo, "remote", "add", "origin", "git@github.com:o/r.git")
    assert gitmeta.discover(tmp_path) == {"https://github.com/o/r": repo}


# --- drift detection --------------------------------------------------------

def test_rewritten_file_is_stale(tmp_path):
    """THE case the existence check misses: a.py still exists, but its contents
    changed after the card was written."""
    repo, sha0 = _repo(tmp_path)
    card = make_card(project="proj", files=["a.py"], source_commit=sha0)
    conn = _conn_with(tmp_path, card)

    assert consolidate.mark_stale(conn, tmp_path) == []  # nothing changed yet

    (repo / "a.py").write_text("completely different implementation")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-qm", "rewrite")

    assert consolidate.mark_stale(conn, tmp_path) == [card.id]
    assert conn.execute("SELECT stale FROM cards WHERE id=?", (card.id,)).fetchone()[0] == 1
    assert (repo / "a.py").exists()  # existence check would have said "fine"


def test_untouched_file_is_not_stale(tmp_path):
    repo, sha0 = _repo(tmp_path)
    card = make_card(project="proj", files=["a.py"], source_commit=sha0)
    conn = _conn_with(tmp_path, card)
    (repo / "other.py").write_text("unrelated")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-qm", "unrelated change")
    assert consolidate.mark_stale(conn, tmp_path) == []


def test_no_source_commit_falls_back_to_existence_check(tmp_path):
    repo, _ = _repo(tmp_path)
    card = make_card(project="proj", files=["gone.py"], source_commit=None)
    conn = _conn_with(tmp_path, card)
    assert consolidate.mark_stale(conn, tmp_path) == [card.id]  # file never existed

    fresh = make_card(project="proj", files=["a.py"], source_commit=None)
    conn2 = _conn_with(tmp_path / "d2", fresh)
    assert consolidate.mark_stale(conn2, tmp_path) == []  # a.py exists → not stale
    assert repo.exists()


def test_unknown_commit_falls_back_not_crashes(tmp_path):
    """A sha authored on another device: git can't diff it. Fall back to the
    existence check rather than assuming drift or assuming freshness."""
    _repo(tmp_path)
    card = make_card(project="proj", files=["a.py"], source_commit="0" * 40)
    conn = _conn_with(tmp_path, card)
    assert consolidate.mark_stale(conn, tmp_path) == []  # a.py exists


def test_project_absent_on_this_device_is_not_drift(tmp_path):
    """Absence of a checkout is ignorance, not drift — device 2 must not flag
    every card for projects it hasn't cloned."""
    card = make_card(project="never_cloned", files=["a.py"], source_commit="abc")
    conn = _conn_with(tmp_path, card)
    assert consolidate.mark_stale(conn, tmp_path) == []


def test_mark_stale_clears_flag_when_drift_resolved(tmp_path):
    repo, sha0 = _repo(tmp_path)
    card = make_card(project="proj", files=["a.py"], source_commit=sha0)
    conn = _conn_with(tmp_path, card)
    (repo / "a.py").write_text("rewrite")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-qm", "rewrite")
    assert consolidate.mark_stale(conn, tmp_path) == [card.id]

    # re-anchor the card to current HEAD (what a fresh survey does) → not stale
    conn.execute("UPDATE cards SET source_commit=? WHERE id=?",
                 (gitmeta.head_commit(repo), card.id))
    assert consolidate.mark_stale(conn, tmp_path) == []
    assert conn.execute("SELECT stale FROM cards WHERE id=?", (card.id,)).fetchone()[0] == 0


# --- demotion ---------------------------------------------------------------

def _row(**over):
    base = dict(id="x", title="approval chain state machine", body="body text here",
                type="capability", project="p", updated="2026-07-01",
                usefulness=0.0, verified=1, status="active", bm25_raw=-5.0, stale=0)
    base.update(over)
    return base


NOW = datetime(2026, 7, 10, tzinfo=timezone.utc)  # pin recency; scores must be deterministic


def _score(stale, floor=0.0, type="capability"):
    return rank.rank([_row(stale=stale, type=type)], None, floor=floor, k=5,
                     query_text="approval chain", now=NOW)


def test_stale_card_is_demoted_by_half():
    fresh, drifted = _score(0), _score(1)
    assert drifted[0].score == pytest.approx(fresh[0].score * rank.STALE_FACTOR, abs=1e-3)
    assert drifted[0].stale is True and fresh[0].stale is False


def test_stale_demotion_can_drop_below_floor():
    """Demotion has teeth: a marginal stale card falls out of the results
    entirely rather than misdirecting an agent (Manifesto P3)."""
    floor = _score(0)[0].score * 0.75  # between demoted (0.5×) and fresh
    assert _score(1, floor=floor) == []
    assert _score(0, floor=floor) != []  # the fresh twin still survives


def test_missing_stale_column_never_demotes():
    """Pre-ADR-018 readonly index: absent column → treated as fresh, not stale."""
    row = _row()
    del row["stale"]
    scored = rank.rank([row], None, floor=0.0, k=5, query_text="approval chain", now=NOW)
    assert scored[0].stale is False


# --- card contract ----------------------------------------------------------

def test_source_commit_round_trips_through_frontmatter():
    c = make_card(source_commit="abc123")
    assert cards.parse(cards.render(c)).source_commit == "abc123"


def test_source_commit_absent_is_omitted_from_frontmatter():
    c = make_card(source_commit=None)
    text = cards.render(c)
    assert "source_commit" not in text
    assert cards.parse(text).source_commit is None


# --- retroactive anchoring ---------------------------------------------------

def test_commit_at_returns_head_on_or_before_date(tmp_path):
    repo, _ = _repo(tmp_path)
    _run(repo, "commit", "--amend", "--no-edit", "-q", date="2026-01-01T00:00:00")
    old = gitmeta.head_commit(repo)
    (repo / "a.py").write_text("later")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-qm", "later", date="2026-06-01T00:00:00")

    assert gitmeta.commit_at(repo, "2026-03-01") == old      # before the 2nd commit
    assert gitmeta.commit_at(repo, "2026-07-01") == gitmeta.head_commit(repo)
    assert gitmeta.commit_at(repo, "2025-01-01") is None     # predates the repo


def test_backfill_anchors_old_cards_and_is_idempotent(tmp_path):
    repo, _ = _repo(tmp_path)
    _run(repo, "commit", "--amend", "--no-edit", "-q", date="2026-01-01T00:00:00")
    anchor = gitmeta.head_commit(repo)

    card = make_card(project="proj", files=["a.py"], source_commit=None,
                     created="2026-02-01", updated="2026-02-01")
    wiki = tmp_path / "wiki"
    path = cards.save(card, wiki)
    conn = index_db.connect(tmp_path / "index.db")
    index_db.upsert_card(conn, card, str(path))
    conn.commit()

    assert consolidate.backfill_anchors(conn, tmp_path) == 1
    # frontmatter is the source of truth — it must carry the anchor
    assert cards.parse(path.read_text()).source_commit == anchor
    assert conn.execute("SELECT source_commit FROM cards WHERE id=?",
                        (card.id,)).fetchone()[0] == anchor
    assert consolidate.backfill_anchors(conn, tmp_path) == 0  # idempotent


def test_backfill_skips_cards_predating_the_repo(tmp_path):
    _repo(tmp_path)  # first commit is "now"
    card = make_card(project="proj", files=["a.py"], source_commit=None,
                     created="2020-01-01", updated="2020-01-01")
    wiki = tmp_path / "wiki"
    path = cards.save(card, wiki)
    conn = index_db.connect(tmp_path / "index.db")
    index_db.upsert_card(conn, card, str(path))
    conn.commit()
    assert consolidate.backfill_anchors(conn, tmp_path) == 0
    assert cards.parse(path.read_text()).source_commit is None  # existence-check fallback


def test_anchored_card_with_dead_pointer_is_stale(tmp_path):
    """Regression: commit-diff alone reports 'fresh' for a card citing a path
    that was NEVER in the repo (scratch /tmp path, typo, wrong project) — such a
    path can never appear in a diff. The dead-pointer check must still fire even
    when the repo has not changed since the anchor."""
    repo, sha0 = _repo(tmp_path)
    card = make_card(project="proj", files=["docs/never_existed.md"], source_commit=sha0)
    conn = _conn_with(tmp_path, card)
    assert gitmeta.changed_files(repo, sha0) == set()   # repo unchanged: empty diff
    assert consolidate.mark_stale(conn, tmp_path) == [card.id]


def test_any_dead_pointer_makes_the_card_stale(tmp_path):
    """ADR-020 D6: a card is only as good as its WEAKEST pointer. Requiring every
    cited file to be missing let a card citing `a.py` + `ghost.py` read as fresh."""
    repo, sha0 = _repo(tmp_path)
    card = make_card(project="proj", files=["a.py", "ghost.py"], source_commit=sha0)
    conn = _conn_with(tmp_path, card)
    assert consolidate.mark_stale(conn, tmp_path) == [card.id]

    live = make_card(project="proj", files=["a.py"], source_commit=sha0)
    conn2 = _conn_with(tmp_path / "d2", live)
    assert consolidate.mark_stale(conn2, tmp_path) == []
    assert repo.exists()


def test_absolute_path_outside_repo_is_a_dead_pointer(tmp_path):
    """ADR-020 D4: `Path('/proj') / '/tmp/x'` == `/tmp/x`. A LIVE scratch file
    outside the repo would otherwise count as a valid pointer, and being untracked
    it never shows up in a diff either — permanently 'fresh'."""
    repo, sha0 = _repo(tmp_path)
    live_outside = tmp_path / "outside.css"
    live_outside.write_text("real file, but not in the repo")
    card = make_card(project="proj", files=[str(live_outside)], source_commit=sha0)
    conn = _conn_with(tmp_path, card)
    assert live_outside.exists()  # it exists...
    assert consolidate.mark_stale(conn, tmp_path) == [card.id]  # ...and is still dead
    assert repo.exists()


def test_parent_escape_is_a_dead_pointer(tmp_path):
    repo, sha0 = _repo(tmp_path)
    card = make_card(project="proj", files=["../escape.py"], source_commit=sha0)
    conn = _conn_with(tmp_path, card)
    assert consolidate.mark_stale(conn, tmp_path) == [card.id]
    assert repo.exists()


def test_only_capability_cards_are_demoted_for_drift(tmp_path):
    """Drift demotes POINTERS, not knowledge. A decision citing a file that got
    edited is still true — demoting it made a verified card unretrievable in the
    real corpus (eval caught it: 20 of 23 drifted cards were not capabilities)."""
    fresh_dec = _score(0, type="decision")[0].score
    stale_dec = _score(1, type="decision")[0].score
    assert stale_dec == fresh_dec          # knowledge is not demoted
    assert _score(1, type="decision")[0].stale is True  # ...but drift is still reported

    for t in ("gotcha", "error_fix", "fact", "preference"):
        assert _score(1, type=t)[0].score == _score(0, type=t)[0].score

    # the pointer type still demotes
    assert _score(1, type="capability")[0].score < _score(0, type="capability")[0].score
    assert tmp_path


def test_inject_never_imports_gitmeta_at_module_scope():
    """The in-process check in test_embed pops `afterwit.gitmeta` and re-runs inject, so
    it catches a RUNTIME import. It cannot catch a module-scope one — `afterwit.inject`
    is already cached, so `from . import gitmeta` never re-executes. A clean
    interpreter is the only way to pin that. `afterwit inject` is p95 < 200ms: it must
    never be one import away from shelling out to git."""
    code = (
        "import sys; from afterwit import inject; "
        "assert 'afterwit.gitmeta' not in sys.modules, "
        "'inject imported gitmeta: ' + repr(sorted(m for m in sys.modules if m.startswith('afterwit.')))"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr or r.stdout


# --- ADR-020: defects found by adversarial audit ----------------------------

def test_rebuild_then_restale_keeps_the_flag(tmp_path, monkeypatch):
    """D1 (critical): index_db.rebuild() DELETEs cards, zeroing every derived
    `stale` flag. `afterwit sync` rebuilds at the END of every nightly — AFTER lint —
    so the demotion feature was silently inert during all serving."""
    repo, sha0 = _repo(tmp_path)
    wiki = tmp_path / "wiki"
    card = make_card(project="proj", type="capability", files=["a.py"],
                     source_commit=sha0, verified=True)
    cards.save(card, wiki)
    conn = index_db.connect(tmp_path / "index.db")
    index_db.rebuild(conn, wiki)

    (repo / "a.py").write_text("rewritten")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-qm", "rewrite")

    assert consolidate.mark_stale(conn, tmp_path) == [card.id]
    assert conn.execute("SELECT stale FROM cards WHERE id=?", (card.id,)).fetchone()[0] == 1

    index_db.rebuild(conn, wiki)  # what afterwit sync does at the end of the nightly
    assert conn.execute("SELECT stale FROM cards WHERE id=?", (card.id,)).fetchone()[0] == 0, \
        "rebuild zeroes stale — this is the bug"

    consolidate.mark_stale(conn, tmp_path)  # the fix: recompute after any rebuild
    assert conn.execute("SELECT stale FROM cards WHERE id=?", (card.id,)).fetchone()[0] == 1


def test_cross_device_resolves_by_repo_url_not_folder_name(tmp_path):
    """D2: the card was written on a device where the repo lived at `original`;
    here the same repo is cloned as `different-local-name`. Only repo_url can
    bridge that. Previously this silently reported no drift."""
    repo, sha0 = _repo(tmp_path, name="different-local-name")
    _run(repo, "remote", "add", "origin", "git@github.com:org/portable.git")
    url = gitmeta.remote_url(repo)

    card = make_card(project="original-device-slug", files=["a.py"],
                     source_commit=sha0, repo_url=url)
    conn = _conn_with(tmp_path, card)
    assert not (tmp_path / "original-device-slug").exists()

    assert consolidate.mark_stale(conn, tmp_path) == []  # resolves, nothing changed yet
    (repo / "a.py").write_text("rewritten on this device")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-qm", "rewrite")
    assert consolidate.mark_stale(conn, tmp_path) == [card.id]


def test_merge_reanchors_so_refresh_can_clear_drift(tmp_path):
    """My own finding: merge adopted the newer body but kept the OLD anchor, so a
    refreshed capability card was flagged stale the instant it was refreshed and
    the ADR-014 drift-triggered refresh loop could never converge."""
    from afterwit import config as config_mod
    from afterwit import wiki as wiki_mod

    repo, sha_old = _repo(tmp_path)
    cfg = config_mod.Config(wiki_root=tmp_path / "wiki", db_path=tmp_path / "i.db",
                            projects_root=tmp_path)
    cfg.wiki_root.mkdir(parents=True)
    conn = index_db.connect(cfg.db_path)
    old = make_card(project="proj", type="capability", title="Approval machine",
                    files=["a.py"], source_commit=sha_old)
    index_db.upsert_card(conn, old, str(cards.save(old, cfg.wiki_root)))

    (repo / "a.py").write_text("v2")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-qm", "two")
    sha_new = gitmeta.head_commit(repo)
    cand = make_card(project="proj", type="capability", title="Approval machine",
                     body="new body describing current code", files=["a.py"],
                     source_commit=sha_new)

    merged = wiki_mod.execute(("merge", old.id, cand), cfg, conn)
    assert merged.source_commit == sha_new  # re-anchored to the refreshed body
    assert consolidate.mark_stale(conn, cfg.projects_root) == []  # drift cleared


def test_device_id_is_stable_and_not_just_hostname(tmp_path, monkeypatch):
    """D8: two machines can share a hostname; the shared log filename would
    recreate the rebase conflict ADR-019 removed."""
    from afterwit import config as config_mod

    fake_home(monkeypatch, tmp_path)
    monkeypatch.setattr("socket.gethostname", lambda: "shared-host")
    first = config_mod.device_id()
    assert first.startswith("shared-host-") and len(first) > len("shared-host-")
    assert config_mod.device_id() == first  # persisted, stable across calls

    fake_home(monkeypatch, tmp_path / "other-machine")
    assert config_mod.device_id() != first  # different device, same hostname


def test_save_insight_stamps_an_anchor(tmp_path, monkeypatch):
    """D5: MCP-proposed capability cards reached the queue with source_commit=None
    even in a git checkout, so drift for them fell back to the weak check."""
    from afterwit import config as config_mod
    from afterwit import mcp_server

    repo, sha = _repo(tmp_path)
    _run(repo, "remote", "add", "origin", "git@github.com:org/proj.git")
    cfg = config_mod.Config(wiki_root=tmp_path / "wiki", db_path=tmp_path / "i.db",
                            projects_root=tmp_path)
    cfg.wiki_root.mkdir(parents=True)
    conn = index_db.connect(cfg.db_path)
    mcp_server._save_insight(cfg, conn, {
        "type": "capability", "title": "Approval machine", "project": "proj",
        "body": "lives at a.py", "files": ["a.py"],
    })
    import json as _json
    queued = _json.loads(conn.execute("SELECT card_json FROM review_queue").fetchone()[0])
    assert queued["source_commit"] == sha
    assert queued["repo_url"] == "https://github.com/org/proj"
