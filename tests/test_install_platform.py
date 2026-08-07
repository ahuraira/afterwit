"""Cross-platform scheduler + shipped-mode entrypoint resolution.

Every branch runs on every host: `mode=` overrides the sys.platform pick, and
the subprocess side effects are injected. Nothing here touches the real
~/.claude, ~/Library/LaunchAgents, crontab, systemd or Task Scheduler.
"""
import plistlib
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from afterwit import install


class FakeRun:
    """Records argv; returns a queued (returncode, stdout[, stderr]) per call."""

    def __init__(self, *results):
        self.calls: list[list[str]] = []
        self.results = list(results)

    def __call__(self, argv):
        self.calls.append(argv)
        rc, out, *err = self.results.pop(0) if self.results else (0, "")
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr=err[0] if err else "")


def _exe(path: Path, name: str) -> Path:
    """A fake console script that shutil.which() will find on a faked PATH."""
    path.mkdir(parents=True, exist_ok=True)
    p = path / (f"{name}.exe" if sys.platform == "win32" else name)
    p.write_text("#!/bin/sh\n", encoding="utf-8")
    p.chmod(0o755)
    return p


# --- _server_argv priority ---------------------------------------------------

def test_server_argv_prefers_dev_checkout(tmp_path, monkeypatch):
    """A checkout with pyproject.toml + uv wins: the hook must run the working
    tree, not whatever `pip install afterwit` left on PATH."""
    bin_dir = tmp_path / "bin"
    uv = _exe(bin_dir, "uv")
    _exe(bin_dir, "afterwit")
    monkeypatch.setenv("PATH", str(bin_dir))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

    argv = install._server_argv("serve-mcp", repo)
    # Path, not str, for argv[0]: shutil.which builds its candidate from PATHEXT, so
    # on Windows it returns `uv.EXE` for a file named `uv.exe`. Path equality is
    # case-insensitive there and exact on POSIX.
    assert Path(argv[0]) == uv
    assert argv[1:] == ["run", "--no-sync", "--project", str(repo), "afterwit", "serve-mcp"]


def test_server_argv_falls_back_to_console_script(tmp_path, monkeypatch):
    """pip-installed: no checkout, so resolve the shipped console script."""
    bin_dir = tmp_path / "bin"
    _exe(bin_dir, "uv")  # uv present, but the repo is not a checkout
    exe = _exe(bin_dir, "afterwit")
    monkeypatch.setenv("PATH", str(bin_dir))

    argv = install._server_argv("inject", tmp_path / "not-a-repo")
    assert [Path(argv[0]), *argv[1:]] == [exe, "inject"]


def test_server_argv_prefers_afterwit_over_hh(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    afterwit = _exe(bin_dir, "afterwit")
    _exe(bin_dir, "afterwit")
    monkeypatch.setenv("PATH", str(bin_dir))

    assert Path(install._server_argv("run", tmp_path / "nope")[0]) == afterwit


def test_server_argv_falls_back_to_python_m_hh(tmp_path, monkeypatch):
    """Nothing on PATH at all (`pip install --target`, odd venv layouts)."""
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))

    assert install._server_argv("recall", tmp_path / "nope") == [
        sys.executable, "-m", "afterwit", "recall"]


def test_dunder_main_module_exists():
    """`python -m afterwit` is the last-resort entrypoint — it must import."""
    assert (Path(install.__file__).parent / "__main__.py").exists()


# --- systemd (Linux) ---------------------------------------------------------

def test_systemd_mode_writes_units_and_is_idempotent(tmp_path):
    sd = tmp_path / "systemd"
    kw = dict(systemd_dir=sd, repo=install._repo_root(), activate=False, mode="systemd")
    r1 = install.install_cron(**kw)
    assert r1["mode"] == "systemd"
    assert "afterwit run" in (sd / "afterwit.service").read_text()
    assert len(r1["changed"]) == 2

    assert install.install_cron(**kw)["changed"] == []


# --- launchd (macOS) ---------------------------------------------------------

def _launchd(tmp_path, run, activate=True):
    return install.install_cron(mode="launchd", repo=install._repo_root(),
                                plist_path=tmp_path / "dev.afterwit.nightly.plist",
                                activate=activate, run=run)


