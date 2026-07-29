import io
import json

from afterwit import cli, config, index_db
from tests.test_cards import make_card
from tests.test_inject import setup_env


def test_queue_caps_confidence_and_lands_in_review(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch, [make_card(verified=True)])
    proposal = {
        "type": "gotcha", "title": "Smartsheet truncates cells silently",
        "project": "acme_hr", "body": "Chunk before write.",
        "sources": [{"path": "docs/ADR.md", "heading": "Gotcha 7", "kind": "doc"}],
        "confidence": 0.99, "reason": "agent-sweep",
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(proposal)))
    assert cli.main(["queue"]) == 0
    conn = index_db.connect(config.load().db_path, readonly=True)
    rows = conn.execute("SELECT card_json, reason FROM review_queue").fetchall()
    assert len(rows) == 1 and rows[0]["reason"] == "agent-sweep"
    card = json.loads(rows[0]["card_json"])
    assert card["verified"] is False and card["confidence"] <= 0.79  # cap holds
    # never written to the wiki directly
    assert conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 1
