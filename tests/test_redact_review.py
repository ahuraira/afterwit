"""Redaction hardening + auto-review trust boundary (ADR-021).

These are security tests. Each one should fail loudly if the property it names
stops holding — the ADR-020 lesson was that a test you only reasoned about is
not a test. Where a check is cheap to fake, it asserts on observable output
(what landed on disk, what reached the driver), not on an internal call count.
"""
import json

import pytest

from afterwit import cards, config, index_db, redact, review, ui
from tests.test_cards import make_card
from tests.conftest import toml_config

# --- redaction ---------------------------------------------------------------

LEAKS = {
    "anthropic_key": "sk-ant-api03-" + "A" * 40,
    "openai_key": "sk-proj-" + "a" * 32,
    "github_token": "ghp_" + "B" * 36,
    "github_pat": "github_pat_11ABCDEF_" + "c" * 40,
    # synthetic like its neighbours on purpose: a realistic-looking fake trips
    # GitHub push protection and blocks the push, secret or not.
    "slack_token": "xoxb-" + "0" * 12 + "-" + "X" * 24,
    "slack_webhook": "https://hooks.slack.com/services/T00/B00/XXXXXXXXXXXX",
    "google_key": "AIzaSy" + "D" * 33,
    "npm_token": "npm_" + "e" * 36,
    "stripe_key": "sk_live_51" + "F" * 20,
    "aws_key": "AKIAIOSFODNN7EXAMPLE",
    "aws_secret": "aws_secret_access_key wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "aws_session_token": "AWS_SESSION_TOKEN=FwoGZXIvYXdzEExampleSessionTokenValue123",
    "refresh_token": "refresh_token: 1//0eXaMPLErEfReShToKeN123456789",
    "bearer": "Authorization: Bearer " + "g" * 32,
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w",
    "private_key_full": "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----",
    "private_key_header": "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAA",
    "url_password": "postgres://user:hunter2@db.internal:5432/app",
    "email": "reach me at alice.smith@corp.example.com",
}


@pytest.mark.parametrize("name,text", sorted(LEAKS.items()))
def test_every_credential_class_is_redacted(name, text):
    out = redact.redact(text)
    assert "[REDACTED:" in out, f"{name} leaked: {out!r}"


@pytest.mark.parametrize("text", [
    "git@github.com:auser/afterwit.git",   # every SSH clone URL, and repo_url
    "see src/afterwit/redact.py and tests/test_ui.py",
    "raise CardError('card missing token')",  # bare keyword, no assignment
    "the model is claude-opus-4-8",
])
def test_ordinary_text_survives_untouched(text):
    assert redact.redact(text) == text


@pytest.mark.parametrize("text", sorted(LEAKS.values()))
def test_sanitize_is_idempotent(text):
    once = redact.sanitize(text)
    assert redact.sanitize(once) == once, "a marker re-matched a pattern"


def test_home_paths_become_tilde_for_any_user():
    assert redact.scrub_home("/home/alice/x.py") == "~/x.py"
    assert redact.scrub_home("/Users/bob/dev/y.ts") == "~/dev/y.ts"
    assert redact.scrub_home(r"C:\Users\Carol\code\z.rs") == r"~\code\z.rs"


# --- the write boundary: cards.save, not just adapters ------------------------

def test_save_redacts_the_card_not_just_the_transcript(tmp_path):
    """The artifact that leaves this machine is the CARD — the wiki is git-pushed.
    `save_insight` and `afterwit queue` write cards that never touch an adapter."""
    c = make_card(
        title="deploy with ghp_" + "Z" * 36,
        body="run `curl -H 'Authorization: Bearer " + "t" * 32 + "'` from /home/user/x",
    )
    c.sources = [{"path": "/home/user/Desktop/Projects/p/notes.md", "lines": "L1"}]
    path = cards.save(c, tmp_path)
    written = path.read_text()

    assert "ghp_" not in written
    assert "t" * 32 not in written
    assert "/home/user" not in written
    assert "~/Desktop/Projects/p/notes.md" in written
    # and the in-memory card matches disk, so the subsequent upsert_card indexes
    # what was actually written
    assert "[REDACTED:github_token]" in c.title


def test_save_leaves_clean_cards_byte_identical(tmp_path):
    c = make_card(title="Prisma P1001 on WSL2", body="Use 127.0.0.1 in DATABASE_URL.")
    before = (c.title, c.body)
    cards.save(c, tmp_path)
    assert (c.title, c.body) == before


