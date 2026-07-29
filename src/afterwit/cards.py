"""Card contract: parse, validate, render, locate. SPEC §5.1.

A card missing id/type/status/sources is invalid and must never be written —
this module is the single enforcement point (Manifesto P6).
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from . import redact

CARD_TYPES = {
    "decision", "gotcha", "error_fix", "preference", "fact",
    "snippet", "concept", "db_schema", "doc_ref", "capability",
}
STATUSES = {"active", "superseded", "deprecated", "quarantined"}

# type → wiki subdirectory (project-scoped types); global types live under global/
TYPE_DIRS = {
    "decision": "decisions", "gotcha": "gotchas", "error_fix": "errors",
    "fact": "facts", "snippet": "snippets", "db_schema": "db",
    "doc_ref": "facts", "preference": "facts", "concept": "facts",
    "capability": "capabilities",  # SPEC §7a / ADR-014: pointers to living code
}

_WIKILINK = re.compile(r"\[\[([^\]|#]+)")
_CODE = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class CardError(ValueError):
    pass


def new_ulid(ts: float | None = None) -> str:
    """Stdlib ULID: 48-bit ms timestamp + 80 random bits, Crockford base32."""
    ms = int((time.time() if ts is None else ts) * 1000)
    n = (ms << 80) | int.from_bytes(os.urandom(10))
    out = []
    for _ in range(26):
        out.append(_CROCKFORD[n & 31])
        n >>= 5
    return "".join(reversed(out))


@dataclass
class Card:
    id: str
    type: str
    title: str
    project: str  # project slug or "global"
    status: str
    body: str
    sources: list[dict]  # [{path: str, lines: str|None, heading: str|None}]
    tags: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    confidence: float = 0.8
    verified: bool = False
    superseded_by: str | None = None
    # machine-owned curated links (ADR-045): card IDS picked by the relink judge
    # from kNN candidates, never hand-written prose. Living in frontmatter (not
    # SQLite) is what keeps `index --rebuild` lossless; being one strippable key
    # (`afterwit relink --strip`) is what makes the unreviewed write acceptable —
    # reversible-as-a-class instead of reviewed-per-link.
    related: list[str] = field(default_factory=list)
    created: str = ""
    updated: str = ""
    # usage checkpoint (ADR-008): live counters live in SQLite; these frontmatter
    # copies make `afterwit index --rebuild` and cross-device git sync lossless (P4).
    usefulness: float = 0.0
    last_used: str | None = None
    # commit the card's claims were true at (ADR-018). Optional: cards predating
    # commit anchoring, and cards in non-git projects, carry None and fall back
    # to the existence check. Never the basis for deletion — only demotion.
    source_commit: str | None = None
    # normalized `origin` URL of the project this card cites (ADR-020). This is
    # the CROSS-DEVICE key: the folder name under projects_root differs per
    # machine, the remote does not. Must live in frontmatter (synced), not the
    # device-local `projects` table, or a second device cannot resolve the card.
    repo_url: str | None = None
    # who cleared this card for serving (ADR-021): "human", or `driver:model:effort`
    # of the auto-reviewer. Never the distiller that wrote it — separation of duties
    # is the property the review gate actually enforces. None until approved.
    reviewed_by: str | None = None
    # who EXTRACTED it, same `driver:model:effort` shape (ADR-035). The card records
    # what the session ran on (per-source `harness`/`model`/`effort`) and who approved
    # it, but the model that did the extracting was nowhere — so "these cards are
    # weak, which model wrote them?" was unanswerable, and separation of duties was
    # unprovable after the fact. None for deterministic importers (no LLM involved).
    distilled_by: str | None = None

    def validate(self) -> None:
        if not self.id:
            raise CardError("card missing id")
        if self.type not in CARD_TYPES:
            raise CardError(f"bad card type: {self.type!r}")
        if self.status not in STATUSES:
            raise CardError(f"bad card status: {self.status!r}")
        if not self.title.strip():
            raise CardError("card missing title")
        if not self.project:
            raise CardError("card missing project")
        if not self.sources:
            raise CardError("card missing sources — provenance is mandatory")
        for s in self.sources:
            if not isinstance(s, dict) or not s.get("path"):
                raise CardError(f"source without path: {s!r}")
        if not (0.0 <= float(self.confidence) <= 1.0):
            raise CardError(f"confidence out of range: {self.confidence}")
        if not self.body.strip():
            raise CardError("card missing body")

    def wikilinks(self) -> list[str]:
        return [m.strip() for m in _WIKILINK.findall(_CODE.sub("", self.body))]

    def slug(self) -> str:
        s = re.sub(r"[^a-z0-9]+", "-", self.title.lower()).strip("-")
        return s[:60] or self.id.lower()

    def relpath(self) -> Path:
        """Wiki-relative path for this card."""
        sub = TYPE_DIRS[self.type]
        base = Path("global") if self.project == "global" else Path("projects") / self.project
        return base / sub / f"{self.slug()}.md"


def render(card: Card) -> str:
    card.validate()
    fm = {
        "id": card.id, "type": card.type, "title": card.title,
        "project": card.project, "status": card.status,
        "tags": card.tags, "files": card.files,
        "confidence": card.confidence, "verified": card.verified,
        "sources": card.sources,
        "created": card.created, "updated": card.updated,
    }
    if card.superseded_by:
        fm["superseded_by"] = card.superseded_by
    if card.related:
        fm["related"] = card.related
    if card.usefulness:
        fm["usefulness"] = round(float(card.usefulness), 2)
    if card.last_used:
        fm["last_used"] = card.last_used
    if card.source_commit:
        fm["source_commit"] = card.source_commit
    if card.repo_url:
        fm["repo_url"] = card.repo_url
    if card.reviewed_by:
        fm["reviewed_by"] = card.reviewed_by
    if card.distilled_by:
        fm["distilled_by"] = card.distilled_by
    head = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{head}\n---\n\n{card.body.strip()}\n"


def parse(text: str, *, path: str = "<memory>") -> Card:
    m = re.match(r"\A---\n(.*?)\n---\n?(.*)\Z", text, re.DOTALL)
    if not m:
        raise CardError(f"{path}: no frontmatter block")
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        raise CardError(f"{path}: bad YAML frontmatter: {e}") from e
    card = Card(
        id=str(fm.get("id", "")),
        type=str(fm.get("type", "")),
        title=str(fm.get("title", "")),
        project=str(fm.get("project", "")),
        status=str(fm.get("status", "active")),
        body=m.group(2).strip(),
        sources=fm.get("sources") or [],
        tags=[str(t) for t in (fm.get("tags") or [])],
        files=[str(f) for f in (fm.get("files") or [])],
        confidence=float(fm.get("confidence", 0.8)),
        verified=bool(fm.get("verified", False)),
        superseded_by=fm.get("superseded_by"),
        related=[str(r) for r in (fm.get("related") or [])],
        created=str(fm.get("created", "")),
        updated=str(fm.get("updated", "") or fm.get("created", "")),
        usefulness=float(fm.get("usefulness") or 0.0),
        last_used=fm.get("last_used"),
        source_commit=fm.get("source_commit"),
        repo_url=fm.get("repo_url"),
        reviewed_by=fm.get("reviewed_by"),
        distilled_by=fm.get("distilled_by"),
    )
    card.validate()
    return card


def load(path: Path) -> Card:
    return parse(path.read_text(encoding="utf-8"), path=str(path))


def sanitize(card: Card) -> Card:
    """Strip credentials and absolute home paths from a card, in place.

    The card — not the transcript — is what leaves this machine: the wiki is
    `git push`ed. Adapters redact at ingest, but the distiller, `save_insight`
    and `afterwit queue` all write cards that never passed an adapter. This is the one
    door they share. Mutates so the caller's subsequent `upsert_card` indexes
    exactly what was written to disk.
    """
    card.title = redact.sanitize(card.title)
    card.body = redact.sanitize(card.body)
    for s in card.sources:
        if s.get("path"):
            s["path"] = redact.scrub_home(str(s["path"]))
    # repo_url is synced frontmatter; strip any credential a pre-fix anchor
    # captured from a credentialed remote (audit: outside-the-10 finding).
    if card.repo_url:
        card.repo_url = redact.redact(card.repo_url)
    return card


def save(card: Card, wiki_root: Path) -> Path:
    sanitize(card)
    desired = wiki_root / card.relpath()
    path = desired
    # Never overwrite a different card that happens to share a title. Checking
    # only the desired path keeps bulk deterministic imports O(n), not O(n²).
    if desired.exists():
        try:
            collision = load(desired)
        except CardError:
            collision = None
        if collision is None or collision.id != card.id:
            path = desired.with_name(f"{desired.stem}-{card.id[:8].lower()}.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(card), encoding="utf-8")
    return path


def iter_cards(wiki_root: Path):
    """Yield (path, Card) for every card file; review/ is excluded (unapproved)."""
    for p in sorted(wiki_root.rglob("*.md")):
        rel = p.relative_to(wiki_root)
        if rel.parts and rel.parts[0] in {"review", ".git", ".obsidian"}:
            continue
        if (rel.name in {"index.md", "log.md", "schema.md", "brief.md", "map.md"}
                or rel.name.startswith("log-")):  # per-device audit logs (ADR-019)
            continue  # regenerable artifacts, not cards (map.md: SPEC §7a.2)
        try:
            yield p, load(p)
        except CardError:
            continue  # non-card markdown (e.g. hand notes) is legal in the wiki
