"""Test-wide isolation from the developer's own harness install.

`harness.config_dir` honours CLAUDE_CONFIG_DIR / CODEX_HOME. A developer who has
either set would otherwise have tests that patch `Path.home()` quietly read — and
`afterwit install` tests quietly WRITE — their real harness config. Clear both
everywhere; the tests that exercise the override set it themselves.
"""

import pytest


@pytest.fixture(autouse=True)
def _no_harness_env(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)


def toml_config(**values: object) -> str:
    """Render config.toml lines with real TOML string escaping.

    A Windows tmp_path (``C:\\Users\\runneradmin\\...``) interpolated raw into a
    TOML basic string turns ``\\U`` into an escape sequence, and tomllib rejects
    the file with "Invalid hex value" — which errored every fixture that wrote a
    config this way and failed the whole Windows CI leg. `json.dumps` output IS
    TOML basic-string syntax; `config._toml_value` uses it for the same reason.
    """
    import json

    return "".join(f"{k} = {json.dumps(str(v))}\n" for k, v in values.items())


def fake_home(monkeypatch, path):
    """Point `Path.home()` and `~` at `path` on every platform. Returns `path`.

    `monkeypatch.setenv("HOME", ...)` alone is a no-op on Windows: `ntpath.expanduser`
    reads USERPROFILE, then HOMEDRIVE+HOMEPATH, and never consults HOME. A test that
    sets only HOME therefore keeps reading — and, for anything that persists state,
    WRITING — the real profile of whoever is running it, which is both a Windows CI
    failure and contamination of the developer's own ~/.afterwit.
    """
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.setenv("USERPROFILE", str(path))
    return path