# --- auto-review: the deterministic vetoes a model cannot overrule ------------

def _boom(_prompt):
    raise AssertionError("the model must never be consulted for a gated card")


def test_preference_cards_are_never_auto_reviewed():
    cfg = config.Config(wiki_root=".", db_path=".", projects_root=".")  # type: ignore[arg-type]
    v = review.review_one(cfg, make_card(type="preference"), driver=_boom)
    assert v.verdict == "abstain" and v.model == "gate"


def test_a_card_carrying_a_secret_is_rejected_without_asking_the_model():
    cfg = config.Config(wiki_root=".", db_path=".", projects_root=".")  # type: ignore[arg-type]
    c = make_card(body="key was [REDACTED:anthropic_key] so we rotated it")
    v = review.review_one(cfg, c, driver=_boom)
    assert v.verdict == "reject" and v.model == "gate"


def test_low_confidence_and_long_bodies_abstain():
    cfg = config.Config(wiki_root=".", db_path=".", projects_root=".")  # type: ignore[arg-type]
    assert review.review_one(cfg, make_card(confidence=0.3), driver=_boom).verdict == "abstain"
    assert review.review_one(cfg, make_card(body="x" * 2000), driver=_boom).verdict == "abstain"


@pytest.mark.parametrize("raw", [
    "", "yes, approve it", "{not json", '{"verdict": "APPROVE_MAYBE"}',
    '{"reason": "looks fine"}',
])
def test_anything_unreadable_is_an_abstain_never_an_approve(raw):
    assert review._parse(raw, "m").verdict == "abstain"


def test_duplicate_verdict_key_cannot_smuggle_an_approve():
    """Audit claim 5 (critical): {"verdict":"reject","verdict":"approve"} resolves
    to the LAST value under normal dict semantics. A duplicated key is ambiguous
    and must abstain, not silently approve."""
    v = review._parse('{"verdict":"reject","verdict":"approve"}', "m")
    assert v.verdict == "abstain"


@pytest.mark.parametrize("raw", [
    '{"verdict": ["approve"]}',            # list
    '{"verdict": {"value": "approve"}}',   # nested object
    '{"verdict": true}',                   # bool
])
def test_non_string_verdict_abstains(raw):
    assert review._parse(raw, "m").verdict == "abstain"


def test_reviewer_equal_to_distiller_is_overridden_not_honoured(capsys):
    """Audit claim 6: an explicit auto_review_driver == distill_driver would void
    separation of duties. It must be overridden to the opposite, with a warning."""
    cfg = config.Config(wiki_root=".", db_path=".", projects_root=".",  # type: ignore[arg-type]
                        distill_driver="codex", auto_review_driver="codex")
    assert review.reviewer_name(cfg) == "claude-p"
    assert "overriding reviewer" in capsys.readouterr().err


def test_apply_verdict_regates_a_forged_approve(tmp_path):
    """Audit claim 7: apply_verdict is the last step before a card is written
    verified. A forged approve on a gated card (here a preference) must be blocked
    at persistence even though it never went through review_one."""
    cfg = _cfg(tmp_path)
    conn = index_db.connect(cfg.db_path)
    ui.queue_insert(conn, make_card(type="preference"), "x")
    rowid = conn.execute("SELECT rowid FROM review_queue").fetchone()[0]
    outcome = review.apply_verdict(cfg, conn, rowid,
                                   review.Verdict("approve", "forged", "codex"))
    assert outcome != "approved"
    assert not list(cards.iter_cards(cfg.wiki_root))  # nothing written verified
    assert conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0] == 1


def test_normalize_url_strips_embedded_credentials():
    from afterwit import gitmeta
    assert gitmeta.normalize_url(
        "https://alice:ghp_SECRETTOKEN@github.com/o/r.git") == "https://github.com/o/r"
    assert gitmeta.normalize_url("git@github.com:o/r.git") == "https://github.com/o/r"


def test_repo_url_credential_is_scrubbed_on_save(tmp_path):
    c = make_card()
    c.repo_url = "https://u:ghp_" + "T" * 36 + "@github.com/o/r"
    path = cards.save(c, tmp_path)
    assert "ghp_" not in path.read_text()


