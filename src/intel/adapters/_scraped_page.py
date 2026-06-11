"""Shared dataclass for adapters that capture pages via a headless browser.

Used by both the Amazon Brand Store and Website adapters. Runner code
iterates `result.extras["pages"]` and reads attributes directly, so any new
adapter that scrapes pages should populate one of these and put a list of
them in `IngestResult.extras["pages"]`.

`page_key` is a stable identifier for the page within the source:
  - Amazon Brand Store: the page UUID parsed from `/stores/page/<UUID>`
  - Website: a domain-slug derived from the URL
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ScrapedPage:
    page_key: str
    url: str
    title: str | None
    hero_text: str | None
    visible_text: str
    image_urls: list[str]
    image_local_paths: list[str]
    subpage_links: list[str]
    screenshot_path: str | None
    html_path: str | None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_key": self.page_key,
            "url": self.url,
            "title": self.title,
            "hero_text": self.hero_text,
            "image_count": len(self.image_local_paths),
            "subpage_link_count": len(self.subpage_links),
            "screenshot_path": self.screenshot_path,
            "error": self.error,
        }
