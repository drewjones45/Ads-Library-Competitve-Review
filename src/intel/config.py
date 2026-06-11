from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
DATA_DIR = Path(os.environ.get("INTEL_DATA_DIR", ROOT / "data")).resolve()
DB_PATH = Path(os.environ.get("INTEL_DB_PATH", DATA_DIR / "intel.db")).resolve()

SOURCE_TYPES = Literal["website", "meta_ads", "rss", "retailer", "amazon_brand_store"]


@dataclass
class Source:
    type: str
    cadence: str = "daily"
    url: str | None = None
    page_id: str | None = None
    page_name: str | None = None
    country: str = "US"
    watch: list[str] = field(default_factory=list)
    method: str = "auto"  # for meta_ads: 'auto' | 'graph_api' | 'scrape'
    # Phase B1: Amazon Brand Store fields. Only used when type == 'amazon_brand_store'.
    store_url: str | None = None       # e.g. https://www.amazon.com/stores/page/<UUID>
    max_subpages: int = 10             # discovery cap; set to 0 for landing-only
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Source":
        known = {
            "type", "cadence", "url", "page_id", "page_name", "country",
            "watch", "method", "store_url", "max_subpages",
        }
        extra = {k: v for k, v in d.items() if k not in known}
        return cls(
            type=d["type"],
            cadence=d.get("cadence", "daily"),
            url=d.get("url"),
            page_id=d.get("page_id"),
            page_name=d.get("page_name"),
            country=d.get("country", "US"),
            watch=d.get("watch", []) or [],
            method=d.get("method", "auto"),
            store_url=d.get("store_url"),
            max_subpages=int(d.get("max_subpages", 10)),
            extra=extra,
        )

    def stable_key(self) -> str:
        """Deterministic ID — survives renames inside the same competitor."""
        parts = [self.type]
        if self.url:
            parts.append(self.url)
        elif self.store_url:
            parts.append(self.store_url)
        elif self.page_id:
            parts.append(f"page:{self.page_id}")
        elif self.page_name:
            parts.append(f"page_name:{self.page_name}:{self.country}")
        return "|".join(parts)


@dataclass
class Competitor:
    id: str
    name: str
    vertical: str = ""
    priority: str = "medium"
    sources: list[Source] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Competitor":
        return cls(
            id=d["id"],
            name=d["name"],
            vertical=d.get("vertical", ""),
            priority=d.get("priority", "medium"),
            sources=[Source.from_dict(s) for s in d.get("sources", [])],
        )


@dataclass
class Settings:
    weights: dict[str, float]
    noise_filters: dict[str, Any]
    llm: dict[str, Any]

    @property
    def reasoning_model(self) -> str:
        return os.environ.get("INTEL_MODEL", self.llm.get("reasoning_model", "claude-opus-4-7"))

    @property
    def vision_model(self) -> str:
        return os.environ.get("INTEL_VISION_MODEL", self.llm.get("vision_model", "claude-sonnet-4-6"))

    @property
    def cheap_model(self) -> str:
        return self.llm.get("cheap_model", "claude-haiku-4-5-20251001")


def load_competitors(path: Path | None = None) -> list[Competitor]:
    path = path or Path(os.environ.get("INTEL_COMPETITORS_FILE", CONFIG_DIR / "competitors.yaml"))
    with open(path) as f:
        raw = yaml.safe_load(f)
    return [Competitor.from_dict(c) for c in raw.get("competitors", [])]


def load_settings(path: Path | None = None) -> Settings:
    path = path or (CONFIG_DIR / "settings.yaml")
    with open(path) as f:
        raw = yaml.safe_load(f)
    return Settings(
        weights=raw.get("weights", {}),
        noise_filters=raw.get("noise_filters", {}),
        llm=raw.get("llm", {}),
    )


def get_competitor(comp_id: str) -> Competitor | None:
    for c in load_competitors():
        if c.id == comp_id:
            return c
    return None