def test_a_reviewer_that_raises_abstains():
    cfg = config.Config(wiki_root=".", db_path=".", projects_root=".")  # type: ignore[arg-type]
    def explode(_):
        raise RuntimeError("cli not installed")
    assert review.review_one(cfg, make_card(), driver=explode).verdict == "abstain"


def test_the_reviewer_is_never_the_distiller():
    """Separation of duties is the property ADR-011 was actually buying."""
    base = dict(wiki_root=".", db_path=".", projects_root=".")
    assert review.reviewer_name(config.Config(**base, distill_driver="codex")) == "claude-p"  # type: ignore[arg-type]
    assert review.reviewer_name(config.Config(**base, distill_driver="claude-p")) == "codex"  # type: ignore[arg-type]


def test_reviewer_receives_cited_source_evidence(tmp_path):
    source = tmp_path / "result.md"
    source.write_text("setup\nThe migration passed after using 127.0.0.1.\ntrailer\n")
    card = make_card(sources=[{"path": str(source), "lines": "L2-L2"}])
    seen = {}

    def driver(prompt):
        seen["prompt"] = prompt
        return '{"verdict":"approve","reason":"supported"}'

    assert review.review_one(_cfg(tmp_path), card, driver=driver).verdict == "approve"
    assert "migration passed after using 127.0.0.1" in seen["prompt"]


def test_reviewer_abstains_when_source_is_unavailable(tmp_path):
    card = make_card(sources=[{"path": str(tmp_path / "missing.md"), "lines": "L1"}])
    assert review.review_one(_cfg(tmp_path), card, driver=_boom).verdict == "abstain"


# --- applying verdicts -------------------------------------------------------

def _cfg(tmp_path, **kw):
    cfg = config.Config(wiki_root=tmp_path / "wiki", db_path=tmp_path / "i.db",
                        projects_root=tmp_path / "P", **kw)
    cfg.wiki_root.mkdir(parents=True, exist_ok=True)
    return cfg


def _queued(tmp_path, card=None):
    cfg = _cfg(tmp_path)
    conn = index_db.connect(cfg.db_path)
    source = tmp_path / "evidence.md"
    source.write_text("The migration succeeded after applying the documented fix.\n")
    card = card or make_card()
    card.sources = [{"path": str(source), "lines": "L1-L1"}]
    ui.queue_insert(conn, card, "test")
    rowid = conn.execute("SELECT rowid FROM review_queue").fetchone()[0]
    return cfg, conn, rowid


def test_auto_approval_stamps_who_cleared_it(tmp_path):
    cfg, conn, rowid = _queued(tmp_path)
    review.apply_verdict(cfg, conn, rowid, review.Verdict("approve", "resolved fix", "codex"))
    row = conn.execute("SELECT id FROM cards").fetchone()
    saved = next(c for _, c in cards.iter_cards(cfg.wiki_root) if c.id == row["id"])
    assert saved.verified is True
    assert saved.reviewed_by == "codex"  # attributable, and greppable to undo
    assert "auto-review approve [codex]" in config.log_path(cfg.wiki_root).read_text()
    assert conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0] == 0


def test_human_approval_still_says_human(tmp_path):
    cfg, conn, rowid = _queued(tmp_path)
    ui._approve(cfg, conn, rowid, None)
    _, card = next(iter(cards.iter_cards(cfg.wiki_root)))
    assert card.reviewed_by == "human"


def test_abstain_leaves_the_card_queued_for_a_human(tmp_path):
    cfg, conn, rowid = _queued(tmp_path)
    review.apply_verdict(cfg, conn, rowid, review.Verdict("abstain", "unsure", "codex"))
    assert conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0] == 1
    assert not list(cards.iter_cards(cfg.wiki_root))  # nothing was written


def test_dry_run_changes_nothing(tmp_path):
    cfg, conn, _ = _queued(tmp_path)
    counts = review.review_queue(cfg, conn, dry_run=True,
                                 driver=lambda p: '{"verdict":"approve","reason":"ok"}')
    assert counts["approved"] == 1
    assert conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0] == 1
    assert not list(cards.iter_cards(cfg.wiki_root))


