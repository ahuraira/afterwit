"""Claude Code JSONL adapter. SPEC §6.1."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from afterwit import config as config_mod
from afterwit.adapters import warn_once
from afterwit.events import Event
from afterwit.redact import redact

log = logging.getLogger(__name__)

_DROP_TYPES = {
    "attachment",
    "file-history-snapshot",
    # Sibling of the snapshot above: the CLI's own undo journal (a per-edit patch),
    # never knowledge. Dropped for the same reason, and dropping it is the ROOT fix
    # for the 585 KB warning flood of 2026-07-29 — warn_once only bounds the noise.
    "file-history-delta",
    "last-prompt",
    "mode",
    "permission-mode",
    "queue-operation",
    "bridge-session",
    "system",
    # UI plumbing, reviewed: an artifact URL, an agent's display name, a PR link.
    "frame-link",
    "agent-name",
    "pr-link",
}


def iter_events(path: Path) -> Iterator[Event]:
    cfg = config_mod.load()
    with path.open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                log.warning("claude_jsonl invalid json %s:%s: %s", path, lineno, exc)
                continue
            yield from _events_for_record(rec, path, lineno, cfg)


def _events_for_record(
    rec: dict[str, Any], path: Path, lineno: int, cfg: config_mod.Config
) -> Iterator[Event]:
    typ = rec.get("type")
    if rec.get("isCompactSummary"):
        text = _message_text(rec.get("message"))
        if text:
            yield _event(rec, path, lineno, "assistant", "assistant", text, cfg,
                         {"compaction": True})
        return
    if typ in _DROP_TYPES:
        return
    if typ == "ai-title":
        text = str(rec.get("title") or rec.get("content") or "").strip()
        if text:
            yield _event(rec, path, lineno, "assistant", "assistant", text, cfg,
                         {"record_type": "ai-title"})
        return
    if typ == "user":
        yield from _user_events(rec, path, lineno, cfg)
        return
    if typ == "assistant":
        yield from _assistant_events(rec, path, lineno, cfg)
        return
    warn_once((path, typ), "claude_jsonl unknown record type %r at %s:%s (further "
              "occurrences in this file suppressed)", typ, path, lineno)


def _user_events(
    rec: dict[str, Any], path: Path, lineno: int, cfg: config_mod.Config
) -> Iterator[Event]:
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if rec.get("isMeta") and not rec.get("isCompactSummary"):
        return
    if isinstance(content, str):
        if _synthetic_user_text(content):
            return
        yield _event(rec, path, lineno, "user", "user", content, cfg, {})
        return
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = str(block.get("text") or "")
            if text and not _synthetic_user_text(text):
                yield _event(rec, path, lineno, "user", "user", text, cfg,
                             {"block_type": "text"})
        elif btype == "tool_result":
            text = _tool_result_text(block, rec)
            if text:
                meta = {"block_type": "tool_result", "is_error": _is_tool_error(block, rec)}
                yield _event(rec, path, lineno, "user", "user", text, cfg, meta)


def _assistant_events(
    rec: dict[str, Any], path: Path, lineno: int, cfg: config_mod.Config
) -> Iterator[Event]:
    msg = rec.get("message")
    model = msg.get("model") if isinstance(msg, dict) else None
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        yield _event(rec, path, lineno, "assistant", "assistant", content, cfg,
                     {"model": model})
        return
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = str(block.get("text") or "")
            if text:
                yield _event(rec, path, lineno, "assistant", "assistant", text, cfg,
                             {"block_type": "text", "model": model})
        elif btype == "thinking":
            text = str(block.get("thinking") or block.get("text") or "")
            if text:
                yield _event(rec, path, lineno, "assistant", "thinking", text, cfg,
                             {"block_type": "thinking", "model": model})
        elif btype == "tool_use":
            name = block.get("name") or "tool"
            args = json.dumps(block.get("input") or {}, sort_keys=True)[:1000]
            yield _event(
                rec,
                path,
                lineno,
                "assistant",
                "assistant",
                f"tool_use {name}: {args}",
                cfg,
                {"block_type": "tool_use", "tool": name, "model": model},
            )


def _tool_result_text(block: dict[str, Any], rec: dict[str, Any]) -> str:
    text = block.get("content")
    if isinstance(text, list):
        text = "\n".join(str(x.get("text", "")) if isinstance(x, dict) else str(x) for x in text)
    text = str(text or "")
    if not _is_tool_error(block, rec) and len(text) > 500:
        return ""
    return text


def _is_tool_error(block: dict[str, Any], rec: dict[str, Any]) -> bool:
    if bool(block.get("is_error")):
        return True
    tur = rec.get("toolUseResult")
    return isinstance(tur, dict) and bool(tur.get("stderr"))


def _message_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text") or block.get("content") or ""))
        return "\n".join(p for p in parts if p)
    return ""


def _synthetic_user_text(text: str) -> bool:
    return (
        "<local-command-caveat>" in text
        or "<local-command-stdout>" in text
        or "<command-name>" in text
    )


def _event(
    rec: dict[str, Any],
    path: Path,
    lineno: int,
    role: str,
    kind: str,
    text: str,
    cfg: config_mod.Config,
    meta: dict[str, Any],
) -> Event:
    cwd = str(rec.get("cwd") or "")
    model = meta.pop("model", None)
    clean_meta: dict[str, Any] = {
        "harness": "claude",
        "kind": kind,
        "model": model,
        # Claude Code stamps the session's reasoning effort on the record itself
        # ("max"/"high"/…), not inside `message` — a card distilled from a
        # low-effort turn is weaker evidence than one from a max-effort turn.
        "effort": rec.get("effort"),
        "uuid": rec.get("uuid"),
        "parent_uuid": rec.get("parentUuid"),
        "sidechain": bool(rec.get("isSidechain")),
        **meta,
    }
    if rec.get("gitBranch"):
        clean_meta["git_branch"] = rec.get("gitBranch")
    return Event(
        source_path=str(path),
        lines=(lineno, lineno),
        project=config_mod.project_from_cwd(cwd, cfg.projects_root, cfg.project_aliases) if cwd else _project_from_path(path, cfg.project_aliases),
        ts=rec.get("timestamp"),
        role=role,
        kind=kind,
        text=redact(text),
        meta=clean_meta,
    )


def _project_from_path(path: Path, aliases: dict[str, str] | None = None) -> str:
    """Fallback when a record carries no cwd: recover the slug from Claude Code's
    flattened transcript directory (`-home-user-Desktop-Projects-my-proj`).

    Aliased like every other slug source (ADR-039) — otherwise a cwd-less record
    stamps the folder name while its neighbours in the same session stamp the
    real slug, and one session mints cards under two projects."""
    for part in path.parts:
        if part.startswith("-home-"):
            folder = part.rsplit("-Projects-", 1)[-1].replace("-", "_") or "global"
            return (aliases or {}).get(folder, folder)
    return "global"
