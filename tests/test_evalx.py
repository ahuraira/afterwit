from pathlib import Path

from afterwit import config, evalx
from tests.test_cards import make_card
from tests.test_inject import setup_env


def _golden(tmp_path, queries):
    import yaml
    p = tmp_path / "golden.yaml"
    p.write_text(yaml.safe_dump({"queries": queries}))
    return p


def test_traps_only_passes(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch, [])
    r = evalx.evaluate(config.load(), {"queries": [
        {"kind": "no_answer", "query": "what is the capital of france"},
        {"kind": "no_answer", "query": "write a haiku about ingress controllers"},
    ]})
    assert r["no_answer_precision"] == 1.0 and r["recall_at3"] is None and r["passed"]


def test_hit_found_scores_recall_and_mrr(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch, [
        make_card(title="Prisma P1001 fix use 127.0.0.1", type="error_fix",
                  body="Fix: 127.0.0.1", verified=True, updated="2026-07-05"),
    ])
    r = evalx.evaluate(config.load(), {"queries": [
        {"kind": "hit", "query": "prisma P1001 cannot reach database",
         "project": "acme_hr", "expected_title_contains": "P1001"},
    ]})
    assert r["recall_at3"] == 1.0 and r["mrr"] == 1.0 and r["passed"]


def test_hit_missed_fails_recall_gate(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch, [make_card(title="Prisma P1001 fix", type="error_fix",
                                                verified=True)])
    r = evalx.evaluate(config.load(), {"queries": [
        {"kind": "hit", "query": "playwright screenshot flakiness",
         "expected_title_contains": "playwright"},
    ]})
    assert r["recall_at3"] == 0.0 and not r["recall_pass"] and not r["passed"]


def test_trap_leak_fails_no_answer_gate(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch, [
        make_card(title="Prisma P1001 fix use 127.0.0.1", type="error_fix",
                  body="Fix: 127.0.0.1", verified=True, updated="2026-07-05"),
    ])
    r = evalx.evaluate(config.load(), {"queries": [
        {"kind": "no_answer", "query": "prisma P1001 cannot reach database",
         "project": "acme_hr"},
    ]})
    assert r["no_answer_precision"] == 0.0 and not r["no_answer_pass"] and not r["passed"]


def test_main_exit_codes(tmp_path, monkeypatch):
    # one db; a clean trap passes (exit 0), a trap that a seeded card answers fails (exit 1)
    setup_env(tmp_path, monkeypatch, [
        make_card(title="Prisma P1001 fix", type="error_fix", verified=True,
                  body="Fix: 127.0.0.1", updated="2026-07-05"),
    ])
    good = _golden(tmp_path, [{"kind": "no_answer", "query": "what is the capital of france"}])
    assert evalx.main(str(good)) == 0
    bad = tmp_path / "bad.yaml"
    import yaml
    bad.write_text(yaml.safe_dump({"queries": [
        {"kind": "no_answer", "query": "prisma P1001", "project": "acme_hr"}]}))
    assert evalx.main(str(bad)) == 1


def test_repo_golden_traps_pass(tmp_path, monkeypatch):
    # the committed golden.yaml traps must pass on an empty index. Hit queries
    # are pinned to the user's real cards (absent here), so run the trap subset.
    import yaml

    setup_env(tmp_path, monkeypatch, [])
    repo_golden = Path(__file__).resolve().parents[1] / "eval" / "golden.yaml"
    data = yaml.safe_load(repo_golden.read_text())
    traps = [q for q in data["queries"] if q["kind"] == "no_answer"]
    assert len(traps) >= 6
    subset = tmp_path / "traps.yaml"
    subset.write_text(yaml.safe_dump({"queries": traps}))
    assert evalx.main(str(subset)) == 0