def test_launchd_writes_plist_and_bootstraps(tmp_path, monkeypatch):
    monkeypatch.setattr(install.os, "getuid", lambda: 501, raising=False)
    run = FakeRun((1, ""), (0, ""))  # bootout fails (not loaded yet), bootstrap ok
    r = _launchd(tmp_path, run)

    plist_path = tmp_path / "dev.afterwit.nightly.plist"
    assert r["mode"] == "launchd" and r["note"] == "bootstrapped"
    assert r["changed"] == [str(plist_path)]

    plist = plistlib.loads(plist_path.read_bytes())
    assert plist["Label"] == "dev.afterwit.nightly"
    assert plist["StartCalendarInterval"] == {"Hour": 2, "Minute": 30}
    assert plist["RunAtLoad"] is False
    assert plist["ProgramArguments"][-4:] == ["--budget", "30", "--timeout", "50"]

    assert run.calls[0] == ["launchctl", "bootout", "gui/501/dev.afterwit.nightly"]
    assert run.calls[1] == ["launchctl", "bootstrap", "gui/501", str(plist_path)]


def test_launchd_idempotent_second_call_does_not_touch_launchctl(tmp_path, monkeypatch):
    monkeypatch.setattr(install.os, "getuid", lambda: 501, raising=False)
    _launchd(tmp_path, FakeRun((0, ""), (0, "")))

    run2 = FakeRun()
    r2 = _launchd(tmp_path, run2)
    assert r2["changed"] == [] and r2["backed_up"] == [] and r2["note"] == "unchanged"
    assert run2.calls == []


def test_launchd_bootstrap_failure_is_reported_not_raised(tmp_path, monkeypatch):
    monkeypatch.setattr(install.os, "getuid", lambda: 501, raising=False)
    r = _launchd(tmp_path, FakeRun((1, ""), (5, "")))
    assert "not loaded (exit 5)" in r["note"]


def test_macos_never_picks_crontab(monkeypatch):
    """cron on macOS needs Full Disk Access and fails silently — never pick it."""
    monkeypatch.setattr(install.sys, "platform", "darwin")
    assert install._default_mode() == "launchd"


# --- schtasks (Windows) ------------------------------------------------------

_TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Actions><Exec><Command>{cmd}</Command><Arguments>{args}</Arguments></Exec></Actions>
</Task>"""


def test_schtasks_creates_task(tmp_path):
    run = FakeRun((1, ""), (0, ""))  # /Query: no such task, then /Create ok
    r = install.install_cron(mode="schtasks", repo=install._repo_root(), run=run)

    assert r["mode"] == "schtasks" and r["changed"] == ["schtasks:afterwit-nightly"]
    create = run.calls[1]
    assert create[:5] == ["schtasks", "/Create", "/TN", "afterwit-nightly", "/TR"]
    assert create[6:] == ["/SC", "DAILY", "/ST", "02:30", "/F"]


def test_schtasks_tr_quotes_a_path_containing_a_space(tmp_path, monkeypatch):
    """`C:\\Program Files\\...\\afterwit.exe` must reach /TR as ONE quoted token."""
    exe = r"C:\Program Files\afterwit\afterwit.exe"
    monkeypatch.setattr(install, "_run_argv", lambda *a: [exe, "run", "--budget", "30"])
    run = FakeRun((1, ""), (0, ""))
    install.install_cron(mode="schtasks", repo=tmp_path, run=run)

    tr = run.calls[1][5]
    assert tr == f'"{exe}" run --budget 30'
    assert subprocess.list2cmdline([exe, "run"]).startswith(f'"{exe}"')


def test_hook_command_survives_bash_on_windows():
    """Claude Code hands a hook command to BASH on every platform, Windows included.

    Quoted for cmd.exe, `C:\\Users\\...\\uv.EXE` reaches bash with its backslashes read
    as escapes — `C:UsersE112323scoopshimsuv.EXE: command not found`, on every prompt.
    shlex.split is how bash splits it, so it is the check that catches the regression.
    """
    argv = [r"C:\Users\E1\scoop\shims\uv.EXE", "run", "--no-sync", "--project",
            r"C:\Users\E1\OneDrive - Org (Head Office)\Projects\afterwit",
            "afterwit", "inject", "--mode", "prompt"]
    assert shlex.split(install._join(argv)) == argv


def test_schtasks_idempotent_when_task_already_matches(tmp_path):
    argv = install._run_argv(install._repo_root(), 30, 50)
    tr = subprocess.list2cmdline(argv)
    cmd, args = subprocess.list2cmdline(argv[:1]), subprocess.list2cmdline(argv[1:])
    xml = _TASK_XML.format(cmd=cmd, args=args)
    assert install._schtasks_command(xml) == tr  # the parse round-trips

    run = FakeRun((0, xml))
    r = install.install_cron(mode="schtasks", repo=install._repo_root(), run=run)
    assert r["changed"] == [] and r["note"] == "unchanged"
    assert len(run.calls) == 1  # queried, never created


def test_schtasks_create_failure_is_reported(tmp_path):
    run = FakeRun((1, ""), (1, "", "Access is denied."))
    r = install.install_cron(mode="schtasks", repo=tmp_path, run=run)
    assert r["changed"] == [] and "Access is denied." in r["note"]


def test_schtasks_command_survives_garbage_xml():
    assert install._schtasks_command("not xml at all") is None


# --- mode selection ----------------------------------------------------------

@pytest.mark.parametrize("platform,expected", [("win32", "schtasks"), ("darwin", "launchd")])
def test_default_mode_from_platform(monkeypatch, platform, expected):
    monkeypatch.setattr(install.sys, "platform", platform)
    assert install._default_mode() == expected


def test_default_mode_linux_without_systemctl(monkeypatch):
    monkeypatch.setattr(install.sys, "platform", "linux")
    monkeypatch.setattr(install.shutil, "which", lambda _n: None)
    assert install._default_mode() == "cron"


def test_unknown_mode_raises(tmp_path):
    with pytest.raises(ValueError, match="unknown scheduler mode"):
        install.install_cron(mode="upstart", repo=tmp_path, activate=False)


def test_user_token_survives_a_platform_without_getuid(monkeypatch):
    """`os.getuid` is POSIX-only and index_db called it at MODULE level, so
    `import afterwit.index_db` — the first thing every entry point does —
    raised AttributeError on Windows. CI never caught it because collection
    died before any test ran."""
    from afterwit import index_db

    monkeypatch.delattr(index_db.os, "getuid", raising=False)
    monkeypatch.setenv("USERNAME", "Alice B")
    assert index_db._user_token() == "AliceB"      # path-safe, per-user
    monkeypatch.delenv("USERNAME")
    assert index_db._user_token() == "user"        # never empty, never raises


# --- run_time threads into every scheduler (ADR-046) -------------------------

def test_run_time_reaches_all_four_schedulers(tmp_path, monkeypatch):
    """One `at` value, four scheduler dialects. Paired against the 02:30
    defaults asserted above — a branch that silently keeps its hardcoded time
    fails here, not at 04:05 in production."""
    at = "04:05"
    sd = tmp_path / "systemd"
    install.install_cron(systemd_dir=sd, repo=install._repo_root(),
                         activate=False, mode="systemd", at=at)
    assert "OnCalendar=*-*-* 04:05:00" in (sd / "afterwit.timer").read_text()

    monkeypatch.setattr(install.os, "getuid", lambda: 501, raising=False)
    install.install_cron(mode="launchd", repo=install._repo_root(),
                         plist_path=tmp_path / "p.plist", activate=False,
                         run=FakeRun(), at=at)
    plist = plistlib.loads((tmp_path / "p.plist").read_bytes())
    assert plist["StartCalendarInterval"] == {"Hour": 4, "Minute": 5}

    run = FakeRun((1, ""), (0, ""))
    install.install_cron(mode="schtasks", repo=install._repo_root(), run=run, at=at)
    assert run.calls[1][-3:] == ["/ST", "04:05", "/F"]

    tab = {"text": ""}
    install.install_cron(mode="cron", repo=install._repo_root(), at=at,
                         crontab_get=lambda: tab["text"],
                         crontab_set=lambda t: tab.__setitem__("text", t))
    assert "5 4 * * *" in tab["text"]


def test_bad_run_time_is_rejected_before_any_write(tmp_path):
    for bad in ("24:00", "2:30", "02:60", "noon", ""):
        with pytest.raises(ValueError):
            install.install_cron(systemd_dir=tmp_path / "sd", activate=False,
                                 repo=install._repo_root(), mode="systemd", at=bad)
    assert not (tmp_path / "sd").exists()  # rejected BEFORE the unit was written


def test_schtasks_recreates_when_only_the_time_changed(tmp_path):
    """The old command-only idempotence reported 'unchanged' for a pure time
    change; a StartBoundary that differs from `at` must recreate the task."""
    argv = install._run_argv(install._repo_root(), 30, 50)
    cmd, args = subprocess.list2cmdline(argv[:1]), subprocess.list2cmdline(argv[1:])
    xml = _TASK_XML_WITH_START.format(cmd=cmd, args=args, start="2026-01-01T02:30:00")

    run = FakeRun((0, xml), (0, ""))
    r = install.install_cron(mode="schtasks", repo=install._repo_root(), run=run,
                             at="04:05")
    assert r["changed"] == ["schtasks:afterwit-nightly"]
    assert run.calls[1][-3:] == ["/ST", "04:05", "/F"]

    # and same time -> still idempotent
    run2 = FakeRun((0, _TASK_XML_WITH_START.format(cmd=cmd, args=args,
                                                   start="2026-01-01T04:05:00")))
    r2 = install.install_cron(mode="schtasks", repo=install._repo_root(), run=run2,
                              at="04:05")
    assert r2["note"] == "unchanged" and len(run2.calls) == 1


_TASK_XML_WITH_START = """<?xml version="1.0" encoding="UTF-16"?>
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers><CalendarTrigger><StartBoundary>{start}</StartBoundary></CalendarTrigger></Triggers>
  <Actions><Exec><Command>{cmd}</Command><Arguments>{args}</Arguments></Exec></Actions>
