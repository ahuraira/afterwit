"""Normalized ingestion events. SPEC §6 contract."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class Event:
    source_path: str
    lines: tuple[int, int]
    project: str
    ts: str | None
    role: str
    kind: str
    text: str
    meta: dict[str, Any] = field(default_factory=dict)

    def asdict(self) -> dict[str, Any]:
        return asdict(self)
