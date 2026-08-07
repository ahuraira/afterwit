"""`afterwit install claude|codex`. SPEC §9.2/§9.3, ADR-003, Gotchas #4/#5.

Registers the MCP server + bounded SessionStart/UserPromptSubmit hooks so both
harnesses can reach the knowledge base and usage learning has real servings.

Non-negotiables:
- timestamped backup of every file before the first write that changes it;
- idempotent — a second run changes nothing (no write, no new backup);
- fence-only edits to text files: bytes outside `<!-- afterwit:begin -->…<!-- afterwit:end -->`
  (or the `# afterwit:begin`/`# afterwit:end` TOML variant) never change.

Every mutating function takes explicit target paths so tests redirect to tmp
copies — this module must never be pointed at the real ~/.claude or ~/.codex
in a test.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import threading
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MCP_NAME = "afterwit"
_LEGACY_MCP_NAME = "harness_helper"  # pre-rename server id; removed on reinstall
CRON_UNIT = "afterwit"
LAUNCHD_LABEL = "dev.afterwit.nightly"
SCHTASKS_NAME = "afterwit-nightly"
MD_BEGIN, MD_END = "<!-- afterwit:begin -->", "<!-- afterwit:end -->"
TOML_BEGIN, TOML_END = "# afterwit:begin", "# afterwit:end"
# A pre-rename install fenced with these; strip them on write so reinstall does
# not leave an orphaned block that still calls the removed `hh` command.
_LEGACY_MD = ("<!-- hh:begin -->", "<!-- hh:end -->")
_LEGACY_TOML = ("# hh:begin", "# hh:end")

Runner = Callable[[list[str]], Any]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _server_argv(subcmd: str, repo: Path) -> list[str]:
    """How to invoke `afterwit <subcmd>` on THIS machine, from any cwd.

    A git checkout keeps the `uv run --project` form so the hook runs the
    working tree rather than whatever is installed. A `pip install afterwit`
    user has no checkout, so we resolve the console script, and fall back to
    `python -m afterwit` when even that is missing (e.g. `pip install --target`).
    """
    uv = shutil.which("uv")
    if uv and (repo / "pyproject.toml").exists():
        # Hooks/MCP run in restricted, often offline environments. The checkout
        # was installed during setup; never let an ordinary recall trigger a
        # dependency sync or registry lookup.
        return [uv, "run", "--no-sync", "--project", str(repo), "afterwit", subcmd]
    for name in ("afterwit", "aw"):
        exe = shutil.which(name)
        if exe:
            return [exe, subcmd]
    return [sys.executable, "-m", "afterwit", subcmd]


def _skillify(text: str, repo: Path) -> str:
    """Rewrite the shipped `AW="aw"` placeholder to the invocation that actually
    works on this machine.

    It bit (2026-07-27): the checkout lived under `.../OneDrive - Org (Head Office)/`
    and every documented `$AW recall ...` fallback died with `syntax error near
    unexpected token ('`. Quoting the prefix is NOT enough — bash word-splits an
    expanded `$AW` but does not re-parse quotes, so `'C:\\Program Files\\uv.EXE'`
    would be looked up with the quote marks still attached. Bind a function instead:
    the quoting then lives inside the body, and every `$AW ...` call site in every
    skill keeps working untouched.
    """
    argv = _server_argv("", repo)[:-1]
    if len(argv) == 1 and shlex.quote(argv[0]) == argv[0]:
        return text.replace('AW="aw"', f'AW="{argv[0]}"')  # a bare `aw`, nothing to quote
    return text.replace('AW="aw"', f'aw() {{ {_join(argv)} "$@"; }}; AW=aw')


def _join(argv: list[str]) -> str:
    """Shell-quote argv for the shell that will re-split it — bash, on EVERY platform.

    Claude Code runs hook commands through bash even on Windows (git bash). The old
    win32 branch used `subprocess.list2cmdline`, which quotes only for cmd.exe: it
    leaves `C:\\Users\\...\\uv.EXE` unquoted, bash reads every backslash as an escape,
    and the hook dies with `C:UsersE112323scoopshimsuv.EXE: command not found` on every
    prompt. shlex.join single-quotes anything containing a backslash, space or paren,
    and inside bash single quotes a backslash is literal — so the Windows path arrives
    intact. Found live on a Windows machine 2026-07-27; doctor called the hook "ok"
    throughout, because it checked that the entry existed, not that it ran.
    """
    return shlex.join(argv)


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    dst = path.with_name(f"{path.name}.afterwit-bak-{ts}")
    shutil.copy2(path, dst)
    return dst


def _write_if_changed(path: Path, content: str, changed: list, backed: list) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False  # idempotent no-op
    b = _backup(path)
    if b:
        backed.append(str(b))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    changed.append(str(path))
    return True


def _strip_legacy_fence(text: str, begin: str, end: str) -> str:
    """Remove a legacy managed block (and the blank lines around it) if present."""
    if begin in text and end in text:
        i, j = text.index(begin), text.index(end) + len(end)
        text = text[:i].rstrip("\n") + ("\n" if text[:i].strip() else "") + text[j:].lstrip("\n")
    return text


def _apply_fence(text: str, block: str, begin: str, end: str) -> str:
    """Insert/replace the fenced region, leaving every byte outside it intact.
    A legacy `hh:*` block is removed first so the two never coexist."""
    legacy = _LEGACY_TOML if begin == TOML_BEGIN else _LEGACY_MD
    text = _strip_legacy_fence(text, *legacy)
    region = f"{begin}\n{block}\n{end}"
    if begin in text and end in text:
        i = text.index(begin)
        j = text.index(end) + len(end)
        return text[:i] + region + text[j:]
    if not text:
        return region + "\n"
    sep = "" if text.endswith("\n") else "\n"
    return f"{text}{sep}\n{region}\n"


def _xml_block(md_text: str) -> str:
    """The operator payload of codex-aw.md is its fenced ```xml block."""
    m = re.search(r"```xml\n(.*?)\n```", md_text, re.DOTALL)
    return m.group(1).strip() if m else md_text.strip()


# ------------------------------------------------------------------- claude

def install_claude(*, settings_path: Path | None = None,
                   mcp_config_path: Path | None = None,
                   skills_dir: Path | None = None,
                   repo: Path | None = None) -> dict:
    """Hook → ~/.claude/settings.json; MCP server → ~/.claude.json (user scope,
    where `claude mcp add -s user` stores it); skills → ~/.claude/skills/.

    Paths come from `harness`, which honours CLAUDE_CONFIG_DIR — installing into
    ~/.claude while the user's Claude Code reads somewhere else writes files
    nothing will ever load."""
    from . import harness

    settings_path = settings_path or harness.settings_path("claude")
    mcp_config_path = mcp_config_path or harness.claude_json_path()
    skills_dir = skills_dir or harness.skills_dir("claude")
    repo = repo or _repo_root()
    changed: list[str] = []
    backed: list[str] = []

    # 1. Bounded hooks. The prompt hook is threshold-gated by inject.py and emits
    # nothing in the common case; without it usage learning has no servings.
    settings = _load_json(settings_path)
    hooks = settings.setdefault("hooks", {})
    _set_hook(hooks.setdefault("SessionStart", []),
              _join(_server_argv("inject", repo) + ["--mode", "session"]), "session")
    _set_hook(hooks.setdefault("UserPromptSubmit", []),
              _join(_server_argv("inject", repo) + ["--mode", "prompt"]), "prompt")
    # PostToolUseFailure, NOT PostToolUse: a non-zero Bash exit throws inside the
    # CLI's tool-dispatch try/catch, and the catch path dispatches only the
    # Failure event. A hook registered on PostToolUse would never once fire on a
    # failure — the exact silence this hook exists to break (ADR-038).
    _set_hook(hooks.setdefault("PostToolUseFailure", []),
              _join(_server_argv("inject", repo) + ["--mode", "error"]), "error",
              matcher="Bash")
    _write_if_changed(settings_path, _dump_json(settings), changed, backed)

    # 2. MCP server registration (user scope)
    mcp_cfg = _load_json(mcp_config_path)
    servers = mcp_cfg.setdefault("mcpServers", {})
    # Drop the pre-rename entry: it invokes the removed `hh serve-mcp` and would
    # spawn-fail on every session start alongside the working `afterwit` one.
    servers.pop(_LEGACY_MCP_NAME, None)
    argv = _server_argv("serve-mcp", repo)
    desired = {"type": "stdio", "command": argv[0], "args": argv[1:]}
    if servers.get(MCP_NAME) != desired:
        servers[MCP_NAME] = desired
    _write_if_changed(mcp_config_path, _dump_json(mcp_cfg), changed, backed)

    # 3. skills — all of them (aw-survey was previously dropped)
    for name in ("aw-knowledge", "aw-sweep", "aw-survey"):
        src = repo / "prompts" / "skills" / name
        if src.is_dir():
            _copy_tree_if_changed(src, skills_dir / name, changed, backed, repo)

    return {"changed": changed, "backed_up": backed}


def _is_afterwit_inject(command: str, mode: str) -> bool:
    """Ours, for this mode — regardless of how the argv happened to be spelled,
    INCLUDING the pre-rename `hh inject` (ADR-037).

    The MCP path already drops its legacy twin (`_LEGACY_MCP_NAME`, above) for
    exactly this reason. Hooks did not, and `"afterwit" in command` is false for
    `... hh inject --mode session` — so anyone who installed before the rename
    kept a hook invoking a binary that no longer exists. It fails to spawn on
    every single session start, and no amount of reinstalling ever cleared it,
    because the dedupe could not see it as ours. Found live on the author's
    machine 2026-07-27, ~2 weeks after the rename.

    Matched as `" hh inject"` with the leading space, not a bare `"hh"`, so a
    project path that happens to contain those letters cannot false-positive
    and delete a neighbour's hook.
    """
    if f"--mode {mode}" not in command:
        return False
    return ("afterwit" in command and " inject" in command) or " hh inject" in command


def _set_hook(entries: list, command: str, mode: str, matcher: str | None = None) -> None:
    """Make afterwit's hook for `mode` the ONLY afterwit hook for that mode.

    Presence used to be tested by exact string equality, so a changed command (the repo
    moved, or `--no-sync` was added) matched nothing and appended a SECOND hook. Both
    then fired on every prompt: double the injected tokens, and double the latency of a
    path speced at p95 < 200ms. The MCP entry is keyed by name and converges on
    reinstall; hooks had no key, so identify them by what they ARE (ADR-032).

    Only afterwit's own hooks are touched — the user's other hooks on the same event
    are somebody else's business and must survive untouched.
    """
    for entry in list(entries):
        kept = [h for h in entry.get("hooks", [])
                if not _is_afterwit_inject(str(h.get("command", "")), mode)]
        if kept != entry.get("hooks", []):
            entry["hooks"] = kept
            if not kept:
                entries.remove(entry)          # drop the husk, not the neighbours
    fresh: dict = {"hooks": [{"type": "command", "command": command}]}
    if matcher is not None:
        # Matched against `tool_name`, case-sensitively. Only the error hook needs
        # one — SessionStart/UserPromptSubmit carry no tool to match against.
        fresh["matcher"] = matcher
    entries.append(fresh)


def _copy_tree_if_changed(src: Path, dst: Path, changed: list, backed: list,
                          repo: Path) -> None:
    for f in sorted(src.rglob("*")):
        if f.is_file():
            _write_if_changed(dst / f.relative_to(src),
                              _skillify(f.read_text(encoding="utf-8"), repo),
                              changed, backed)


# -------------------------------------------------------------------- codex

# Codex event name -> our `--mode`. Only the two push surfaces: its PostToolUse
# fires on success AND failure with structurally identical payloads (`tool_response`
# is a bare string with no exit code), so the Claude error hook has nothing to gate
# on here and an ungated port would fire on every shell call (ADR-040).
_CODEX_HOOK_MODES = (("SessionStart", "session"), ("UserPromptSubmit", "prompt"))
_CODEX_EVENT_MODE = {"sessionStart": "session", "userPromptSubmit": "prompt"}


def _codex_hook_block(repo: Path) -> str:
    """The `[[hooks.<Event>]]` groups, in Codex's TOML transcription of the Claude
    hook shape. Handler `type = "command"`; the group-level `matcher` is omitted
    because neither event carries a tool to match on."""
    out = []
    for event, mode in _CODEX_HOOK_MODES:
        cmd = _join(_server_argv("inject", repo) + ["--mode", mode, "--harness", "codex"])
        out.append(f"[[hooks.{event}]]\n[[hooks.{event}.hooks]]\n"
                   f"type = \"command\"\ncommand = {json.dumps(cmd)}")
    return "\n\n".join(out)


def _rpc_stdout(argv: list[str], reqs: str, cwd: str, timeout: float = 60.0) -> str:
    """Speak JSON-RPC to a stdio server and return stdout up to the reply we want.

    The pipe is held OPEN while reading. `subprocess.run(input=...)` closes stdin
    the moment the last byte is written, and `codex app-server` shuts down on that
    EOF while `hooks/list` is still queued: it answers `initialize`, emits one
    notification, and exits 0 with the request unanswered. Install then reported
    "hooks are UNTRUSTED" on a machine where codex was working fine.
    """
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True,
                            # cp1252 has undefined bytes; a non-ASCII path in a
                            # hooks/list reply would raise mid-read and be swallowed
                            # as "no answer" — reported as UNTRUSTED (Gotcha #75).
                            encoding="utf-8", errors="replace", cwd=cwd)
    killer = threading.Timer(timeout, proc.kill)  # readline() can block forever
    killer.start()
    out: list[str] = []
    try:
        assert proc.stdin and proc.stdout
        proc.stdin.write(reqs)
        proc.stdin.flush()
        for line in proc.stdout:
            out.append(line)
            try:
                if json.loads(line).get("id") == 2:
                    break
            except json.JSONDecodeError:
                continue
    except Exception:  # noqa: BLE001 — a broken pipe just means "no answer"
        pass
    finally:
        killer.cancel()
        proc.kill()
        proc.wait(timeout=5)
    return "".join(out)


def _codex_hooks_list(config_path: Path, cwd: Path,
                      run: Callable[..., Any] | None = None) -> list[dict] | None:
    """Ask the Codex binary which hooks it actually discovered, and their trust
    state. `codex app-server` speaks JSON-RPC over stdio; `hooks/list` returns
    each hook's `key`, `currentHash` and `trustStatus`.

    Both are read, never computed: the key embeds the hook's index within its
    event array (so a user's own group ahead of ours shifts it) and the hash is
    over Codex's internal handler struct. Returns None if Codex is absent or the
    call fails — install then reports the hooks as untrusted rather than guessing.
    """
    # `run` IS the transport. Gating on the binary before consulting it made every
    # injected-run test bypass this whole function on any machine without codex
    # installed — locally green because the author had codex, two failures and
    # three silently vacuous passes on all six CI jobs (Gotcha #74).
    exe = shutil.which("codex")
    if run is None and exe is None:
        return None
    reqs = "".join(json.dumps(m) + "\n" for m in (
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"clientInfo": {"name": MCP_NAME, "version": "1"}}},
        {"jsonrpc": "2.0", "method": "initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "hooks/list",
         "params": {"cwds": [str(cwd)]}},
    ))
    try:
        stdout = (run or _rpc_stdout)([exe or "codex", "app-server"], reqs, str(cwd))
    except Exception:  # noqa: BLE001 — install must survive any codex-side failure
        return None
    for line in (stdout or "").splitlines():
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("id") == 2 and isinstance(msg.get("result"), dict):
            entries = msg["result"].get("data") or []
            return [h for e in entries for h in (e.get("hooks") or [])]
    return None


def _codex_trust_block(hooks: list[dict], config_path: Path) -> str:
    """Trust entries for OUR hooks in OUR file, and nothing else.

    Codex discovers hooks but silently skips any that are not trusted — no
    warning, no diagnostic, indistinguishable from a config that failed to parse.
    Both filters below are load-bearing: `sourcePath` keeps us from ever trusting
    a hook defined in someone else's config layer (a plugin, a project file), and
    `_is_afterwit_inject` keeps us from trusting a neighbour's hook that merely
    shares our file.
    """
    out = []
    for h in sorted(hooks, key=lambda x: str(x.get("key") or "")):
        mode = _CODEX_EVENT_MODE.get(str(h.get("eventName") or ""))
        key, chash = h.get("key"), h.get("currentHash")
        if not (mode and key and chash) or h.get("sourcePath") != str(config_path):
            continue
        if not _is_afterwit_inject(str(h.get("command") or ""), mode):
            continue
        out.append(f"[hooks.state.{json.dumps(key)}]\n"
                   f"enabled = true\ntrusted_hash = {json.dumps(chash)}")
    return "\n\n".join(out)


def _carried_trust(text: str) -> str:
    """The trust entries already in our fence.

    Pass 1 below rewrites the whole fenced region before pass 2 can ask Codex for
    fresh hashes. Without carrying the old entries through, that first write drops
    them and the second puts them back — a file that changes on every single
    install, so `install --codex` would never once report "already up to date",
    and every run would leave another backup behind. Scoped to the fence so a
    hand-written `[hooks.state.…]` elsewhere in the user's config is not adopted.
    """
    if TOML_BEGIN not in text or TOML_END not in text:
        return ""
    region = text[text.index(TOML_BEGIN) + len(TOML_BEGIN):text.index(TOML_END)]
    i = region.find("[hooks.state.")
    return region[i:].strip() if i != -1 else ""


def install_codex(*, config_path: Path | None = None,
                  agents_path: Path | None = None,
                  codex_hh_path: Path | None = None,
                  repo: Path | None = None,
                  run: Callable[..., Any] | None = None) -> dict:
    """MCP entry + SessionStart/UserPromptSubmit hooks → ~/.codex/config.toml
    (fenced); operator block → ~/.codex/AGENTS.md (fenced).

    Supersedes ADR-003's "Codex = session-start doc + MCP-pull". That held while
    Codex had no hook system; codex-cli 0.145.0 ships `hooks` as a stable, on-by-
    default feature whose payload keys are identical to Claude Code's, verified
    end-to-end 2026-07-27 (ADR-040).

    Paths come from `harness`, which honours CODEX_HOME."""
    from . import harness

    config_path = config_path or harness.settings_path("codex")
    agents_path = agents_path or harness.agents_path("codex")
    codex_hh_path = codex_hh_path or (repo or _repo_root()) / "prompts" / "skills" / "codex-aw.md"
    repo = repo or _repo_root()
    changed: list[str] = []
    backed: list[str] = []
    notes: list[str] = []

    argv = _server_argv("serve-mcp", repo)
    # json.dumps == TOML basic string: escapes the backslashes in Windows paths.
    args = ", ".join(json.dumps(a) for a in argv[1:])
    toml_block = (f"[mcp_servers.{MCP_NAME}]\n"
                  f"command = {json.dumps(argv[0])}\n"
                  f"args = [{args}]\n\n" + _codex_hook_block(repo))
    existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    carried = _carried_trust(existing)
    _write_if_changed(config_path,
                      _apply_fence(existing, toml_block + (f"\n\n{carried}" if carried else ""),
                                   TOML_BEGIN, TOML_END),
                      changed, backed)

    # Trust is a second pass by necessity: hooks/list reads the file we just
    # wrote. Rewriting a hook changes its hash and silently un-trusts it, so this
    # re-reads and re-writes on every install instead of only the first.
    hooks = _codex_hooks_list(config_path, config_path.parent, run=run)
    trust = _codex_trust_block(hooks, config_path) if hooks is not None else ""
    if trust:
        _write_if_changed(config_path,
                          _apply_fence(config_path.read_text(encoding="utf-8"),
                                       toml_block + "\n\n" + trust, TOML_BEGIN, TOML_END),
                          changed, backed)
    else:
        notes.append("codex hooks are installed but UNTRUSTED — Codex skips untrusted "
                     "hooks silently. Trust them in the Codex TUI hooks panel, or "
                     "re-run this after `codex` is on PATH.")

    block = _skillify(_xml_block(codex_hh_path.read_text(encoding="utf-8")), repo) if codex_hh_path.exists() else ""
    if block:
        existing_md = agents_path.read_text(encoding="utf-8") if agents_path.exists() else ""
        _write_if_changed(agents_path, _apply_fence(existing_md, block, MD_BEGIN, MD_END),
                          changed, backed)

    return {"changed": changed, "backed_up": backed, "note": "\n  ".join(notes)}


# --------------------------------------------------------------------- cron

def _parse_time(at: str) -> tuple[int, int]:
    """24h HH:MM → (hour, minute). One validator shared by config.coerce and
    every scheduler branch, so a time that passes Settings cannot fail here."""
    m = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", at.strip())
    if not m:
        raise ValueError(f"run time must be HH:MM 24h (e.g. 02:30), got {at!r}")
    return int(m.group(1)), int(m.group(2))


def _run_argv(repo: Path, budget: int, timeout: int) -> list[str]:
    """The nightly command. Absolute paths — a scheduler's env has a thin PATH."""
    return _server_argv("run", repo) + ["--budget", str(budget), "--timeout", str(timeout)]


def _run_execstart(repo: Path, budget: int, timeout: int) -> str:
    return _join(_run_argv(repo, budget, timeout))


def _default_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL)


def _systemctl_activate() -> str:
    """Reload + enable the timer. Fail-soft: a headless box without a user D-Bus
    session cannot enable --user units; report instead of crashing the install."""
    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True,
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
        subprocess.run(["systemctl", "--user", "enable", "--now", f"{CRON_UNIT}.timer"],
                       check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        return "enabled"
    except Exception as e:  # noqa: BLE001
        return (f"units written but not enabled ({e}); run manually: "
                f"systemctl --user daemon-reload && "
                f"systemctl --user enable --now {CRON_UNIT}.timer")


def _default_mode() -> str:
    if sys.platform == "win32":
        return "schtasks"
    if sys.platform == "darwin":
        return "launchd"  # NOT crontab: on macOS cron needs Full Disk Access
    return "systemd" if shutil.which("systemctl") else "cron"


def install_cron(*, systemd_dir: Path | None = None, repo: Path | None = None,
                 budget: int = 30, timeout: int = 50, activate: bool = True,
                 use_systemd: bool | None = None, mode: str | None = None,
                 plist_path: Path | None = None, run: Runner | None = None,
                 crontab_get=None, crontab_set=None, at: str = "02:30") -> dict:
    """Schedule the nightly `afterwit run` (ADR-015 §4) on Linux, macOS or Windows.

    Mode comes from `sys.platform`; pass `mode=` to exercise any branch anywhere.
    Backup-first and idempotent, same discipline as the harness installers.
    `activate` runs the side effect (systemctl/crontab/launchctl/schtasks write);
    tests pass activate=False or inject `run`.
    """
    home = Path.home()
    repo = repo or _repo_root()
    if mode is None:
        mode = ("systemd" if use_systemd else "cron") if use_systemd is not None \
            else _default_mode()
    changed: list[str] = []
    backed: list[str] = []
    hour, minute = _parse_time(at)
    argv = _run_argv(repo, budget, timeout)

    if mode == "systemd":
        systemd_dir = systemd_dir or home / ".config" / "systemd" / "user"
        service = ("[Unit]\n"
                   "Description=afterwit nightly knowledge run\n\n"
                   "[Service]\n"
                   "Type=oneshot\n"
                   f"ExecStart={_join(argv)}\n")
        timer = ("[Unit]\n"
                 "Description=afterwit nightly timer\n\n"
                 "[Timer]\n"
                 f"OnCalendar=*-*-* {hour:02d}:{minute:02d}:00\n"
                 "Persistent=true\n\n"
                 "[Install]\n"
                 "WantedBy=timers.target\n")
        _write_if_changed(systemd_dir / f"{CRON_UNIT}.service", service, changed, backed)
        _write_if_changed(systemd_dir / f"{CRON_UNIT}.timer", timer, changed, backed)
        note = _systemctl_activate() if (activate and changed) else "unchanged"
        return {"changed": changed, "backed_up": backed, "mode": "systemd", "note": note}

    if mode == "launchd":
        return _install_launchd(argv, plist_path or _plist_path(home),
                                activate, run or _default_run, changed, backed,
                                hour, minute)

    if mode == "schtasks":
        return _install_schtasks(argv, activate, run or _default_run, changed, backed,
                                 f"{hour:02d}:{minute:02d}")

    if mode != "cron":
        raise ValueError(f"unknown scheduler mode: {mode}")

    # crontab fallback: one managed line inside `# afterwit:begin`/`# afterwit:end`.
    get = crontab_get or _crontab_get
    setter = crontab_set or _crontab_set
    current = get()
    cron_line = f"{minute} {hour} * * * {_join(argv)}"
    updated = _apply_fence(current, cron_line, TOML_BEGIN, TOML_END)
    if updated != current:
        if activate:
            setter(updated)
        changed.append("crontab")
    return {"changed": changed, "backed_up": backed, "mode": "cron", "note": "crontab"}


