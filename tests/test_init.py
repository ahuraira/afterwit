"""`afterwit init` is the one command. It must finish with a system that WORKS,
and it must know whether it does.

Every outage this project has shipped was an install that reported success and
left agents unable to reach a healthy index. So init's contract is not "I ran the
installers" — it is "I checked, and an agent can reach afterwit". It exits nonzero
when it cannot.
"""

import argparse

import pytest

from afterwit import cli


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    """A blank machine: empty HOME, a projects dir, no config, no index."""
    (tmp_path / "Desktop" / "Projects").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setenv("AFTERWIT_CONFIG", str(tmp_path / ".afterwit" / "config.toml"))
    # Never touch the real harness config, the real systemd, or the real PATH.
    monkeypatch.setattr("afterwit.install.main", lambda target: 0)
    # `gh` must resolve to None. Every other name is pretended onto PATH, but a truthy
    # `gh` sends _cmd_init down the sync-repo branch, where `--yes` answers the prompt
    # and `gh repo create afterwit-knowledge --private` RUNS — against the real account
    # of whoever ran the suite, pointed at a tmp_path that is deleted moments later.
    monkeypatch.setattr("shutil.which", lambda n: None if n == "gh" else "/usr/bin/" + n)
    return tmp_path


def _args(**kw):
    return argparse.Namespace(**{"yes": True, "repo": None, "no_install": False, **kw})


def test_init_builds_the_index(fresh, monkeypatch):
    """A fresh install with no index makes `recall` say "no index yet", which every
    agent reads as "this user has no knowledge". init must leave a real index."""
    monkeypatch.setattr(cli, "_cmd_doctor", lambda args: 0)
    assert cli._cmd_init(_args()) == 0
    assert (fresh / ".afterwit" / "index.db").exists()


def test_init_returns_doctors_verdict_not_a_cheerful_zero(fresh, monkeypatch):
    """The load-bearing one. If agents cannot reach afterwit, init must SAY SO by
    failing — not print 'done' over a broken install, which is precisely how the
    last three outages stayed invisible."""
    monkeypatch.setattr(cli, "_cmd_doctor", lambda args: 1)   # a door is shut
    assert cli._cmd_init(_args()) == 1


def test_init_verifies_by_default(fresh, monkeypatch):
    """init must actually CALL doctor — an installer that cannot verify its own work
    is how 'reported success, actually broken' happens."""
    called = {}
    monkeypatch.setattr(cli, "_cmd_doctor", lambda args: called.setdefault("ran", True) and 0)
    cli._cmd_init(_args())
    assert called.get("ran") is True


def test_no_install_stops_before_touching_the_harness(fresh, monkeypatch):
    monkeypatch.setattr(cli, "_cmd_doctor",
                        lambda args: pytest.fail("--no-install must not run doctor"))
    assert cli._cmd_init(_args(no_install=True)) == 0
