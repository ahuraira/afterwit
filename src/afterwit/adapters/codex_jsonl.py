"""Codex JSONL adapter. SPEC §6.2."""

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

# Reviewed and carries no knowledge. The warning below is a schema-drift alarm, so
# every silence here is a deliberate judgement, not a blanket mute:
#   world_state  — a snapshot of AGENTS.md/CLAUDE.md. Real content, but the docs and
#                  memory adapters already ingest those files; indexing it here would
#                  feed the user's own instructions back as if they were findings.
#   inter_agent_communication_metadata — payload is `{"trigger_turn": true}`. Plumbing.
_DROP_TYPES = {
    "world_state",
    "inter_agent_communication_metadata",
}


def iter_events(path: Path) -> Iterator[Event]:
    cfg = config_mod.load()
    state: dict[str, Any] = {"cwd": "", "model": None, "effort": None}
    with path.open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                log.warning("codex_jsonl invalid json %s:%s: %s", path, lineno, exc)
                continue
            yield from _events_for_record(rec, path, lineno, cfg, state)


def _events_for_record(
    rec: dict[str, Any],
    path: Path,
    lineno: int,
    cfg: config_mod.Config,
    state: dict[str, Any],
) -> Iterator[Event]:
    typ = rec.get("type")
    raw_payload = rec.get("payload")
    payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
    if typ in _DROP_TYPES:
        return
    if typ == "session_meta":
        state["cwd"] = payload.get("cwd") or state.get("cwd") or ""
        state["model"] = payload.get("model") or payload.get("model_slug") or state.get("model")
        return
    if typ == "turn_context":
        # Codex records the model and reasoning effort HERE, per turn — its
        # `session_meta` carries `model_provider` but no model at all, so reading
        # only session_meta left every codex card with `model: null` while claude
        # cards had one (audit 2026-07-23: 79 codex cards, 0 models). A later turn
        # may switch model mid-session; the state carries the newest either way.
        state["cwd"] = payload.get("cwd") or state.get("cwd") or ""
        state["model"] = payload.get("model") or state.get("model")
        state["effort"] = (payload.get("effort") or payload.get("model_reasoning_effort")
                           or state.get("effort"))
        return
    if typ == "event_msg":
        yield from _event_msg(payload, rec, path, lineno, cfg, state)
        return
    if typ == "response_item":
        yield from _response_item(payload, rec, path, lineno, cfg, state)
        return
    if typ == "compacted":
        hist = payload.get("replacement_history")
        if hist:
            text = hist if isinstance(hist, str) else json.dumps(hist, ensure_ascii=False)[:5000]
            yield _event(rec, path, lineno, cfg, state, "assistant", "assistant", text,
                         {"compaction": True})
        return
    warn_once((path, typ), "codex_jsonl unknown record type %r at %s:%s (further "
              "occurrences in this file suppressed)", typ, path, lineno)


def _event_msg(
    payload: dict[str, Any],
    rec: dict[str, Any],
    path: Path,
    lineno: int,
    cfg: config_mod.Config,
    state: dict[str, Any],
) -> Iterator[Event]:
    ptype = payload.get("type")
    if ptype == "user_message":
        text = _payload_text(payload)
        if text:
            yield _event(rec, path, lineno, cfg, state, "user", "user", text, {})
    elif ptype == "agent_message":
        text = _payload_text(payload)
        if text:
            yield _event(rec, path, lineno, cfg, state, "assistant", "assistant", text, {})
    elif ptype in {"token_count", "turn_aborted", "task_started"}:
        return


def _response_item(
    payload: dict[str, Any],
    rec: dict[str, Any],
    path: Path,
    lineno: int,
    cfg: config_mod.Config,
    state: dict[str, Any],
) -> Iterator[Event]:
    ptype = payload.get("type")
    if ptype == "message":
        role = str(payload.get("role") or "assistant")
        kind = "user" if role == "user" else "assistant"
        text = _content_text(payload.get("content"))
        if text:
            yield _event(rec, path, lineno, cfg, state, role, kind, text, {})
    elif ptype == "function_call":
        name = str(payload.get("name") or "function_call")
        args = str(payload.get("arguments") or "")[:1000]
        yield _event(rec, path, lineno, cfg, state, "assistant", "assistant",
                     f"function_call {name}: {args}", {"tool": name})
    elif ptype == "function_call_output":
        text = str(payload.get("output") or "")
        if not text:
            return
        is_error = bool(payload.get("is_error") or payload.get("error"))
        if not is_error:
            text = text[:1000]
        yield _event(rec, path, lineno, cfg, state, "tool", "assistant", text,
                     {"block_type": "function_call_output", "is_error": is_error})
    elif ptype == "reasoning":
        text = _reasoning_text(payload)
        if text:
            yield _event(rec, path, lineno, cfg, state, "assistant", "thinking", text, {})
    elif ptype in {"web_search_call", "custom_tool_call"}:
        return


def _payload_text(payload: dict[str, Any]) -> str:
    for key in ("message", "text", "content"):
        val = payload.get(key)
        if isinstance(val, str):
            return val
    return ""


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            val = block.get("text") or block.get("content")
            if isinstance(val, str):
                parts.append(val)
    return "\n".join(parts)


def _reasoning_text(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("content"), str):
        return str(payload["content"])
    summary = payload.get("summary")
    if not isinstance(summary, list):
        return ""
    parts: list[str] = []
    for item in summary:
        if isinstance(item, dict):
            val = item.get("text") or item.get("summary") or item.get("content")
            if isinstance(val, str):
                parts.append(val)
        elif isinstance(item, str):
            parts.append(item)
    return "\n".join(parts)


def _event(
    rec: dict[str, Any],
    path: Path,
    lineno: int,
    cfg: config_mod.Config,
    state: dict[str, Any],
    role: str,
    kind: str,
    text: str,
    meta: dict[str, Any],
) -> Event:
    cwd = str(state.get("cwd") or "")
    ts = rec.get("timestamp")
    return Event(
        source_path=str(path),
        lines=(lineno, lineno),
        project=config_mod.project_from_cwd(cwd, cfg.projects_root, cfg.project_aliases) if cwd else "global",
        ts=ts if isinstance(ts, str) else None,
        role=role,
        kind=kind,
        text=redact(text),
        meta={"harness": "codex", "model": state.get("model"),
              "effort": state.get("effort"), "kind": kind, **meta},
    )
