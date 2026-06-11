from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..config import Competitor, Source


@dataclass
class IngestResult:
    """Outcome of a single source ingestion."""
    competitor_id: str
    source_key: str
    ok: bool
    changed: bool = False
    content_hash: str = ""
    raw_path: str | None = None
    parsed: dict[str, Any] = field(default_factory=dict)
    change_summary: str | None = None
    severity: float = 0.0
    error: str | None = None
    # Extras that flow into downstream processing (e.g. new ads to enrich).
    extras: dict[str, Any] = field(default_factory=dict)


class Adapter(ABC):
    """A source-type adapter. One class per source type; one instance per (competitor, source)."""

    source_type: str = ""

    def __init__(self, competitor: Competitor, source: Source):
        self.competitor = competitor
        self.source = source

    @abstractmethod
    def fetch(self) -> IngestResult:
        """Pull current state from the source, persist raw payload, return result."""
        ...