def cron_scheduled(*, mode: str | None = None, home: Path | None = None,
                   run: Runner | None = None, crontab_get=None) -> bool:
    """Is the nightly already scheduled on THIS machine? Detection only.

    The Settings save path uses this so a `run_time` change reschedules an
    existing timer — and never installs one for a user who chose not to
    schedule. Best-effort: unknown mode reads as not scheduled."""
    home = home or Path.home()
    mode = mode or _default_mode()
    if mode == "systemd":
        return (home / ".config" / "systemd" / "user" / f"{CRON_UNIT}.timer").exists()
    if mode == "launchd":
        return _plist_path(home).exists()
    if mode == "schtasks":
        r = (run or _default_run)(["schtasks", "/Query", "/TN", SCHTASKS_NAME])
        return r.returncode == 0
    if mode == "cron":
        return TOML_BEGIN in (crontab_get or _crontab_get)()
    return False


def _crontab_get() -> str:
    r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return r.stdout if r.returncode == 0 else ""


def _crontab_set(text: str) -> None:
    subprocess.run(["crontab", "-"], input=text, text=True, check=True)


# ------------------------------------------------------------------- launchd

def _plist_path(home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _install_launchd(argv: list[str], plist_path: Path, activate: bool,
                     run: Runner, changed: list[str], backed: list[str],
                     hour: int = 2, minute: int = 30) -> dict:
    """A user LaunchAgent firing at the configured time. RunAtLoad=false —
    bootstrapping the agent at install time must not kick off a full nightly
    run right then."""
    body = plistlib.dumps({  # sort_keys=True by default → byte-stable → idempotent
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": argv,
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "RunAtLoad": False,
    }).decode("utf-8")
    _write_if_changed(plist_path, body, changed, backed)

    note = "unchanged"
    if activate and changed:
        uid = os.getuid()  # type: ignore[attr-defined]  # launchd ⇒ macOS
        run(["launchctl", "bootout", f"gui/{uid}/{LAUNCHD_LABEL}"])  # may not exist yet
        r = run(["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)])
        note = "bootstrapped" if r.returncode == 0 else (
            f"plist written but not loaded (exit {r.returncode}); run manually: "
            f"launchctl bootstrap gui/{uid} {plist_path}")
    return {"changed": changed, "backed_up": backed, "mode": "launchd", "note": note}


# ------------------------------------------------------------------ schtasks

def _schtasks_command(xml_text: str) -> str | None:
    """The command line of an existing task, rebuilt from its XML definition.
    schtasks splits what you gave `/TR` into <Command> + <Arguments>."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    cmd = root.find(".//{*}Exec/{*}Command")
    if cmd is None or not cmd.text:
        return None
    args = root.find(".//{*}Exec/{*}Arguments")
    return f"{cmd.text} {args.text}".strip() if (args is not None and args.text) \
        else cmd.text.strip()


def _schtasks_start(xml_text: str) -> str | None:
    """The task's HH:MM from its <StartBoundary> (e.g. 2026-07-29T02:30:00)."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    sb = root.find(".//{*}StartBoundary")
    if sb is None or not sb.text or "T" not in sb.text:
        return None
    return sb.text.split("T", 1)[1][:5]


def _install_schtasks(argv: list[str], activate: bool, run: Runner,
                      changed: list[str], backed: list[str],
                      at: str = "02:30") -> dict:
    """Task Scheduler daily at the configured time. `/TR` takes ONE string that
    Windows re-splits, so `Program Files` paths must be quoted — list2cmdline,
    never " ".join. Remove with: schtasks /Delete /TN afterwit-nightly /F"""
    tr = subprocess.list2cmdline(argv)
    # Idempotence compares command AND schedule (the ponytail command-only
    # shortcut bit the moment run_time became configurable). A task XML with no
    # readable StartBoundary falls back to command-only — degrade to the old
    # behaviour rather than recreating the task every install.
    existing = run(["schtasks", "/Query", "/TN", SCHTASKS_NAME, "/XML", "ONE"])
    if existing.returncode == 0 and _schtasks_command(existing.stdout or "") == tr \
            and _schtasks_start(existing.stdout or "") in (at, None):
        return {"changed": changed, "backed_up": backed, "mode": "schtasks",
                "note": "unchanged"}

    note = f"task {SCHTASKS_NAME} pending"
    if activate:
        r = run(["schtasks", "/Create", "/TN", SCHTASKS_NAME, "/TR", tr,
                 "/SC", "DAILY", "/ST", at, "/F"])
        if r.returncode != 0:
            return {"changed": changed, "backed_up": backed, "mode": "schtasks",
                    "note": f"schtasks /Create failed (exit {r.returncode}): "
                            f"{(r.stderr or '').strip()}"}
        note = f"registered {SCHTASKS_NAME}"
    changed.append(f"schtasks:{SCHTASKS_NAME}")
    return {"changed": changed, "backed_up": backed, "mode": "schtasks", "note": note}


# ------------------------------------------------------------------- json io

def _load_json(path: Path) -> dict:
    if path.exists() and path.read_text(encoding="utf-8").strip():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def _dump_json(obj: dict) -> str:
    return json.dumps(obj, indent=2) + "\n"


# ---------------------------------------------------------------------- main

def main(harness: str) -> int:
    if harness == "claude":
        res = install_claude()
    elif harness == "codex":
        res = install_codex()
    elif harness == "cron":
        from . import config as config_mod  # lazy: config.save imports install

        res = install_cron(at=config_mod.load().run_time)
    else:
        print(f"unknown harness: {harness}")
        return 2
    if res["changed"]:
        print(f"installed for {harness}:")
        for p in res["changed"]:
            print(f"  wrote {p}")
        for b in res["backed_up"]:
            print(f"  backup {b}")
    else:
        print(f"{harness}: already up to date (no changes)")
    if res.get("note"):
        print(f"  {res['note']}")
    return 0
