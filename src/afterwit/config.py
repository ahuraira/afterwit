"""Config: ~/.afterwit/config.toml with safe defaults.

AFTERWIT_CONFIG env var overrides the path (tests, alternate setups).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _safe_host() -> str:
    host = re.sub(r"[^A-Za-z0-9_.-]+", "-", socket.gethostname() or "")
    return host.strip("-")[:32] or "device"


def device_id() -> str:
    """Stable per-device identity (ADR-020). Hostname alone is NOT unique — two
    machines can share one, and a collision recreates the very log conflict
    ADR-019 removed. Persisted at ~/.afterwit/device_id; the hostname is
    kept as a readable prefix. Unwritable home → falls back to hostname alone
    (degrade, never crash)."""
    path = Path.home() / ".afterwit" / "device_id"
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass
    ident = f"{_safe_host()}-{secrets.token_hex(3)}"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(ident + "\n", encoding="utf-8")
    except OSError:
        return _safe_host()
    return ident


def log_path(wiki_root: Path) -> Path:
    """The one audit log for this device (ADR-019/020). Lives here, not in wiki.py,
    because wiki imports ui — anything ui or distill logs to would be a cycle."""
    return wiki_root / f"log-{device_id()}.md"


@dataclass
class Config:
    wiki_root: Path
    db_path: Path
    projects_root: Path
    # Folder name -> project slug, for a project whose directory is not named what
    # the project is called. Without it a slug IS a folder name: `project_from_cwd`
    # returns `rel.parts[0]` verbatim, so renaming a project without moving its
    # directory silently breaks it — the session line is a hard project filter
    # (112 cards -> 0 when this repo's slug moved), ranking loses its +0.15
    # same-project boost, and the adapters keep stamping the old slug on every new
    # card, so the rename undoes itself on the next nightly. ADR-039.
    project_aliases: dict[str, str] = field(default_factory=dict)
    floor: float = 0.35
    inject_max_cards: int = 3
    inject_max_tokens: int = 600
    session_max_tokens: int = 300
    # HIGH RISK opt-out of ADR-011: when true, push injection may serve cards no
    # human has reviewed (labeled [unverified]). Memory-poisoning surface — a bad
    # extraction auto-propagates into every session. Leave false unless you
    # review the queue rarely and accept that risk.
    push_unverified: bool = False
    # WHICH card types may be pushed unasked. Pull (recall/why/for_file/lookup_error)
    # is unrestricted — the agent asked, so anything it can read is fair.
    #
    # Push is different: it spends the agent's attention without being asked, so a
    # card only earns a slot if knowing it can CHANGE what happens next. Measured on
    # 197 real inject servings: decision 77, fact 43, gotcha 39, doc_ref 24 — i.e.
    # 73% of the budget went to reference material. "We chose JSONB because the
    # schema churns" is worth looking up and worthless as an interruption; "this API
    # truncates silently" changes the next edit. Types here are behavioral; the rest
    # stay fully reachable by asking. Widen via `push_types` in config.toml.
    push_types: frozenset[str] = frozenset({"gotcha", "error_fix", "preference"})
    # 24h local time the scheduled nightly fires (ADR-046). Read by
    # `afterwit install cron`; the Settings save path also reapplies an
    # ALREADY-INSTALLED scheduler when this changes.
    run_time: str = "02:30"
    # ADR-045: cards judged for curated `related:` links per nightly run. 0 = off,
    # the default until the hand-judged precision sweep (`afterwit relink
    # --dry-run`) has passed on this corpus. Each card costs one LLM judge call.
    relink_budget: int = 0
    # Distillation LLM defaults; `afterwit run`/`afterwit distill` --driver/--model/--effort
    # flags override per invocation. Leaving model/effort unset inherits whatever
    # that harness's own config says (~/.claude/settings.json, ~/.codex/config.toml).
    distill_driver: str = "claude-p"
    distill_model: str | None = None
    distill_effort: str | None = None
    # ADR-021. Off by default. When on, an INDEPENDENT model may clear cards for
    # serving; it abstains on doubt and can never approve a `preference` card.
    # `auto_review_driver` defaults to the driver the distiller is NOT using, so
    # the writer and the approver are never the same model.
    auto_review: bool = False
    auto_review_driver: str | None = None
    auto_review_model: str | None = None
    auto_review_effort: str | None = None
    # The wiki is mined from private sessions. `afterwit sync` refuses to push to a
    # remote it can prove is a public GitHub repo unless this is explicitly set.
    allow_public_wiki_remote: bool = False
    databases: list[dict] = field(default_factory=list)


def _state_dir() -> Path:
    """~/.afterwit, migrating a pre-rename ~/.harness_helper in place the first
    time (one `os.rename`, same filesystem, atomic). Existing config.toml,
    index.db, device_id and archive follow the directory, so a dogfooding user's
    440-card index is not orphaned by the rename. Best-effort: an unwritable or
    already-present target just uses ~/.afterwit as-is."""
    new = Path.home() / ".afterwit"
    old = Path.home() / ".harness_helper"
    if not new.exists() and old.is_dir():
        try:
            old.rename(new)
        except OSError:
            return new
    return new


def load(path: str | Path | None = None) -> Config:
    p = Path(path or os.environ.get("AFTERWIT_CONFIG")
             or _state_dir() / "config.toml")
    data: dict = {}
    if p.exists():
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    home = Path.home()
    return Config(
        wiki_root=Path(data.get("wiki_root", home / "knowledge")).expanduser(),
        db_path=Path(data.get("db_path", home / ".afterwit" / "index.db")).expanduser(),
        projects_root=Path(data.get("projects_root", home / "Desktop" / "Projects")).expanduser(),
        project_aliases={str(k): str(v) for k, v in
                         (data.get("project_aliases") or {}).items()},
        floor=float(data.get("floor", 0.35)),
        inject_max_cards=int(data.get("inject_max_cards", 3)),
        inject_max_tokens=int(data.get("inject_max_tokens", 600)),
        session_max_tokens=int(data.get("session_max_tokens", 300)),
        push_unverified=bool(data.get("push_unverified", False)),
        # An explicitly empty list means "push nothing" and must survive; only an
        # absent key falls back to the default set.
        push_types=(frozenset(str(t) for t in data["push_types"])
                    if "push_types" in data
                    else Config.push_types),
        run_time=str(data.get("run_time", "02:30")),
        relink_budget=int(data.get("relink_budget", 0)),
        distill_driver=str(data.get("distill_driver", "claude-p")),
        distill_model=data.get("distill_model"),
        distill_effort=data.get("distill_effort"),
        auto_review=bool(data.get("auto_review", False)),
        auto_review_driver=data.get("auto_review_driver"),
        auto_review_model=data.get("auto_review_model"),
        auto_review_effort=data.get("auto_review_effort"),
        allow_public_wiki_remote=bool(data.get("allow_public_wiki_remote", False)),
        databases=list(data.get("databases", [])),
    )


# --------------------------------------------------------------- editable schema
#
# The single description of what a human may change and what a legal value is.
# The UI renders this (it ships no field list of its own) and `save()` validates
# against it, so a new setting is one entry here, not an edit in three files.
#
# `kind` drives both the widget and the coercion:
#   driver  select of the installed distillation drivers ("" = inherit)
#   model   free text + a datalist of models discovered in the harness config of
#           the driver named by `of` (harness.models) — ids go stale, so nothing
#           is hardcoded and nothing is rejected for being unrecognised
#   effort  same, for reasoning-effort levels
#   bool | int | float | path | str
#
# Injection caps are bounded HERE, not only by good intentions: Manifesto P3
# makes ≤3 cards / ≤600 tokens a hard limit, so the UI must not be able to
# offer a value that breaks it.
EDITABLE: dict[str, dict[str, Any]] = {
    "distill_driver": {
        "section": "Distillation", "kind": "driver", "label": "Driver", "required": True,
        "help": "Which harness CLI turns sessions into cards. Uses that harness's own subscription."},
    "distill_model": {
        "section": "Distillation", "kind": "model", "of": "distill_driver", "label": "Model",
        "help": "Leave empty to inherit the model the harness itself is configured with."},
    "distill_effort": {
        "section": "Distillation", "kind": "effort", "of": "distill_driver", "label": "Reasoning effort",
        "help": "Leave empty to inherit the harness default."},
    "run_time": {
        "section": "Nightly run", "kind": "time", "label": "Run the nightly at", "required": True,
        "help": "24h local time for the scheduled `afterwit run`. Applies immediately when the "
                "nightly is already scheduled; otherwise takes effect at `afterwit install cron`."},
    "relink_budget": {
        "section": "Distillation", "kind": "int", "label": "Cards auto-linked per nightly run",
        "min": 0, "max": 100, "step": 5,
        "help": "ADR-045: kNN proposes neighbours, an LLM judge prunes them into machine-owned "
                "`related:` links. 0 = off. Undo everything: afterwit relink --strip"},
    "auto_review": {
        "section": "Auto-review", "kind": "bool", "label": "Let a second model clear cards",
        "help": "ADR-021. Off means every card waits for you here. On means an independent "
                "model may approve cards for serving; it abstains on doubt and can never "
                "approve a preference card."},
    "auto_review_driver": {
        "section": "Auto-review", "kind": "driver", "label": "Reviewer driver",
        "help": "Empty = the driver the distiller is NOT using, so writer and approver are "
                "never the same model."},
    "auto_review_model": {
        "section": "Auto-review", "kind": "model", "of": "auto_review_driver", "label": "Reviewer model",
        "help": "Empty inherits that harness's configured model."},
    "auto_review_effort": {
        "section": "Auto-review", "kind": "effort", "of": "auto_review_driver", "label": "Reviewer effort",
        "help": "Empty inherits the harness default."},
    "floor": {
        "section": "Injection", "kind": "float", "label": "Relevance floor",
        "min": 0.0, "max": 1.0, "step": 0.05,
        "help": "Below this score nothing is injected. Raise it if injected cards feel off-topic."},
    "inject_max_cards": {
        "section": "Injection", "kind": "int", "label": "Max cards per prompt",
        "min": 0, "max": 3, "step": 1,
        "help": "Hard ceiling of 3 (Manifesto P3). 0 switches prompt injection off."},
    "inject_max_tokens": {
        "section": "Injection", "kind": "int", "label": "Max tokens per prompt",
        "min": 0, "max": 600, "step": 50, "help": "Hard ceiling of 600 (Manifesto P3)."},
    "session_max_tokens": {
        "section": "Injection", "kind": "int", "label": "Max tokens at session start",
        "min": 0, "max": 600, "step": 50, "help": "Budget for the session-start brief."},
    "push_unverified": {
        "section": "Injection", "kind": "bool", "label": "Serve cards nobody reviewed", "risky": True,
        "help": "HIGH RISK (ADR-011 opt-out): one bad extraction then auto-propagates into "
                "every session. Only if you never open this queue."},
    "wiki_root": {
        "section": "Paths", "kind": "path", "label": "Wiki root", "required": True,
        "help": "Source of truth: one markdown file per card. Changing this does NOT move "
                "existing cards — move them yourself, then rebuild the index."},
    "db_path": {
        "section": "Paths", "kind": "path", "label": "Index database", "required": True,
        "help": "Rebuildable cache. After changing it run: afterwit index --rebuild"},
    "projects_root": {
        "section": "Paths", "kind": "path", "label": "Projects root", "required": True,
        "help": "The folder whose direct children name your projects."},
    "allow_public_wiki_remote": {
        "section": "Sync", "kind": "bool", "label": "Allow pushing to a public remote", "risky": True,
        "help": "The wiki is mined from private sessions. Leave off unless you truly mean it."},
}


class ConfigError(ValueError):
    """A rejected setting — message is safe to show a user verbatim."""


def coerce(key: str, raw: Any, drivers: tuple[str, ...] = ("claude-p", "codex")) -> Any:
    """Validate one incoming setting and return the value to persist.

    Empty string means "unset" for every optional field, which is what makes
    "inherit the harness default" expressible from a web form.
    """
    spec = EDITABLE.get(key)
    if spec is None:
        raise ConfigError(f"unknown setting: {key}")
    kind = spec["kind"]
    if isinstance(raw, str):
        raw = raw.strip()
    if raw in ("", None):
        if spec.get("required"):
            raise ConfigError(f"{key} cannot be empty")
        return None
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        if str(raw).lower() in ("true", "1", "yes", "on"):
            return True
        if str(raw).lower() in ("false", "0", "no", "off"):
            return False
        raise ConfigError(f"{key} must be true or false")
    if kind in ("int", "float"):
        try:
            value = int(raw) if kind == "int" else float(raw)
        except (TypeError, ValueError):
            raise ConfigError(f"{key} must be a number") from None
        low, high = spec.get("min"), spec.get("max")
        if (low is not None and value < low) or (high is not None and value > high):
            raise ConfigError(f"{key} must be between {low} and {high}")
        return value
    if kind == "driver":
        if raw not in drivers:
            raise ConfigError(f"{key} must be one of {', '.join(drivers)}")
        return str(raw)
    if kind == "time":
        from . import install  # lazy both ways: install imports config lazily too

        try:
            install._parse_time(str(raw))
        except ValueError as e:
            raise ConfigError(str(e)) from None
        return str(raw).strip()
    if kind == "path":
        path = Path(str(raw)).expanduser()
        if not path.is_absolute():
            raise ConfigError(f"{key} must be an absolute path (got {raw})")
        if not path.parent.exists():
            raise ConfigError(f"{key}: parent folder does not exist ({path.parent})")
        return str(path)
    return str(raw)  # model / effort / str — ids drift, so never reject unknown ones


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    # json.dumps == a TOML basic string, and it escapes the backslashes in
    # Windows paths (the same trick install.py uses for the MCP argv).
    return json.dumps(str(value))


def save(updates: dict[str, Any], path: str | Path | None = None) -> Path:
    """Persist scalar settings into config.toml, byte-preserving everything else.

    Line-oriented on purpose. tomllib is read-only (stdlib, and no new deps), and
    a dump-the-whole-dict writer would erase the user's comments and reorder
    their file on every save. A value of None deletes the key so the built-in
    default applies again.

    A new key is inserted BEFORE the first table header, never appended: at EOF
    it would land inside `[[databases]]` and silently become `databases.floor`.
    """
    target = Path(path or os.environ.get("AFTERWIT_CONFIG") or _state_dir() / "config.toml")
    text = target.read_text(encoding="utf-8") if target.exists() else ""
    lines = text.splitlines()
    insert_at = next((i for i, ln in enumerate(lines) if ln.lstrip().startswith("[")), len(lines))
    inserted = False

    for key, value in updates.items():
        if key not in EDITABLE:
            raise ConfigError(f"unknown setting: {key}")
        found = next((i for i in range(insert_at)
                      if re.match(rf"\s*{re.escape(key)}\s*=", lines[i])), None)
        if value is None:
            if found is not None:
                del lines[found]
                insert_at -= 1
            continue
        rendered = f"{key} = {_toml_value(value)}"
        if found is None:
            lines.insert(insert_at, rendered)
            insert_at += 1
            inserted = True
        else:
            lines[found] = rendered

    if (inserted and insert_at < len(lines) and lines[insert_at].lstrip().startswith("[")
            and insert_at and lines[insert_at - 1].strip()):
        lines.insert(insert_at, "")  # keep a blank line before the first table
    out = "\n".join(lines).strip("\n") + "\n"
    try:
        parsed = tomllib.loads(out)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"refusing to write unparseable config: {e}") from None
    for key, value in updates.items():  # the edit landed where we meant it to
        landed = parsed.get(key)
        if (landed is not None) if value is None else (landed != value):
            raise ConfigError(f"refusing to write: {key} did not round-trip")

    if target.exists():
        from . import install  # same backup convention as the harness installers

        install._backup(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(out, encoding="utf-8")
    return target


def project_from_cwd(cwd: str | Path, projects_root: Path,
                     aliases: dict[str, str] | None = None) -> str:
    """Map a working directory to a project slug; 'global' if outside projects_root.

    `aliases` maps the FOLDER name to the project's real slug (ADR-039). Default
    None keeps the old identity behaviour for every caller that has no config to
    hand — a project whose folder is named what it is called needs no entry.
    """
    try:
        rel = Path(cwd).resolve().relative_to(projects_root.resolve())
    except ValueError:
        return "global"
    if not rel.parts:
        return "global"
    return (aliases or {}).get(rel.parts[0], rel.parts[0])


def project_dir_name(slug: str, aliases: dict[str, str] | None = None) -> str:
    """The reverse of `project_from_cwd`: the folder a slug actually lives in.

    Anything that touches the working tree — git anchoring, staleness resolution —
    needs the folder, not the slug, and they stop being the same thing the moment
    an alias exists. Falls back to the slug, which is correct for every project
    that is not aliased.
    """
    for folder, alias in (aliases or {}).items():
        if alias == slug:
            return folder
    return slug
