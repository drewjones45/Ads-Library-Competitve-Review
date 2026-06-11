"""Amazon Brand Store adapter.

Brand Stores are the brand-owned destinations Amazon hosts under
`/stores/page/<UUID>`. They contain hero imagery, category navigation,
shoppable product grids, and brand storytelling — none of which Meta Ads or
the brand's own website surface.

Amazon blocks plain HTTP fetches (503 on default UA). We use Playwright with
stealth-ish browser args + a realistic user agent. No proxy is required for
low-volume use (one brand, weekly cadence); document escalation path if 503s
appear in adapter logs.

This module ships in two layers:
  - scrape_brand_store(url, ...) — pure-function Playwright scrape, easy to
    test standalone via `intel test-amazon-store <url>`.
  - AmazonBrandStoreAdapter — Adapter ABC implementation: wires the scrape
    output into IngestResult so runner.py can persist it.

Phase B1 (this commit): landing-page only. Phase B2 will add sub-page
discovery + sequential visits with backoff.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from ._scraped_page import ScrapedPage
from .base import Adapter, IngestResult
from ..storage import asset_path


log = logging.getLogger("intel.amazon_brand_store")


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
# Args that reduce trivial bot fingerprints (Playwright sets navigator.webdriver=true
# by default; --disable-blink-features=AutomationControlled removes it).
STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
]

PAGE_UUID_RE = re.compile(r"/stores/page/([\w-]+)", re.I)
MIN_IMAGE_WIDTH = 200  # drop avatars/icons; keep hero/product imagery

# Amazon serves real brand-store assets from three CDN path families:
#   /images/I/<hash>...               — product photos
#   /images/S/abs-image-upload-na/    — brand-uploaded wordmarks/logos
#   /images/S/al-na-<hash>/           — brand-store hero/promo banners (managed creative slots)
# Everything else (especially /images/G/01/) is Amazon's own global UI chrome:
# nav sprites, Prime/Smile/Fresh logos, cart icons, follow-button sprites, etc.
# Allow-list these three paths to drop chrome at extract time.
_ASSET_PATH_KEEP_SUBSTRINGS = (
    "/images/I/",
    "/images/S/abs-image-upload-na/",
    "/images/S/al-na-",
)

# Belt-and-suspenders: explicit denylist for the chrome paths we know about,
# in case Amazon ever serves a non-allowlisted product CDN.
_CHROME_PATH_PATTERNS = (
    "/images/G/01/AmazonStores/",
    "/images/G/01/gno/",
    "/images/G/01/omaha/",
    "/images/G/01/prime/",
    "/images/G/01/follow/",
    "/images/G/01/Wisp/",
    "/images/G/01/nav/",
    "/images/G/01/AmazonUI",
)

# Alt-text blocklist (Amazon chrome typically has these labels).
_CHROME_ALT_TEXTS = frozenset({
    "amazon", "amazon prime", "prime", "amazon fresh", "fresh",
    "amazon pantry", "pantry", "whole foods", "wholefoods",
    "smile", "amazon smile", "amazon.com", "cart", "shopping cart",
    "subscribe", "subscribe & save", "follow", "follow button",
    "checkmark", "close", "spinner", "warning", "success",
    "search", "menu", "navigation",
})


def _is_chrome_image(src: str, alt: str | None, parent_tags: list[str]) -> bool:
    """True if this `<img>` is Amazon UI chrome (nav sprite, Prime/Smile logo,
    cart icon, etc.) rather than a real brand-store creative asset."""
    s = (src or "").split("?", 1)[0]
    # Allow-list: only product-photo CDN paths and brand-upload paths.
    if not any(p in s for p in _ASSET_PATH_KEEP_SUBSTRINGS):
        return True
    # Belt-and-suspenders: explicit chrome path denylist.
    if any(p in s for p in _CHROME_PATH_PATTERNS):
        return True
    # Alt-text heuristic.
    a = (alt or "").strip().lower()
    if a in _CHROME_ALT_TEXTS:
        return True
    # Parent ancestor heuristic — nav/header/footer rarely contain real creative.
    chrome_ancestors = {"nav", "header", "footer"}
    if any(t in chrome_ancestors for t in (parent_tags or [])):
        return True
    return False


def _page_key_from_url(url: str) -> str:
    m = PAGE_UUID_RE.search(urlparse(url).path)
    return m.group(1) if m else hashlib.sha1(url.encode()).hexdigest()[:12]


def scrape_brand_store(
    store_url: str,
    *,
    asset_dir: Path,
    raw_dir: Path,
    max_subpages: int = 0,           # 0 = landing-only (Phase B1)
    headless: bool = True,
    download_images: bool = True,
    scroll_iterations: int = 20,
    inter_request_delay_sec: float = 8.0,
) -> list[ScrapedPage]:
    """Open the brand store, capture landing-page artifacts, optionally walk
    sub-pages. Returns one ScrapedPage per visited URL.

    Raises FileNotFoundError if Playwright isn't installed (browser extra).
    """
    try:
        from playwright.sync_api import sync_playwright, Page
    except ImportError as e:
        raise FileNotFoundError(
            "playwright not installed — run `pip install -e '.[browser]' && playwright install chromium`"
        ) from e

    asset_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    out: list[ScrapedPage] = []
    visited: set[str] = set()
    queue: list[str] = [store_url]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, args=STEALTH_ARGS)
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 900},
            locale="en-US",
        )
        try:
            while queue:
                url = queue.pop(0)
                if url in visited:
                    continue
                visited.add(url)

                page = context.new_page()
                try:
                    sp = _capture_page(page, url, asset_dir=asset_dir, raw_dir=raw_dir,
                                       download_images=download_images,
                                       scroll_iterations=scroll_iterations,
                                       request_context=context.request)
                    out.append(sp)

                    # Sub-page discovery: queue any /stores/page/<UUID> URLs we
                    # haven't visited, up to the cap. Same-brand boundary is
                    # enforced by `_filter_subpage_links` (Phase B2 will tighten).
                    if max_subpages > 0 and len(out) <= max_subpages:
                        for link in _filter_subpage_links(sp.subpage_links, store_url):
                            if link not in visited and len(visited) + len(queue) <= max_subpages:
                                queue.append(link)
                                time.sleep(inter_request_delay_sec)  # polite delay before next request
                finally:
                    page.close()
        finally:
            context.close()
            browser.close()

    return out


def _capture_page(
    page,
    url: str,
    *,
    asset_dir: Path,
    raw_dir: Path,
    download_images: bool,
    scroll_iterations: int,
    request_context,
) -> ScrapedPage:
    """Load one page, scroll to trigger lazy-load, capture screenshot + HTML +
    images + sub-page links."""
    page_key = _page_key_from_url(url)
    page_asset_dir = asset_dir / page_key
    page_asset_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    page_raw_dir = raw_dir / page_key / ts
    page_raw_dir.mkdir(parents=True, exist_ok=True)

    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    except Exception as e:
        log.warning("goto failed for %s: %s", url, e)
        return ScrapedPage(
            page_key=page_key, url=url, title=None, hero_text=None,
            visible_text="", image_urls=[], image_local_paths=[],
            subpage_links=[], screenshot_path=None, html_path=None,
            error=f"goto failed: {type(e).__name__}: {e}",
        )

    if resp is not None and resp.status >= 500:
        log.warning("Amazon returned %s for %s — backing off (no retry)", resp.status, url)
        return ScrapedPage(
            page_key=page_key, url=url, title=None, hero_text=None,
            visible_text="", image_urls=[], image_local_paths=[],
            subpage_links=[], screenshot_path=None, html_path=None,
            error=f"amazon_blocked: HTTP {resp.status} (likely anti-bot; space out runs or add proxy)",
        )

    # Let SPA hydrate.
    page.wait_for_timeout(2_500)

    # Scroll-to-bottom loop until scrollHeight stabilizes (triggers lazy-load).
    last_h = 0
    stable = 0
    for _ in range(scroll_iterations):
        h = page.evaluate("document.body.scrollHeight")
        if h == last_h:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
            last_h = h
        page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
        page.wait_for_timeout(500)
    # Settle.
    page.wait_for_timeout(800)

    # Title + hero text (h1/h2 in first viewport).
    title = (page.title() or "").strip() or None
    try:
        hero_text = page.evaluate(
            """() => {
                const h = document.querySelector('h1, h2');
                return h ? (h.innerText || '').trim().slice(0, 240) : null;
            }"""
        )
    except Exception:
        hero_text = None

    visible_text = (page.inner_text("body") or "")[:50_000]  # cap so corpus stays manageable

    # Sub-page links — relative paths matching /stores/page/<UUID>.
    raw_links: list[str] = page.evaluate(
        """() => Array.from(document.querySelectorAll('a[href*="/stores/page/"]'))
                .map(a => a.href).filter(Boolean)"""
    ) or []
    subpage_links = sorted(set(raw_links))

    # Images: filter by natural width, capture alt + parent tag chain so we can
    # discriminate real brand-store creatives from Amazon UI chrome (nav sprites,
    # Prime/Smile logos, cart icons). Filtering happens in Python via
    # `_is_chrome_image` for testability and easier evolution.
    images = page.evaluate(
        """(minW) => Array.from(document.querySelectorAll('img'))
                .map(im => {
                    const parents = [];
                    let n = im.parentElement;
                    for (let i = 0; i < 6 && n; i++) {
                        parents.push((n.tagName || '').toLowerCase());
                        n = n.parentElement;
                    }
                    return {
                        src: im.currentSrc || im.src,
                        w: im.naturalWidth || 0,
                        h: im.naturalHeight || 0,
                        alt: im.alt || '',
                        parents: parents,
                    };
                })
                .filter(o => o.src && o.src.startsWith('http') && o.w >= minW)""",
        MIN_IMAGE_WIDTH,
    ) or []
    image_urls = []
    seen_src: set[str] = set()
    chrome_skipped = 0
    for img in images:
        src = img["src"]
        if _is_chrome_image(src, img.get("alt"), img.get("parents") or []):
            chrome_skipped += 1
            continue
        # Strip Amazon image-format encoding suffix to dedup near-duplicates.
        # e.g. `..._SX300_.jpg` and `..._SX600_.jpg` are the same image at different sizes.
        canon = re.sub(r"_S[XYL]?\d+[A-Z]?_?", "", src)
        if canon in seen_src:
            continue
        seen_src.add(canon)
        image_urls.append(src)
    if chrome_skipped:
        log.info("page %s: dropped %d Amazon UI-chrome image(s)", page_key, chrome_skipped)

    # Persist screenshot + raw HTML.
    screenshot_path = page_raw_dir / "landing.png"
    html_path = page_raw_dir / "landing.html"
    try:
        page.screenshot(path=str(screenshot_path), full_page=True, timeout=30_000)
    except Exception as e:
        log.warning("screenshot failed for %s: %s", url, e)
        screenshot_path = None  # type: ignore[assignment]
    try:
        html_path.write_text(page.content(), encoding="utf-8")
    except Exception as e:
        log.warning("html save failed for %s: %s", url, e)
        html_path = None  # type: ignore[assignment]

    # Download images via Playwright session (inherits cookies/headers).
    image_local_paths: list[str] = []
    if image_urls:
        for idx, img_url in enumerate(image_urls):
            try:
                r = request_context.get(img_url, timeout=15_000)
                if not r.ok:
                    log.debug("image %s returned %d", img_url, r.status)
                    continue
                body = r.body()
                if len(body) < 2000:
                    continue
                ct = (r.headers.get("content-type") or "").lower()
                ext = ".png" if "png" in ct else ".webp" if "webp" in ct else ".jpg"
                out_path = page_asset_dir / f"image_{idx:02d}{ext}"
                out_path.write_bytes(body)
                image_local_paths.append(str(out_path))
            except Exception as e:
                log.debug("image download failed %s: %s", img_url, e)

    return ScrapedPage(
        page_key=page_key,
        url=url,
        title=title,
        hero_text=hero_text,
        visible_text=visible_text,
        image_urls=image_urls,
        image_local_paths=image_local_paths,
        subpage_links=subpage_links,
        screenshot_path=str(screenshot_path) if screenshot_path else None,
        html_path=str(html_path) if html_path else None,
    )


def _filter_subpage_links(links: list[str], origin_url: str) -> list[str]:
    """Keep only /stores/page/<UUID> links on the same host as origin_url.
    Phase B2 will add same-brand-boundary heuristics (seller id, brand UUID match)."""
    origin_host = urlparse(origin_url).netloc
    out: list[str] = []
    seen: set[str] = set()
    for link in links:
        u = urlparse(link)
        if u.netloc and u.netloc != origin_host:
            continue
        if not PAGE_UUID_RE.search(u.path):
            continue
        # Normalize: drop query strings + fragments for dedup.
        canonical = f"{u.scheme or 'https'}://{u.netloc or origin_host}{u.path}"
        if canonical in seen or canonical == origin_url:
            continue
        seen.add(canonical)
        out.append(canonical)
    return out


def _content_hash_for_pages(pages: list[ScrapedPage]) -> str:
    """Hash that's stable across cosmetic reorderings, sensitive to real content
    changes. Combines: sorted page UUIDs, sorted hero texts, total image count."""
    parts = []
    for p in sorted(pages, key=lambda x: x.page_key):
        parts.append(p.page_key)
        parts.append((p.hero_text or "").strip())
        parts.append(str(len(p.image_local_paths)))
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


class AmazonBrandStoreAdapter(Adapter):
    source_type = "amazon_brand_store"

    def fetch(self) -> IngestResult:
        store_url = self.source.store_url
        source_key = self.source.stable_key()
        if not store_url:
            return IngestResult(
                competitor_id=self.competitor.id,
                source_key=source_key,
                ok=False,
                error="amazon_brand_store source requires `store_url`",
            )
        if not PAGE_UUID_RE.search(store_url):
            return IngestResult(
                competitor_id=self.competitor.id,
                source_key=source_key,
                ok=False,
                error=f"store_url doesn't look like a brand-store URL: {store_url}",
            )

        # Storage layout:
        #   data/raw/<comp>/amazon_brand_store/<page_key>/<ts>/{landing.png, landing.html}
        #   data/creative/<comp>/amazon_brand_store/<page_key>/image_NN.jpg
        raw_dir = asset_path("raw", self.competitor.id, "amazon_brand_store")
        asset_root = asset_path("creative", self.competitor.id, "amazon_brand_store")

        max_subpages = self.source.max_subpages
        try:
            pages = scrape_brand_store(
                store_url,
                asset_dir=asset_root,
                raw_dir=raw_dir,
                max_subpages=max_subpages,
            )
        except FileNotFoundError as e:
            return IngestResult(
                competitor_id=self.competitor.id,
                source_key=source_key,
                ok=False,
                error=str(e),
            )
        except Exception as e:
            log.exception("brand store scrape crashed: %s", e)
            return IngestResult(
                competitor_id=self.competitor.id,
                source_key=source_key,
                ok=False,
                error=f"scrape crashed: {type(e).__name__}: {e}",
            )

        if not pages:
            return IngestResult(
                competitor_id=self.competitor.id,
                source_key=source_key,
                ok=False,
                error="scrape returned no pages",
            )

        # If the landing page errored (Amazon block), surface that.
        landing = pages[0]
        if landing.error:
            return IngestResult(
                competitor_id=self.competitor.id,
                source_key=source_key,
                ok=False,
                raw_path=landing.screenshot_path,
                error=landing.error,
            )

        content_hash = _content_hash_for_pages(pages)
        parsed = {
            "store_url": store_url,
            "pages": [p.to_dict() for p in pages],
            "totals": {
                "pages_captured": len(pages),
                "images": sum(len(p.image_local_paths) for p in pages),
            },
        }

        # Pass full page records (with image paths + phashes deferred to enrichment)
        # through extras for runner.py to persist.
        return IngestResult(
            competitor_id=self.competitor.id,
            source_key=source_key,
            ok=True,
            content_hash=content_hash,
            raw_path=landing.screenshot_path,
            parsed=parsed,
            extras={"pages": pages},  # ScrapedPage objects; runner iterates image_local_paths
        )
