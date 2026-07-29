"""Where each harness keeps its files, and which models it offers on THIS machine.

One module, because five call sites each spelled `Path.home() / ".claude"` by
hand and none honoured `CLAUDE_CONFIG_DIR` / `CODEX_HOME` — so a non-default
harness install was invisible to install, doctor, ingest and the UI alike.

Read-only by contract: afterwit reads harness config to *offer* choices (model
lists, effort levels, "is this harness even here?"); the only code allowed to
write a harness config is `afterwit install`, which backs up and fences first.

Cross-platform: every path is home-relative (`Path.home()` reads `%USERPROFILE%`
on Windows, `$HOME` elsewhere), so nothing here is POSIX-only.
"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import Any

HARNESSES = ("claude", "codex")

# driver name (config `distill_driver`) → the harness whose config describes it
_DRIVER_HARNESS = {"claude-p": "claude", "codex": "codex"}

# Hints, not authority — every model/effort field in the UI is free text, and the
# values discovered from the live harness config are listed first. These only
# keep the dropdown useful on a machine whose config has never been touched.
_CLAUDE_ALIASES = ("default", "fable", "opus", "sonnet", "haiku")
_CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")  # `claude --effort`
_CODEX_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")  # model_reasoning_effort


def harness_of(driver: str) -> str:
    """'claude-p' → 'claude'. Unknown driver names pass through unchanged."""
    return _DRIVER_HARNESS.get(driver, driver)


def config_dir(harness: str) -> Path:
    """The harness's config directory. The env override wins — a user who moved
    it there has moved everything, including the files `afterwit install` edits."""
    if harness == "claude":
        env = os.environ.get("CLAUDE_CONFIG_DIR")
        return Path(env).expanduser() if env else Path.home() / ".claude"
    if harness == "codex":
        env = os.environ.get("CODEX_HOME")
        return Path(env).expanduser() if env else Path.home() / ".codex"
    raise ValueError(f"unknown harness: {harness}")


def settings_path(harness: str) -> Path:
    """The file holding the harness's own model/effort defaults."""
    return config_dir(harness) / ("settings.json" if harness == "claude" else "config.toml")


def claude_json_path() -> Path:
    """Claude Code's global config (MCP servers, model caches). It sits at
    ~/.claude.json by default but moves INSIDE the config dir when
    CLAUDE_CONFIG_DIR is set — checking both is what makes a relocated install
    visible to `afterwit doctor`."""
    inside = config_dir("claude") / ".claude.json"
    if os.environ.get("CLAUDE_CONFIG_DIR") or inside.exists():
        return inside
    return Path.home() / ".claude.json"


def agents_path(harness: str) -> Path:
    """Where the operator block is fenced in (Codex only today)."""
    return config_dir(harness) / "AGENTS.md"


def skills_dir(harness: str) -> Path:
    return config_dir(harness) / "skills"


def sessions_dir(harness: str) -> Path:
    """Root of the transcripts the adapters mine."""
    return config_dir(harness) / ("projects" if harness == "claude" else "sessions")


# ------------------------------------------------------------------ reading

def _read_json(path: Path) -> dict[str, Any]:
    """Never raise: a harness config that is missing, unreadable or mid-write
    must degrade to "nothing discovered", never break the settings page."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def settings(harness: str) -> dict[str, Any]:
    """The harness's own config, parsed. Empty dict when absent."""
    path = settings_path(harness)
    return _read_json(path) if harness == "claude" else _read_toml(path)


def _clean(values: list[Any]) -> list[str]:
    """Dedupe, drop blanks and non-strings, preserve discovery order."""
    out: list[str] = []
    for v in values:
        if isinstance(v, str) and v.strip() and v not in out:
            out.append(v)
    return out


def models(harness: str) -> list[str]:
    """Model ids this machine's harness knows, most-likely-wanted first.

    Claude Code caches the roster it was served in `.claude.json`
    (`additionalModelOptionsCache`); Codex records what it has offered in
    `tui.model_availability_nux` and what it renamed in `notice.model_migrations`.
    Reading those beats hardcoding ids that go stale every model release.
    """
    found: list[Any] = []
    if harness == "claude":
        found.append(settings("claude").get("model"))
        for opt in _read_json(claude_json_path()).get("additionalModelOptionsCache") or []:
            if isinstance(opt, dict):
                found.append(opt.get("value"))
        found.extend(_CLAUDE_ALIASES)
    elif harness == "codex":
        cfg = settings("codex")
        found.append(cfg.get("model"))
        profiles = cfg.get("profiles")
        if isinstance(profiles, dict):
            found.extend(p.get("model") for p in profiles.values() if isinstance(p, dict))
        nux = (cfg.get("tui") or {}).get("model_availability_nux")
        if isinstance(nux, dict):
            found.extend(nux.keys())
        migrations = (cfg.get("notice") or {}).get("model_migrations")
        if isinstance(migrations, dict):
            found.extend(migrations.values())  # the NEW id of each renamed model
    return _clean(found)


def efforts(harness: str) -> list[str]:
    """Reasoning-effort levels, the configured one first."""
    cfg = settings(harness)
    current = cfg.get("effortLevel") if harness == "claude" else cfg.get("model_reasoning_effort")
    known = _CLAUDE_EFFORTS if harness == "claude" else _CODEX_EFFORTS
    return _clean([current, *known])


def default_model(harness: str) -> str | None:
    """What the harness itself would use if afterwit passed no --model."""
    cfg = settings(harness)
    model = cfg.get("model")
    return model if isinstance(model, str) and model.strip() else None


def mcp_registered(harness: str) -> bool:
    """Is afterwit's MCP server wired into this harness right now?"""
    from . import install  # local import: install imports this module

    if harness == "claude":
        servers = _read_json(claude_json_path()).get("mcpServers")
        return isinstance(servers, dict) and install.MCP_NAME in servers
    try:
        text = settings_path("codex").read_text(encoding="utf-8")
    except OSError:
        return False
    return f"[mcp_servers.{install.MCP_NAME}]" in text


def info(harness: str) -> dict[str, Any]:
    """Everything the settings UI shows about one harness."""
    sess = sessions_dir(harness)
    path = settings_path(harness)
    return {
        "harness": harness,
        "driver": "claude-p" if harness == "claude" else "codex",
        "config_dir": str(config_dir(harness)),
        "config_file": str(path),
        "present": path.exists(),
        "model": default_model(harness),
        "effort": (settings(harness).get("effortLevel") if harness == "claude"
                   else settings(harness).get("model_reasoning_effort")),
        "models": models(harness),
        "efforts": efforts(harness),
        "sessions_dir": str(sess),
        "sessions_present": sess.is_dir(),
        "mcp_registered": mcp_registered(harness),
    }


def all_info() -> dict[str, dict[str, Any]]:
    return {h: info(h) for h in HARNESSES}