def test_one_bad_row_does_not_stop_the_drain(tmp_path):
    cfg = _cfg(tmp_path)
    conn = index_db.connect(cfg.db_path)
    conn.execute("INSERT INTO review_queue(card_json, reason, created) VALUES(?,?,?)",
                 ("{not json", "corrupt", "2026-01-01"))
    source = tmp_path / "evidence.md"
    source.write_text("The proposed claim was speculative.\n")
    card = make_card(sources=[{"path": str(source), "lines": "L1-L1"}])
    ui.queue_insert(conn, card, "good")
    counts = review.review_queue(cfg, conn,
                                 driver=lambda p: '{"verdict":"reject","reason":"speculative"}')
    assert counts == {"approved": 0, "rejected": 1, "abstained": 0, "errors": 1,
                      "corroborated": 0, "stopped": 0}


def test_autoreview_endpoints_are_403_when_disabled(tmp_path):
    cfg = _cfg(tmp_path)
    assert cfg.auto_review is False  # default off


# --- SQLite is a second persistence sink (audit claim 4) ---------------------

def test_queue_insert_sanitizes_before_storing_card_json(tmp_path):
    """save_insight / afterwit queue / the distiller all reach the queue via
    queue_insert, none through cards.save(). A raw secret must not sit in the
    review_queue in the clear, be shown in the UI, or be fed to the auto-reviewer."""
    cfg = _cfg(tmp_path)
    conn = index_db.connect(cfg.db_path)
    secret = "ghp_" + "R" * 36
    ui.queue_insert(conn, make_card(title=f"deploy {secret}", body=f"token {secret}"), "x")
    raw = conn.execute("SELECT card_json FROM review_queue").fetchone()[0]
    assert secret not in raw
    assert "[REDACTED:github_token]" in raw


def test_a_raw_secret_reaching_the_queue_is_gated_after_sanitize(tmp_path):
    """The whole point of sanitizing at the queue: has_secret now sees the marker,
    so the auto-review gate rejects a card that arrived carrying a credential."""
    cfg = _cfg(tmp_path, auto_review=True)
    conn = index_db.connect(cfg.db_path)
    ui.queue_insert(conn, make_card(body="key sk-ant-" + "A" * 40), "x")
    data = json.loads(conn.execute("SELECT card_json FROM review_queue").fetchone()[0])
    card = cards.Card(**{k: v for k, v in data.items()
                         if k in cards.Card.__dataclass_fields__})
    v = review.review_one(cfg, card, driver=_boom)  # must never reach the model
    assert v.verdict == "reject" and v.model == "gate"


def test_upsert_card_sanitizes_what_gets_served(tmp_path):
    """recall/inject/MCP serve straight from the cards table. Even a direct
    upsert (bypassing cards.save) must not leave a servable secret in the index."""
    cfg = _cfg(tmp_path)
    conn = index_db.connect(cfg.db_path)
    secret = "ghp_" + "S" * 36
    c = make_card(title=f"note {secret}", body=f"body {secret}")
    # forge a path without calling cards.save(), the way the audit repro did
    index_db.upsert_card(conn, c, str(cfg.wiki_root / "x.md"))
    row = conn.execute("SELECT title, body FROM cards WHERE id=?", (c.id,)).fetchone()
    assert secret not in row["title"] and secret not in row["body"]
    fts = conn.execute("SELECT body FROM cards_fts WHERE id=?", (c.id,)).fetchone()
    assert secret not in fts["body"]


# --- the public-remote push guard --------------------------------------------

def test_sync_refuses_to_push_a_public_wiki(tmp_path, monkeypatch, capsys):
    import subprocess

    from afterwit import cli
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    subprocess.run(["git", "init", "-q", str(wiki)], check=True)
    subprocess.run(["git", "-C", str(wiki), "remote", "add", "origin",
                    "https://github.com/someone/public-repo.git"], check=True)
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text(toml_config(wiki_root=wiki, db_path=tmp_path / "i.db"))
    monkeypatch.setenv("AFTERWIT_CONFIG", str(cfg_file))
    monkeypatch.setattr(cli, "_remote_visibility", lambda url: "public")

    assert cli._cmd_sync(None) == 1
    err = capsys.readouterr().err
    assert "REFUSING TO PUSH" in err and "allow_public_wiki_remote" in err


