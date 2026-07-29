"""doctor: the relocation check.

A checkout install bakes an absolute repo path into the mcp args, the hook command
and the systemd unit. Moving the folder kills all three at once — including the hook
that would have warned you, since it is itself referenced by the dead path. Nothing
inside afterwit can self-heal that, so the check must at minimum NOTICE.
"""

import json
from pathlib import Path

from afterwit import cli, install

REPO = install._repo_root()
# doctor prints the registered path as a Path, so the expected text is separator-
# dependent: `\gone\moved-away` on Windows.
MOVED = str(Path("/gone/moved-away"))


def _run_doctor(home, monkeypatch, capsys, registered_args):
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {
        install.MCP_NAME: {"type": "stdio", "command": "uv", "args": registered_args}}}))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    cli.main(["doctor"])
    return capsys.readouterr().out


def test_doctor_flags_a_moved_checkout(tmp_path, monkeypatch, capsys):
    out = _run_doctor(tmp_path, monkeypatch, capsys,
                      ["run", "--project", "/gone/moved-away", "afterwit", "serve-mcp"])
    assert "FAIL  registered repo path is current" in out
    assert MOVED in out and str(REPO) in out                # names BOTH paths
    assert "doctor --fix" in out                            # and the recovery


def test_doctor_accepts_the_current_checkout(tmp_path, monkeypatch, capsys):
    out = _run_doctor(tmp_path, monkeypatch, capsys,
                      ["run", "--project", str(REPO), "afterwit", "serve-mcp"])
    assert "ok    registered repo path is current" in out


def test_packaged_install_has_no_repo_path_to_go_stale(tmp_path, monkeypatch, capsys):
    """A pip/uv-tool install registers the console script with no --project, so it
    carries no repo path at all — it is immune to a rename by construction, and the
    check must not invent a failure for it. This is WHY packaged installs are the
    rename-safe answer."""
    out = _run_doctor(tmp_path, monkeypatch, capsys, ["serve-mcp"])
    assert "registered repo path is current" not in out


def _codex_home(home, trusted=True):
    """A ~/.codex that install_codex would have produced."""
    d = home / ".codex"
    d.mkdir(parents=True, exist_ok=True)
    (d / "AGENTS.md").write_text(f"{install.MD_BEGIN}\nAW=\"x\"\n{install.MD_END}\n")
    body = [install.TOML_BEGIN, f"[mcp_servers.{install.MCP_NAME}]"]
    for event, mode in install._CODEX_HOOK_MODES:
        body += [f"[[hooks.{event}]]", "[[hooks.%s.hooks]]" % event, 'type = "command"',
                 f'command = "afterwit inject --mode {mode} --harness codex"']
    if trusted:
        body.append('trusted_hash = "sha256:x"')
    body.append(install.TOML_END)
    (d / "config.toml").write_text("\n".join(body) + "\n")
    return d


def _hooks(cfg, status):
    return [{"eventName": name, "sourcePath": str(cfg / "config.toml"),
             "trustStatus": status, "key": f"k{i}", "currentHash": "sha256:x"}
            for i, name in enumerate(("sessionStart", "userPromptSubmit"))]


def test_doctor_fails_when_codex_would_silently_skip_our_hooks(tmp_path, monkeypatch,
                                                               capsys):
    """Registered is not running. Codex skips an untrusted hook with no warning,
    which is indistinguishable from never having installed it (Gotcha #69) — the
    exact class of silence doctor exists to break."""
    d = _codex_home(tmp_path)
    monkeypatch.setattr(install, "_codex_hooks_list",
                        lambda cfg, cwd, run=None: _hooks(d, "modified"))
    out = _run_doctor(tmp_path, monkeypatch, capsys,
                      ["run", "--project", str(REPO), "afterwit", "serve-mcp"])
    assert "ok    codex prompt hook" in out          # registered, and it says so
    assert "FAIL  codex hooks trusted" in out        # but not running
    assert "sessionStart=modified" in out            # names which one and why


def test_doctor_passes_when_the_codex_hooks_are_trusted(tmp_path, monkeypatch, capsys):
    d = _codex_home(tmp_path)
    monkeypatch.setattr(install, "_codex_hooks_list",
                        lambda cfg, cwd, run=None: _hooks(d, "trusted"))
    out = _run_doctor(tmp_path, monkeypatch, capsys,
                      ["run", "--project", str(REPO), "afterwit", "serve-mcp"])
    assert "ok    codex hooks trusted" in out and "FAIL  codex" not in out
    # and the Codex command is SPAWNED, not just substring-matched. Its argv
    # differs from Claude's (`--harness codex`), so a green Claude spawn proves
    # nothing about it — shipping that flag unparsed exited 2 on every Codex
    # prompt while every config-reading check still said ok (Gotcha #71).
    assert "codex prompt hook actually runs" in out


def test_doctor_flags_a_codex_config_with_no_afterwit_hooks_at_all(tmp_path, monkeypatch,
                                                                   capsys):
    d = tmp_path / ".codex"
    d.mkdir(parents=True)
    (d / "config.toml").write_text(f"{install.TOML_BEGIN}\n[mcp_servers.afterwit]\n"
                                   f"{install.TOML_END}\n")
    (d / "AGENTS.md").write_text(f"{install.MD_BEGIN}\n{install.MD_END}\n")
    # hermetic: never let a doctor test spawn the real codex binary
    monkeypatch.setattr(install, "_codex_hooks_list", lambda cfg, cwd, run=None: None)
    out = _run_doctor(tmp_path, monkeypatch, capsys,
                      ["run", "--project", str(REPO), "afterwit", "serve-mcp"])
    assert "FAIL  codex session hook" in out and "FAIL  codex prompt hook" in out


def test_toml_str_unescapes_a_windows_path():
    """install writes hook commands with json.dumps, so a Windows path arrives
    escaped. `.strip('"')` left the backslashes DOUBLED and doctor spawned a
    string that was not the one on disk — in the check whose whole purpose is
    running exactly what is on disk (Gotcha #76)."""
    import json

    from afterwit import cli

    cmd = r"'C:\Users\E1\uv.EXE' run --project 'C:\Users\E1\OneDrive - Org (HO)\aw' afterwit"
    assert cli._toml_str(f"command = {json.dumps(cmd)}") == cmd
    assert cli._toml_str("command = ") == ""  # degrades, never raises
