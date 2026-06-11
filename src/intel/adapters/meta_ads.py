from __future__ import annotations

import json
import os
import re
from collections import Counter
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import quote_plus

import httpx

from ..storage import asset_path, content_hash
from .base import Adapter, IngestResult


META_GRAPH = "https://graph.facebook.com/v19.0/ads_archive"

# Fields the Graph API can return for the ad_archive endpoint.
GRAPH_FIELDS = [
    "id",
    "ad_creation_time",
    "ad_creative_bodies",
    "ad_creative_link_captions",
    "ad_creative_link_descriptions",
    "ad_creative_link_titles",
    "ad_delivery_start_time",
    "ad_delivery_stop_time",
    "ad_snapshot_url",
    "currency",
    "page_id",
    "page_name",
    "publisher_platforms",
    "languages",
    "target_locations",
    "impressions",
    "spend",
]


def _name_similarity(a: str, b: str) -> float:
    """Crude normalized similarity — lowercase, strip punctuation."""
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


def _resolve_best_page(
    raw_ads: list[dict[str, Any]], requested_name: str, *, similarity_threshold: float = 0.8
) -> tuple[str | None, list[dict[str, Any]]]:
    """Pick the single Page whose name best matches `requested_name`, then
    return (page_id, ads_from_that_page_only). Defeats free-text search noise
    when ad copy mentions a brand the page doesn't belong to."""
    # Collect (page_id, page_name, count) — group by page_id since names vary.
    grouped: dict[str, tuple[str, int]] = {}
    for a in raw_ads:
        pid = a.get("page_id")
        pname = a.get("page_name") or ""
        if not pid:
            continue
        prev_name, prev_count = grouped.get(pid, (pname, 0))
        grouped[pid] = (prev_name or pname, prev_count + 1)
    if not grouped:
        return None, raw_ads

    # Score each candidate page: similarity weighted lightly by ad count
    # (popular brand page usually has more ads than a one-off spammer).
    best_pid: str | None = None
    best_score = -1.0
    for pid, (pname, count) in grouped.items():
        sim = _name_similarity(pname, requested_name)
        # heavy weight on name similarity; small tiebreak on volume
        score = sim + (0.05 * min(count, 20) / 20)
        if score > best_score:
            best_score, best_pid = score, pid
    if best_pid is None:
        return None, raw_ads

    best_name = grouped[best_pid][0]
    if _name_similarity(best_name, requested_name) < similarity_threshold:
        # No page is close enough — refuse to guess; return unfiltered with flag.
        return None, raw_ads
    kept = [a for a in raw_ads if a.get("page_id") == best_pid]
    return best_pid, kept


def _is_currently_active(stop_time: str | None, now: datetime | None = None) -> bool:
    """An ad is currently active iff its delivery stop time is null or in the future.
    The API's own ad_active_status flag is unreliable for US commercial ads — this
    inference matches what the public Ad Library web UI shows."""
    if not stop_time:
        return True
    try:
        # Stop times come in 'YYYY-MM-DD' or full ISO. Treat date-only as end-of-day UTC.
        st = stop_time.strip()
        if len(st) == 10:
            dt = datetime.fromisoformat(st + "T23:59:59+00:00")
        else:
            dt = datetime.fromisoformat(st.replace("Z", "+00:00"))
        return dt > (now or datetime.now(dt.tzinfo))
    except Exception:
        return True  # if we can't parse, assume active