</Task>"""


def test_cron_scheduled_detects_each_mode(tmp_path):
    home = tmp_path
    assert install.cron_scheduled(mode="systemd", home=home) is False
    unit = home / ".config" / "systemd" / "user" / "afterwit.timer"
    unit.parent.mkdir(parents=True)
    unit.write_text("x")
    assert install.cron_scheduled(mode="systemd", home=home) is True

    assert install.cron_scheduled(mode="launchd", home=home) is False
    p = install._plist_path(home)
    p.parent.mkdir(parents=True)
    p.write_text("x")
    assert install.cron_scheduled(mode="launchd", home=home) is True

    assert install.cron_scheduled(mode="schtasks", run=FakeRun((1, ""))) is False
    assert install.cron_scheduled(mode="schtasks", run=FakeRun((0, ""))) is True

    assert install.cron_scheduled(mode="cron", crontab_get=lambda: "") is False
    assert install.cron_scheduled(
        mode="cron", crontab_get=lambda: "# afterwit:begin\nx\n# afterwit:end") is True


def test_every_subprocess_call_closes_stdin():
    """No afterwit subprocess may inherit its parent's stdin.

    afterwit runs inside harnesses that hand it a PIPE on stdin: the MCP server
    speaks JSON-RPC over it, hooks receive their payload on it, and an agent
    shelling out to `aw` gets one too. A child that inherits that pipe leaves
    `communicate()` waiting for an EOF that only arrives when the *harness*
    exits, so the reader threads park and the call burns its whole timeout.

    On Windows `timeout=` is no defence: after TimeoutExpired, CPython's
    `run()` calls `process.communicate()` a second time with NO timeout to
    collect the output (subprocess.py, `if _mswindows:`). That second wait is
    unbounded — which is how one `git rev-parse` behind a 10s timeout became
    30 minutes of MCP silence with no error and no response (ADR gotcha #86).

    `input=` is exempt: it implies stdin=PIPE, and passing both raises.
    """
    import ast

    offenders = []
    for path in sorted(Path(install.__file__).parent.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if getattr(getattr(fn, "value", None), "id", None) != "subprocess":
                continue
            if getattr(fn, "attr", None) not in {"run", "Popen", "check_output", "call"}:
                continue
            kwargs = {k.arg for k in node.keywords}
            if not kwargs & {"stdin", "input"}:
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, f"subprocess call inherits the harness pipe: {offenders}"
