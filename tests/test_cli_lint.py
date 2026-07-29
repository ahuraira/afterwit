import json

from afterwit import cli
from tests.test_cards import make_card
from tests.test_inject import setup_env


def test_cli_lint_opens_writable_index_for_drift_refresh(tmp_path, monkeypatch, capsys):
    setup_env(tmp_path, monkeypatch, [
        make_card(verified=True, files=[], body="Plain verified fact.")
    ])

    assert cli.main(["lint"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "broken_links": [], "code_drift": [], "stale_unverified": []
    }