def _normalize_graph_ad(item: dict[str, Any], competitor_id: str) -> dict[str, Any]:
    """Map Graph API ad record into our canonical ad dict."""
    bodies = item.get("ad_creative_bodies") or []
    titles = item.get("ad_creative_link_titles") or []
    body_text = "\n".join(bodies) if bodies else None
    stop = item.get("ad_delivery_stop_time")
    return {
        "ad_archive_id": item.get("id"),
        "competitor_id": competitor_id,
        "page_id": item.get("page_id"),
        "page_name": item.get("page_name"),
        "start_date": item.get("ad_delivery_start_time"),
        "end_date": stop,
        "is_active_inferred": _is_currently_active(stop),
        "body_text": body_text,
        "link_title": titles[0] if titles else None,
        "link_url": (item.get("ad_creative_link_captions") or [None])[0],
        "publisher_platforms": item.get("publisher_platforms"),
        "snapshot_url": item.get("ad_snapshot_url"),
        "impressions_range": item.get("impressions"),
        "spend_range": item.get("spend"),
        "currency": item.get("currency"),
        "raw_source": "graph_api",
    }


class MetaAdsAdapter(Adapter):
    """Pulls current active ads for a Page from the Meta Ad Library.

    Two paths, in order of preference:

    1. Graph API (`/ads_archive`) — requires META_AD_LIBRARY_ACCESS_TOKEN env var
       and is the supported path. Note: the public token returns *only* ads about
       social issues/elections/politics by default. For commercial-only ads you
       must request elevated access OR fall through to (2).

    2. Headless-browser scrape via Playwright (extension point) — for commercial
       ads at https://www.facebook.com/ads/library/. Not wired by default to keep
       the MVP install light; install with `[browser]` extras and implement
       `_scrape_with_browser` to enable.

    The adapter records *every* ad seen, deduped on ad_archive_id; new-ad
    detection is handled downstream by comparing against the `ads` table.
    """

    source_type = "meta_ads"

    def _resolve_page_id(self, client: httpx.Client, token: str) -> str | None:
        """If only a page name was given, look it up. Cheap, idempotent."""
        if self.source.page_id:
            return self.source.page_id
        if not self.source.page_name:
            return None
        # Page search via Graph requires the page-search permission; many tokens
        # won't have it. We attempt and degrade gracefully.
        try:
            r = client.get(
                "https://graph.facebook.com/v19.0/pages/search",
                params={"q": self.source.page_name, "fields": "id,name", "access_token": token},
                timeout=20.0,
            )
            r.raise_for_status()
            data = r.json().get("data", [])
            if data:
                return data[0]["id"]
        except Exception:
            return None
        return None

    def _fetch_graph(self) -> list[dict[str, Any]]:
        token = os.environ.get("META_AD_LIBRARY_ACCESS_TOKEN")
        if not token:
            raise RuntimeError(
                "META_AD_LIBRARY_ACCESS_TOKEN not set — set in .env or call "
                "ingest_meta_ads with a token, or implement Playwright scrape."
            )
        results: list[dict[str, Any]] = []
        with httpx.Client(timeout=30.0) as client:
            page_id = self._resolve_page_id(client, token)
            # Graph wants list-valued params as JSON-encoded strings, not repeated
            # querystring keys. Pass them pre-serialized.
            # Use ad_active_status=ALL — the API's ACTIVE filter is unreliable for
            # US commercial ads (ads visible as "active" on the public Ad Library web
            # UI are often returned with active=false here). We infer active state on
            # our side from ad_delivery_stop_time (see _is_currently_active).
            params: dict[str, Any] = {
                "access_token": token,
                "ad_reached_countries": json.dumps([self.source.country]),
                "ad_active_status": "ALL",
                "ad_type": "ALL",
                "fields": ",".join(GRAPH_FIELDS),
                "limit": 100,
            }
            if page_id:
                params["search_page_ids"] = json.dumps([page_id])
            elif self.source.page_name:
                params["search_terms"] = self.source.page_name
            url: str | None = META_GRAPH
            while url:
                r = client.get(url, params=params if url == META_GRAPH else None)
                r.raise_for_status()
                payload = r.json()
                for item in payload.get("data", []):
                    results.append(item)
                url = (payload.get("paging") or {}).get("next")
                params = {}  # next URL embeds query
                if len(results) >= 500:
                    break  # safety cap
        return results

    def _normalize_and_filter(self, raw_ads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Apply per-page filtering then normalize Graph API records to our canonical shape."""
        if raw_ads and not self.source.page_id and self.source.page_name:
            _, kept = _resolve_best_page(raw_ads, self.source.page_name)
            raw_ads = kept
        return [_normalize_graph_ad(a, self.competitor.id) for a in raw_ads]

    def _scrape_with_browser(self) -> list[dict[str, Any]]:
        """Playwright-driven scrape of the public Meta Ad Library.

        Returns dicts in the same shape as `_normalize_graph_ad` so the downstream
        upsert/runner code doesn't care which source the ads came from. Images are
        downloaded to `data/creative/<competitor>/<ad_archive_id>/` and the local
        paths are attached to each ad under `local_creative_paths`.

        Requires a `page_id` on the source — the page_name fuzzy resolver only
        works against API results.
        """
        if not self.source.page_id:
            raise RuntimeError(
                "browser scrape requires source.page_id (the numeric Facebook Page ID). "
                "Find it via facebook.com/<brand> URL or the Ad Library web UI's view_all_page_id param."
            )
        from .meta_ads_scrape import scrape_page_ads
        from ..config import DATA_DIR
        asset_dir = DATA_DIR / "creative" / self.competitor.id
        scraped = scrape_page_ads(
            self.source.page_id,
            country=self.source.country,
            asset_dir=asset_dir,
        )
        # Re-shape: scraped ads use the same field names as _normalize_graph_ad
        # output (intentional). Just stamp the page_id we already knew.
        for ad in scraped:
            ad["page_id"] = self.source.page_id
            ad["competitor_id"] = self.competitor.id
        return scraped

    def fetch(self) -> IngestResult:
        method = (self.source.method or "auto").lower()
        try:
            if method == "scrape":
                ads = self._scrape_with_browser()
                source_label = "browser_scrape"
            elif method == "graph_api":
                raw = self._fetch_graph()
                ads = self._normalize_and_filter(raw)
                source_label = "graph_api"
            else:  # 'auto' — try graph, fall back to scrape only if no token at all
                try:
                    raw = self._fetch_graph()
                    ads = self._normalize_and_filter(raw)
                    source_label = "graph_api"
                except RuntimeError:
                    ads = self._scrape_with_browser()
                    source_label = "browser_scrape"
        except Exception as e:
            return IngestResult(
                competitor_id=self.competitor.id,
                source_key=self.source.stable_key(),
                ok=False,
                error=f"meta ads fetch failed ({method}): {e}",
            )

        resolved_page_id = self.source.page_id
        archive_ids = sorted([a["ad_archive_id"] for a in ads if a.get("ad_archive_id")])
        h = content_hash(json.dumps(archive_ids))

        stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
        slug = re.sub(
            r"[^a-z0-9]+", "_", (self.source.page_name or self.source.page_id or "unknown").lower()
        ).strip("_")
        raw_p = asset_path("raw", self.competitor.id, "meta_ads", f"{slug}__{stamp}.json")
        # Archive the normalized output; the on-disk JSON is the "raw" payload
        # for downstream replay regardless of which source method produced it.
        raw_p.write_text(json.dumps(ads, indent=2, default=str), encoding="utf-8")

        parsed = {
            "ad_count": len(ads),
            "active_archive_ids": archive_ids,
            "page_id": self.source.page_id or resolved_page_id,
            "page_name": self.source.page_name,
            "country": self.source.country,
            "source": source_label,
            "resolved_via": "configured_page_id" if self.source.page_id else (
                "best_page_name_match" if resolved_page_id else "unfiltered"
            ),
        }

        return IngestResult(
            competitor_id=self.competitor.id,
            source_key=self.source.stable_key(),
            ok=True,
            content_hash=h,
            raw_path=str(raw_p),
            parsed=parsed,
            extras={"ads": ads},
        )
