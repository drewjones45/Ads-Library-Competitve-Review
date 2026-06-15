"""Google Ads Transparency Center (ATC) adapter.

Pulls an advertiser's current ads from Google ATC. Google exposes no public API,
so this mirrors `MetaAdsAdapter`'s hybrid design:

  1. API path (`_fetch_api`) — a paid third-party provider (SerpApi's
     `google_ads_transparency_center` engine) when `SERPAPI_API_KEY` (or the
     generic `GOOGLE_ATC_API_KEY`) is set. This is the supported route.
  2. Scrape path (`_scrape_with_browser`) — a best-effort Playwright scrape of
     ATC's internal RPC, isolated in `google_ads_scrape.py`. Fallback only.

`method` selects: 'api' | 'scrape' | 'auto' (default). 'auto' tries the API and
falls back to scraping when no key is set — exactly like the Meta adapter's
graph→scrape fallback. Degrades to a clean `IngestResult(ok=False, ...)` (a skip
row in the ingest report, not a crash) when neither a key nor Playwright exists.

Output ads are normalized to a superset of `_normalize_graph_ad`'s shape so the
runner/upsert path is shared. Google creative ids are prefixed `g_` because
`ads.ad_archive_id` is globally UNIQUE and must not collide with a Meta id.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from typing import Any

import httpx

from ..storage import asset_path, content_hash
from .base import Adapter, IngestResult
from .meta_ads import _is_currently_active

log = logging.getLogger("intel.google_ads")

SERPAPI_ENDPOINT = "https://serpapi.com/search.json"


def _api_key() -> str | None:
    return os.environ.get("SERPAPI_API_KEY") or os.environ.get("GOOGLE_ATC_API_KEY")


# SerpApi's ATC engine expects Google geo-target *codes* (e.g. 2840 = United
# States), NOT the 2-letter country code the ATC website URL uses — passing "US"
# returns an "Unsupported `US` region parameter" error. Map the common ones;
# pass through anything already numeric; return None (→ omit region = search
# anywhere) for unknown values. The scrape path keeps the 2-letter code because
# the ATC website URL (adstransparency.google.com/...?region=US) accepts it.
_ATC_API_REGION = {
    "US": "2840", "USA": "2840", "GB": "2826", "UK": "2826",
    "CA": "2124", "AU": "2036", "DE": "2276", "FR": "2250",
}


def _atc_api_region(country: str | None) -> str | None:
    if not country:
        return None
    c = country.strip()
    if c.isdigit():
        return c
    return _ATC_API_REGION.get(c.upper())


class GoogleAdsAdapter(Adapter):
    """Pulls an advertiser's ATC ads (image + text) via API-first, scrape-fallback."""

    source_type = "google_ads"

    # ---- normalization (shared by both paths) ----------------------------------

    def _normalize_creative(self, raw: dict[str, Any], *, raw_source: str) -> dict[str, Any] | None:
        """Map a 'raw creative' dict (from either path) to the canonical ad shape.

        SerpApi's ATC list gives only previews/links, not parsed copy: a *text* ad
        comes back as a rendered preview image (the search ad as shown), and an
        *image*/Shopping ad's real thumbnails + product snippets live behind the
        per-creative details endpoint. So headlines/descriptions are usually empty
        here — the text ad's copy is read off its preview image by the analyzer
        (vision), and image ads carry product `snippets` as their body text."""
        cid = str(raw.get("creative_id") or "").strip()
        if not cid:
            return None
        archive_id = cid if cid.startswith("g_") else f"g_{cid}"
        fmt = (raw.get("ad_format") or "unknown").lower()
        headlines = [str(h).strip() for h in (raw.get("headlines") or []) if str(h).strip()]
        descriptions = [str(d).strip() for d in (raw.get("descriptions") or []) if str(d).strip()]
        snippets = [str(s).strip() for s in (raw.get("snippets") or []) if str(s).strip()]
        body_parts = headlines + descriptions or snippets
        last_shown = raw.get("last_shown")
        return {
            "ad_archive_id": archive_id,
            "competitor_id": self.competitor.id,
            "source": "google",
            "ad_format": fmt,
            "page_name": raw.get("advertiser_name") or self.source.page_name,
            "page_id": self.source.advertiser_id,
            "start_date": raw.get("first_shown"),
            "end_date": last_shown,
            "is_active_inferred": _is_currently_active(last_shown),
            "total_days_shown": raw.get("total_days_shown"),
            "body_text": "\n".join(body_parts) if body_parts else None,
            "link_title": headlines[0] if headlines else None,
            # The ATC creative URL is the canonical, user-shareable link (the list
            # `link` is just a fletch render preview), so use it for the CTA.
            "link_url": raw.get("details_link") or raw.get("link_url"),
            "publisher_platforms": ["google"],
            "regions": raw.get("regions") or [],
            "headlines": headlines,
            "descriptions": descriptions,
            "snippets": snippets,
            "creative_image_urls": raw.get("image_urls") or [],
            "text_preview_url": raw.get("text_preview_url"),
            "creative_video_urls": raw.get("video_urls") or [],
            "raw_source": raw_source,
        }

    def _download_images(self, ad: dict[str, Any]) -> None:
        """Download a creative's assets to data/creative/<comp>/<archive_id>/.
        Google creative CDNs are public, so a plain httpx GET works (no session
        needed, unlike Meta's fbcdn). Best-effort: failures are logged, not raised.

        - Image/Shopping ads: their thumbnails (`creative_image_urls`) become the
          `local_creative_paths` the runner stores as `image` creatives.
        - Text ads: the rendered search-ad preview (`text_preview_url`) is saved to
          `text_preview_image` — the runner points the `text_ad` creative's sidecar
          at it, and the analyzer reads the copy off it (vision)."""
        with httpx.Client(timeout=20.0, follow_redirects=True) as client:
            def _dl(url: str, stem: str) -> str | None:
                try:
                    r = client.get(url)
                    if r.status_code != 200 or len(r.content) < 1_500:
                        return None
                    ct = (r.headers.get("content-type") or "").lower()
                    ext = ".png" if "png" in ct else ".webp" if "webp" in ct else ".jpg"
                    p = asset_path("creative", self.competitor.id, ad["ad_archive_id"], f"{stem}{ext}")
                    p.write_bytes(r.content)
                    return str(p)
                except Exception as e:
                    log.debug("google asset download failed %s: %s", url, e)
                    return None

            paths: list[str] = []
            for i, u in enumerate(ad.get("creative_image_urls") or []):
                p = _dl(u, f"image_{i}")
                if p:
                    paths.append(p)
            if paths:
                ad["local_creative_paths"] = paths
            if ad.get("text_preview_url"):
                p = _dl(ad["text_preview_url"], "text_preview")
                if p:
                    ad["text_preview_image"] = p

    # ---- API path (SerpApi) ----------------------------------------------------

    def _resolve_advertiser_id(self, client: httpx.Client, key: str) -> str | None:
        """Use the configured advertiser_id, or resolve it from page_name via a
        text search. Best-effort: returns None if it can't be determined."""
        if self.source.advertiser_id:
            return self.source.advertiser_id
        if not self.source.page_name:
            return None
        try:
            r = client.get(SERPAPI_ENDPOINT, params={
                "engine": "google_ads_transparency_center",
                "text": self.source.page_name,
                "api_key": key,
            }, timeout=30.0)
            r.raise_for_status()
            data = r.json()
            advertisers = data.get("advertisers") or []
            if advertisers:
                return advertisers[0].get("advertiser_id") or advertisers[0].get("id")
            creatives = data.get("ad_creatives") or []
            if creatives:
                return creatives[0].get("advertiser_id")
        except Exception as e:
            log.debug("advertiser resolve failed: %s", e)
        return None

    def _fetch_api(self) -> list[dict[str, Any]]:
        key = _api_key()
        if not key:
            raise RuntimeError(
                "SERPAPI_API_KEY / GOOGLE_ATC_API_KEY not set — set one in .env "
                "to use the API path, or rely on the Playwright scrape fallback."
            )
        out: list[dict[str, Any]] = []
        with httpx.Client(timeout=30.0) as client:
            advertiser_id = self._resolve_advertiser_id(client, key)
            if not advertiser_id:
                raise RuntimeError("could not resolve a Google advertiser_id (set advertiser_id in config)")
            params: dict[str, Any] = {
                "engine": "google_ads_transparency_center",
                "advertiser_id": advertiser_id,
                "api_key": key,
            }
            region = _atc_api_region(self.source.country)
            if region:
                params["region"] = region
            max_pages = max(1, int(os.environ.get("GOOGLE_ATC_MAX_PAGES", "6")))
            raws: list[dict[str, Any]] = []
            for _ in range(max_pages):  # paginate politely
                r = client.get(SERPAPI_ENDPOINT, params=params)
                r.raise_for_status()
                data = r.json()
                for item in data.get("ad_creatives") or []:
                    raws.append(_serpapi_to_raw(item))
                nxt = (data.get("serpapi_pagination") or {}).get("next_page_token") or \
                      (data.get("pagination") or {}).get("next_page_token")
                if not nxt or len(raws) >= 500:
                    break
                params = {**params, "next_page_token": nxt}

            # Enrich: the list response carries no real assets. Text ads get a
            # rendered preview image inline; image/Shopping ads need a per-creative
            # details call for their product thumbnails + snippets. Cap the (paid)
            # details calls per run.
            max_details = max(0, int(os.environ.get("GOOGLE_ATC_MAX_DETAILS", "80")))
            details_done = 0
            for raw in raws:
                fmt = (raw.get("ad_format") or "").lower()
                if fmt == "text":
                    if raw.get("list_image"):
                        raw["text_preview_url"] = raw["list_image"]
                elif fmt == "image":
                    if details_done < max_details:
                        imgs, snips = self._fetch_details(client, key, advertiser_id, raw["creative_id"])
                        details_done += 1
                        # One representative thumbnail per Shopping ad (the product
                        # snippets below preserve the full product list); keeps the
                        # creative gallery + analysis volume sane.
                        raw["image_urls"] = imgs[:1]
                        raw["snippets"] = snips
                    if not raw.get("image_urls") and raw.get("list_image"):
                        raw["image_urls"] = [raw["list_image"]]
                # video: store the ad row only (no still asset / YouTube skip).
                norm = self._normalize_creative(raw, raw_source="serpapi")
                if norm:
                    out.append(norm)
        return out

    def _fetch_details(
        self, client: httpx.Client, key: str, advertiser_id: str, creative_id: str
    ) -> tuple[list[str], list[str]]:
        """Per-creative details (image/Shopping ads): return (image_urls, snippets).
        Best-effort — never raises; an empty result just means no enrichment."""
        try:
            params: dict[str, Any] = {
                "engine": "google_ads_transparency_center_ad_details",
                "advertiser_id": advertiser_id,
                "creative_id": creative_id,
                "api_key": key,
            }
            region = _atc_api_region(self.source.country)
            if region:
                params["region"] = region
            r = client.get(SERPAPI_ENDPOINT, params=params, timeout=30.0)
            r.raise_for_status()
            data = r.json()
            images: list[str] = []
            snippets: list[str] = []
            for it in data.get("ad_creatives") or []:
                if it.get("image"):
                    images.append(it["image"])
                if it.get("snippet"):
                    snippets.append(str(it["snippet"]).strip())
            return images, snippets
        except Exception as e:
            log.debug("ad details fetch failed for %s: %s", creative_id, e)
            return [], []

    # ---- scrape path -----------------------------------------------------------

    def _scrape_with_browser(self) -> list[dict[str, Any]]:
        if not self.source.advertiser_id:
            raise RuntimeError(
                "google ads scrape requires source.advertiser_id (the ATC 'AR…' id). "
                "Find it at adstransparency.google.com → click the advertiser → copy "
                "the AR… segment from /advertiser/AR…?region=US."
            )
        from .google_ads_scrape import scrape_advertiser_ads  # deferred: needs playwright
        from ..config import DATA_DIR
        asset_dir = DATA_DIR / "creative" / self.competitor.id
        raws = scrape_advertiser_ads(
            self.source.advertiser_id,
            region=self.source.country,
            asset_dir=asset_dir,
            headless=True,
        )
        out: list[dict[str, Any]] = []
        for raw in raws:
            norm = self._normalize_creative(raw, raw_source="atc_scrape")
            if norm:
                out.append(norm)
        return out

    # ---- entry point -----------------------------------------------------------

    def fetch(self) -> IngestResult:
        method = (self.source.method or "auto").lower()
        try:
            if method == "scrape":
                ads = self._scrape_with_browser()
                source_label = "atc_scrape"
            elif method == "api":
                ads = self._fetch_api()
                source_label = "serpapi"
            else:  # 'auto' — API first, fall back to scrape only when no key
                try:
                    ads = self._fetch_api()
                    source_label = "serpapi"
                except RuntimeError:
                    ads = self._scrape_with_browser()
                    source_label = "atc_scrape"
        except ImportError as e:
            return IngestResult(
                competitor_id=self.competitor.id,
                source_key=self.source.stable_key(),
                ok=False,
                error=f"google ads: no API key and Playwright not installed ({e})",
            )
        except Exception as e:
            return IngestResult(
                competitor_id=self.competitor.id,
                source_key=self.source.stable_key(),
                ok=False,
                error=f"google ads fetch failed ({method}): {e}",
            )

        # Download assets now (URLs from API/scrape, public CDN): image-ad
        # thumbnails and text-ad rendered previews.
        for ad in ads:
            if ad.get("creative_image_urls") or ad.get("text_preview_url"):
                self._download_images(ad)

        archive_ids = sorted([a["ad_archive_id"] for a in ads if a.get("ad_archive_id")])
        h = content_hash(json.dumps(archive_ids))

        stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
        slug = re.sub(
            r"[^a-z0-9]+", "_",
            (self.source.advertiser_id or self.source.page_name or "unknown").lower(),
        ).strip("_")
        raw_p = asset_path("raw", self.competitor.id, "google_ads", f"{slug}__{stamp}.json")
        raw_p.write_text(json.dumps(ads, indent=2, default=str), encoding="utf-8")

        fmt_counts: dict[str, int] = {}
        for a in ads:
            fmt_counts[a.get("ad_format", "unknown")] = fmt_counts.get(a.get("ad_format", "unknown"), 0) + 1

        parsed = {
            "ad_count": len(ads),
            "active_archive_ids": archive_ids,
            "advertiser_id": self.source.advertiser_id,
            "page_name": self.source.page_name,
            "country": self.source.country,
            "source": source_label,
            "format_counts": fmt_counts,
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


def _epoch_to_date(v: Any) -> str | None:
    """Convert a SerpApi unix-seconds timestamp (e.g. 1769542598) to 'YYYY-MM-DD'
    so it matches the Meta path's date strings (which `_is_currently_active` and
    the length-in-market math expect). Pass through strings; None on failure."""
    if v is None:
        return None
    if isinstance(v, str):
        return v
    try:
        return datetime.utcfromtimestamp(int(v)).strftime("%Y-%m-%d")
    except Exception:
        return None


def _serpapi_to_raw(item: dict[str, Any]) -> dict[str, Any]:
    """Map a SerpApi `ad_creatives[]` LIST item to the common 'raw creative' shape.

    The list response is sparse: `ad_creative_id`, `format`, `advertiser`, unix
    `first_shown`/`last_shown`, `total_days_shown`, a `details_link` (the ATC
    creative URL) and — for *text* ads only — `image` (the rendered preview).
    Real headlines/descriptions and image-ad thumbnails are NOT here; the caller
    enriches image ads via the details endpoint and reads text-ad copy off the
    preview image. Defensive — field names drift, so every read is via `.get()`."""
    fmt = (item.get("format") or item.get("ad_format") or "").lower()
    regions = item.get("regions") or item.get("region") or []
    if isinstance(regions, str):
        regions = [regions]
    return {
        "creative_id": (
            item.get("ad_creative_id") or item.get("creative_id")
            or item.get("ad_id") or item.get("id")
        ),
        "ad_format": fmt or "unknown",
        "advertiser_name": item.get("advertiser") or item.get("advertiser_name"),
        "first_shown": _epoch_to_date(item.get("first_shown") or item.get("first_shown_date")),
        "last_shown": _epoch_to_date(item.get("last_shown") or item.get("last_shown_date")),
        "total_days_shown": item.get("total_days_shown"),
        "list_image": item.get("image"),          # rendered preview (text ads)
        "details_link": item.get("details_link"),  # ATC creative URL
        "serpapi_details_link": item.get("serpapi_details_link"),
        "link_url": item.get("link"),              # fletch render preview (raw only)
        "headlines": [],
        "descriptions": [],
        "snippets": [],
        "image_urls": [],
        "video_urls": [],
        "regions": regions,
    }