@pytest.mark.parametrize("url", [
    "https://GitHub.com/owner/repo.git",   # uppercase host
    "https://github.com:443/owner/repo",   # explicit port
    "git@GitHub.com:owner/repo.git",       # uppercase SSH host
])
def test_remote_visibility_parses_atypical_urls(url, monkeypatch):
    """Audit claim 8: case/port variants previously returned 'unknown' and the
    push proceeded. They must reach the gh check like any other github URL."""
    import shutil
    import subprocess

    from afterwit import cli
    monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/gh")
    calls = {}

    def fake_run(argv, **kw):
        calls["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="true\n", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert cli._remote_visibility(url) == "private"
    assert "owner/repo" in calls["argv"]  # the regex extracted the repo slug


def test_sync_proceeds_when_the_remote_is_private(tmp_path, monkeypatch):
    """Proves the guard is not simply blocking everything: same setup, private
    remote, and we get past the guard to the (expected) push failure."""
    import subprocess

    from afterwit import cli
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    subprocess.run(["git", "init", "-q", str(wiki)], check=True)
    subprocess.run(["git", "-C", str(wiki), "remote", "add", "origin",
                    str(tmp_path / "nonexistent.git")], check=True)
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text(toml_config(wiki_root=wiki, db_path=tmp_path / "i.db"))
    monkeypatch.setenv("AFTERWIT_CONFIG", str(cfg_file))
    monkeypatch.setattr(cli, "_remote_visibility", lambda url: "private")

    rc = cli._cmd_sync(None)
    assert rc == 1  # pull fails (no such remote) — but we REACHED the pull


def test_duplicate_of_an_approved_card_is_corroborated_without_asking_a_model(tmp_path):
    """Re-asking a model to judge a claim its owner already approved is the wrong
    question at any price. 114 of 371 queued cards were duplicates of approved ones.

    The drain must be deterministic (no driver call), must fold the new evidence into
    the approved card, and must NOT rewrite the body a human signed off on.
    """
    cfg = _cfg(tmp_path)
    conn = index_db.connect(cfg.db_path)
    src_a, src_b = tmp_path / "a.md", tmp_path / "b.md"
    src_a.write_text("evidence a\n")
    src_b.write_text("evidence b\n")

    approved = make_card(title="Prisma P1001 use 127.0.0.1",
                         body="On WSL2 localhost resolves to ::1; use 127.0.0.1.",
                         verified=True, sources=[{"path": str(src_a), "lines": "L1-L1"}])
    path = cards.save(approved, cfg.wiki_root)
    index_db.upsert_card(conn, approved, str(path))
    conn.commit()

    dup = make_card(title="Prisma P1001 use 127.0.0.1",
                    body="On WSL2 localhost resolves to ::1; use 127.0.0.1!",
                    sources=[{"path": str(src_b), "lines": "L1-L1"}])
    ui.queue_insert(conn, dup, "high-confidence-unverified")

    def explode(prompt):
        raise AssertionError("the auto-reviewer was asked to re-judge approved knowledge")

    counts = review.review_queue(cfg, conn, driver=explode)

    assert counts["corroborated"] == 1
    assert conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0] == 0
    kept = {c.id: c for _, c in cards.iter_cards(cfg.wiki_root)}[approved.id]
    assert kept.body == "On WSL2 localhost resolves to ::1; use 127.0.0.1."   # approved wording
    assert kept.verified is True
    # scrub_home, because the card-write boundary sanitizes source paths and a
    # Windows tmp_path lives UNDER C:\Users\<name> — so the stored path is `~\...`
    # there and the raw path on Linux, where /tmp is not below a home dir.
    assert ({s["path"] for s in kept.sources}
            == {redact.scrub_home(str(src_a)), redact.scrub_home(str(src_b))})  # evidence grew


def test_reviewed_by_names_the_model_not_the_driver(tmp_path, monkeypatch):
    """`reviewed_by: claude-p` (223 cards in the live wiki) records which CLI ran,
    not which model approved — and separation of duties is a claim about MODELS.
    With no `auto_review_model` set, resolve it from the harness's own config."""
    from pathlib import Path

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".claude").mkdir(parents=True)
    (tmp_path / ".claude" / "settings.json").write_text('{"model": "opus[1m]"}', encoding="utf-8")
    cfg = _cfg(tmp_path, distill_driver="codex", auto_review_effort="high")
    source = tmp_path / "evidence.md"
    source.write_text("The migration succeeded after applying the documented fix.\n")
    card = make_card()
    card.sources = [{"path": str(source), "lines": "L1-L1"}]

    v = review.review_one(cfg, card, driver=lambda _: '{"verdict": "approve", "reason": "ok"}')
    assert v.model == "claude-p:opus[1m]:high"   # reviewer is the NOT-distiller driver
