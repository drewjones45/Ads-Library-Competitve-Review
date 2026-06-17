"""iSpot.tv TV-ads adapter.

Pulls a competitor's national-TV spots from iSpot.tv. Mirrors `GoogleAdsAdapter`'s
hybrid design so the runner/upsert path is shared:

  1. API path (`_fetch_api`) — iSpot's enterprise API when `ISPOT_API_KEY` is set.
     This is the route that exposes per-ad spend / impressions / networks /
     dayparts. It requires a paid iSpot contract; until credentials exist the
     method is stubbed and raises, so `auto` cleanly falls back to scraping.
  2. Scrape path (`_scrape`) — parses iSpot's PUBLIC brand pages (no login). This
     yields the creative library + real brand-level media weight (National
     Airings, Spend/Airing Rank, Total Creatives) and, per ad, the downloadable
     spot video (`og:video`) and thumbnail. iSpot MASKS dollar spend, impressions,
     and SOV for anonymous users (rendered as `$000,000` / `00.00%`); those parse
     to None and are an API-only follow-up.

`method` selects 'api' | 'scrape' | 'auto' (default). Output ads are normalized to
the same shape `upsert_ad` consumes; `source="tv"` tags every row and the
`ad_archive_id` is prefixed `tv_` (the `ads.ad_archive_id` UNIQUE constraint spans
all platforms). In-market tracking is free: a spot newly appearing on the brand
page is a new `ad_archive_id`, which the runner records in `report.new_ads`.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from typing import Any

import httpx
from bs4 import BeautifulSoup

from ..storage import asset_path, content_hash
from .base import Adapter, IngestResult

log = logging.getLogger("intel.ispot_tv")

ISPOT_BASE = "https://www.ispot.tv"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36"
)
# /ad/<id>/<slug> — the two-segment form is a real spot; one-segment links
# (/ad/top-commercials, /ad/top-spenders) are nav and must be excluded.
_AD_HREF = re.compile(r"^/ad/([A-Za-z0-9_]+)/([a-z0-9-]+)$")
# Brand-scorecard labels → the value rendered immediately after them.
_SCORECARD_LABELS = [
    "National TV Spend", "Impressions", "National Airings", "Spend Rank",
    "Airing Rank", "Linear SOV", "Streaming SOV", "Total Creatives",
]


def _api_key() -> str | None:
    return os.environ.get("ISPOT_API_KEY")


def _slugify(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def _num(text: str) -> int | float | None:
    """Parse the numeric value out of a scorecard fragment, or None when iSpot
    has masked it for anonymous users (`$000,000`, `000,000`, `00.00%`, `000`).
    A value is 'masked' when its only digits are zeros."""
    if not text:
        return None
    m = re.search(r"[\d][\d,]*\.?\d*", text)
    if not m:
        return None
    raw = m.group(0).replace(",", "")
    if not raw or set(raw) <= {"0", "."}:
        return None  # masked placeholder
    try:
        return float(raw) if "." in raw else int(raw)
    except ValueError:
        return None


class TvAdsAdapter(Adapter):
    """Pulls a brand's iSpot TV spots (API-first, public-scrape fallback)."""

    source_type = "tv_ads"

    # ---- normalization ---------------------------------------------------------

    def _normalize(self, spot: dict[str, Any], brand_metrics: dict[str, Any]) -> dict[str, Any]:
        sid = str(spot.get("ispot_id") or "").strip()
        title = spot.get("title") or ""
        return {
            "ad_archive_id": f"tv_{sid}",
            "competitor_id": self.competitor.id,
            "source": "tv",
            "ad_format": "video",
            "page_name": self.source.page_name,
            "page_id": self.source.ispot_brand_id,
            # iSpot's public pages don't expose per-ad first/last-air dates, so we
            # can't infer start/end; a spot currently listed is in-market.
            "start_date": None,
            "end_date": None,
            "is_active_inferred": True,
            "body_text": title or None,
            "link_url": spot.get("ispot_url"),
            "publisher_platforms": ["tv"],
            # everything below rides in raw_json via upsert_ad(json.dumps(ad)):
            "ispot_id": sid,
            "title": title,
            "thumbnail_url": spot.get("thumbnail_url"),
            "video_url": spot.get("video_url"),
            "advertiser": spot.get("advertiser"),
            "products": spot.get("products"),
            "duration_sec": spot.get("duration_sec"),
            "ispot_url": spot.get("ispot_url"),
            "brand_metrics": brand_metrics,
            # local asset paths populated by _download_creatives:
            "local_creative_paths": [],
        }

    # ---- scrape path -----------------------------------------------------------

    def _brand_html(self, client: httpx.Client) -> str:
        """Fetch the brand page. The id-only URL redirects to the canonical
        id+slug form (with follow_redirects), so it's the reliable first try;
        the explicit slug is a fallback."""
        bid = self.source.ispot_brand_id
        slug = _slugify(self.source.page_name)
        for url in (f"{ISPOT_BASE}/brands/{bid}",
                    f"{ISPOT_BASE}/brands/{bid}/{slug}" if slug else None):
            if not url:
                continue
            r = client.get(url)
            if r.status_code == 200 and r.text:
                return r.text
        raise RuntimeError(f"iSpot brand page not reachable for id={bid}")

    def _parse_brand(self, html: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        # Spots: dedupe by id, in document order (recent/featured first).
        spots: dict[str, dict[str, Any]] = {}
        for a in soup.select('a[href^="/ad/"]'):
            m = _AD_HREF.match(a.get("href", ""))
            if not m:
                continue
            sid, aslug = m.group(1), m.group(2)
            if sid in spots:
                continue
            spots[sid] = {
                "ispot_id": sid,
                "title": (a.get("title") or a.get_text(strip=True) or "").strip(),
                "thumbnail_url": f"https://images-cdn.ispot.tv/ad/{sid}/default-large.jpg",
                "ispot_url": f"{ISPOT_BASE}/ad/{sid}/{aslug}",
            }
        # Brand scorecard. National Airings / ranks / total creatives are real;
        # spend / impressions / SOV are masked for anonymous users (→ None).
        metrics: dict[str, Any] = {}
        for label in _SCORECARD_LABELS:
            el = soup.find(string=re.compile(re.escape(label)))
            val = None
            if el:
                blk = el.find_parent()
                for _ in range(2):
                    if blk and blk.parent:
                        blk = blk.parent
                frag = re.sub(r"\s+", " ", blk.get_text(" ", strip=True))
                # strip the label itself, then parse the trailing value
                val = _num(frag.split(label, 1)[-1])
            key = label.lower().replace(" ", "_").replace("tv_", "")
            metrics[key] = val
        return list(spots.values()), metrics

    def _scrape_ad_detail(self, client: httpx.Client, spot: dict[str, Any]) -> None:
        """Enrich one spot from its /ad/<id> page: downloadable mp4 + duration.
        Best-effort — never raises; missing fields stay None."""
        try:
            r = client.get(spot["ispot_url"])
            if r.status_code != 200:
                return
            soup = BeautifulSoup(r.text, "html.parser")

            def _meta(prop: str) -> str | None:
                el = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
                return el.get("content") if el else None

            spot["video_url"] = _meta("og:video:url") or _meta("og:video")
            dur = _meta("video:duration")
            if dur and str(dur).isdigit():
                spot["duration_sec"] = int(dur)
        except Exception as e:
            log.debug("iSpot ad-detail fetch failed for %s: %s", spot.get("ispot_id"), e)

    def _download_creatives(self, client: httpx.Client, ad: dict[str, Any]) -> None:
        """Download the spot thumbnail (the analyzable creative asset). The full
        mp4 (`video_url`) is captured in raw_json for a future video-analysis pass
        / the API path, but not downloaded here."""
        url = ad.get("thumbnail_url")
        if not url:
            return
        try:
            r = client.get(url)
            if r.status_code != 200 or len(r.content) < 1_500:
                return
            ct = (r.headers.get("content-type") or "").lower()
            ext = ".png" if "png" in ct else ".webp" if "webp" in ct else ".jpg"
            p = asset_path("creative", self.competitor.id, ad["ad_archive_id"], f"thumb{ext}")
            p.write_bytes(r.content)
            ad["local_creative_paths"] = [str(p)]
        except Exception as e:
            log.debug("iSpot thumbnail download failed %s: %s", url, e)

    def _scrape(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if not self.source.ispot_brand_id:
            raise RuntimeError(
                "iSpot scrape requires source.ispot_brand_id (the id from "
                "ispot.tv/brands/<id>/<slug>). Resolve it by searching the brand "
                "on ispot.tv and copying the id segment."
            )
        max_spots = max(1, int(os.environ.get("ISPOT_MAX_SPOTS", "40")))
        max_details = max(0, int(os.environ.get("ISPOT_MAX_DETAILS", "12")))
        with httpx.Client(timeout=30.0, follow_redirects=True,
                          headers={"User-Agent": _UA}) as client:
            spots, brand_metrics = self._parse_brand(self._brand_html(client))
            if not spots:
                # Some iSpot brand pages use a marketing "hub" template that lists
                # no individual spots (even when rendered) — those need the iSpot
                # API. Surface it rather than silently reporting "no TV ads".
                log.warning(
                    "iSpot brand %s (%s) returned a page with no scrapeable spot "
                    "grid — likely a hub-template brand; needs the iSpot API.",
                    self.source.ispot_brand_id, self.source.page_name,
                )
            spots = spots[:max_spots]
            for i, spot in enumerate(spots):
                if i < max_details:
                    self._scrape_ad_detail(client, spot)
            ads = [self._normalize(s, brand_metrics) for s in spots]
            for ad in ads:
                self._download_creatives(client, ad)
        return ads, brand_metrics

    # ---- API path (stub — enterprise contract required) ------------------------

    def _fetch_api(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if not _api_key():
            raise RuntimeError("ISPOT_API_KEY not set — using the public-page scrape path.")
        # The iSpot enterprise API (per-ad spend/impressions/airings/networks/
        # dayparts) is not yet wired. The adapter is structured so this method can
        # be filled in without touching the runner/dashboard once credentials and
        # the endpoint contract are available.
        raise RuntimeError("iSpot API path not implemented yet — falling back to scrape.")

    # ---- entry point -----------------------------------------------------------

    def fetch(self) -> IngestResult:
        method = (self.source.method or "auto").lower()
        try:
            if method == "scrape":
                ads, brand_metrics = self._scrape()
                source_label = "ispot_scrape"
            elif method == "api":
                ads, brand_metrics = self._fetch_api()
                source_label = "ispot_api"
            else:  # auto — API when a key is set, else scrape
                try:
                    ads, brand_metrics = self._fetch_api()
                    source_label = "ispot_api"
                except RuntimeError:
                    ads, brand_metrics = self._scrape()
                    source_label = "ispot_scrape"
        except Exception as e:
            return IngestResult(
                competitor_id=self.competitor.id,
                source_key=self.source.stable_key(),
                ok=False,
                error=f"tv ads fetch failed ({method}): {e}",
            )

        archive_ids = sorted([a["ad_archive_id"] for a in ads if a.get("ad_archive_id")])
        h = content_hash(json.dumps(archive_ids))

        stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
        slug = _slugify(self.source.ispot_brand_id or self.source.page_name or "unknown")
        raw_p = asset_path("raw", self.competitor.id, "tv_ads", f"{slug}__{stamp}.json")
        raw_p.write_text(json.dumps(ads, indent=2, default=str), encoding="utf-8")

        parsed = {
            "ad_count": len(ads),
            "active_archive_ids": archive_ids,
            "ispot_brand_id": self.source.ispot_brand_id,
            "page_name": self.source.page_name,
            "country": self.source.country,
            "source": source_label,
            "brand_metrics": brand_metrics,
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
