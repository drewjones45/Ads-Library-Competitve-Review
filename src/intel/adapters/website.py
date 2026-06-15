"""Website adapter — Playwright-backed homepage capture.

Captures the rendered HTML, a full-page screenshot, and the visible imagery
from a brand's homepage. Extracts the same hero/banner/popup text regions
the previous httpx-based implementation produced, so the downstream text-based
offer extractor (`analysis/offers.py`) continues to work unchanged.

Layered modules:
  - scrape_homepage(url, ...)        — pure Playwright scrape; testable via
                                       `intel test-homepage <url>`
  - WebsiteAdapter                   — Adapter ABC; wires scrape output into
                                       IngestResult so runner.py can persist

Phase H1: image capture + chrome filter + per-scrape phash dedup. Phase H2
adds hero crop + structured hero extractor (see homepage_hero.py).
"""
from __future__ import annotations

import hashlib
import io
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from ._scraped_page import ScrapedPage
from .base import Adapter, IngestResult
from ..storage import asset_path, content_hash


log = logging.getLogger("intel.website")


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
]

# Emulate a New York City visitor so geo-gated offers/pricing render as a
# NYC shopper would see them. Sets the browser Geolocation API position +
# timezone + Accept-Language; spread into every new_context() below. Note this
# does NOT change the egress IP — sites that geo-target purely by IP still see
# this machine's location.
NYC_GEO_CONTEXT = {
    "geolocation": {"latitude": 40.7128, "longitude": -74.0060, "accuracy": 50},
    "permissions": ["geolocation"],
    "timezone_id": "America/New_York",
    "locale": "en-US",
    "extra_http_headers": {"Accept-Language": "en-US,en;q=0.9"},
}

# Regions on a marketing page that disproportionately carry on-site offers.
OFFER_SELECTORS = [
    "[class*='banner' i]",
    "[class*='announce' i]",
    "[class*='promo' i]",
    "[class*='hero' i]",
    "[class*='popup' i]",
    "[class*='modal' i]",
    "[data-section-type*='banner' i]",
    "[data-section*='hero' i]",
]

MIN_IMAGE_WIDTH = 200
MAX_IMAGES_DEFAULT = 60  # cap per homepage; protects against unbounded image-heavy pages

# Path/URL tokens that flag UI chrome.
_CHROME_PATH_TOKENS = (
    "/icons/", "/payment/", "/payments/", "/social/", "/favicon",
    "/sprite", "/sprites/", "/svg-sprite",
)

# Alt-text + filename tokens that flag UI chrome / trust badges / logos.
_CHROME_ALT_TOKENS = frozenset({
    "trustpilot", "klarna", "affirm", "afterpay", "paypal", "apple-pay",
    "apple pay", "google-pay", "google pay", "bbb", "synchrony",
    "visa", "mastercard", "amex", "american express", "discover",
    "facebook", "instagram", "twitter", "pinterest", "tiktok", "youtube",
    "linkedin", "x.com",
    "cart", "search", "menu", "hamburger", "close", "burger",
})

# Image magic bytes — guards against Cloudflare HTML error pages served 200.
_IMAGE_MAGIC = (
    b"\x89PNG", b"\xff\xd8\xff",
    b"RIFF",          # WebP (full magic is "RIFF....WEBP" but first 4 are enough)
    b"GIF8",
)


# Markers used to detect anti-bot challenge pages (returned in lieu of the real
# homepage). When matched, we treat the observation as "blocked" — record it so
# the dashboard can surface the block, but skip image extraction + hero
# extraction since the captured content isn't the brand's actual homepage.
_CAPTCHA_TEXT_MARKERS: tuple[tuple[str, str], ...] = (
    # PerimeterX (Ashley, many large retailers)
    ("press & hold", "perimeterx"),
    ("press and hold", "perimeterx"),
    ("px-captcha", "perimeterx"),
    ("perimeterx", "perimeterx"),
    # Cloudflare
    ("just a moment", "cloudflare"),
    ("checking your browser before accessing", "cloudflare"),
    ("attention required! | cloudflare", "cloudflare"),
    ("cf-browser-verification", "cloudflare"),
    # DataDome
    ("blocked because", "datadome"),
    ("datadome", "datadome"),
    # Akamai Bot Manager
    ("access denied", "akamai"),
    ("you don't have permission to access", "akamai"),
    # Generic
    ("are you a human", "generic_captcha"),
    ("verify you are human", "generic_captcha"),
    ("solve this puzzle", "generic_captcha"),
    ("robot check", "generic_captcha"),
)

_CAPTCHA_SELECTORS: tuple[tuple[str, str], ...] = (
    ("#px-captcha", "perimeterx"),
    ("[id^='px-captcha']", "perimeterx"),
    ("#cf-challenge-stage", "cloudflare"),
    (".cf-browser-verification", "cloudflare"),
    ("#challenge-running", "cloudflare"),
    ("#captcha-bounding-box", "google_recaptcha"),
)


def _detect_captcha_block(page) -> str | None:
    """Return the vendor name (e.g., 'perimeterx', 'cloudflare') if the loaded
    page is an anti-bot challenge / block screen rather than the brand's real
    homepage. Otherwise None.

    Cheap and best-effort — meant to catch the obvious cases (Ashley, Wayfair).
    Doesn't try to be perfect; a wrong negative just means a noisy observation.
    """
    try:
        title = (page.title() or "").lower()
    except Exception:
        title = ""
    try:
        body_text = (page.inner_text("body") or "")[:4_000].lower()
    except Exception:
        body_text = ""
    try:
        html_head = (page.content() or "")[:4_000].lower()
    except Exception:
        html_head = ""
    haystack = f"{title}\n{body_text}\n{html_head}"
    for marker, vendor in _CAPTCHA_TEXT_MARKERS:
        if marker in haystack:
            return vendor
    for sel, vendor in _CAPTCHA_SELECTORS:
        try:
            if page.query_selector(sel):
                return vendor
        except Exception:
            pass
    return None


def _domain_slug(url: str) -> str:
    host = urlparse(url).netloc.replace("www.", "").replace(".", "_")
    path = re.sub(r"[^a-z0-9_]+", "_", urlparse(url).path.lower()).strip("_") or "root"
    return f"{host}__{path}"


def _extract_visible_text(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{2,}", "\n", text)
    return text


def _extract_candidate_regions(soup: BeautifulSoup) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"hero": [], "banner_or_promo": [], "popup_or_modal": []}
    for sel in OFFER_SELECTORS:
        for el in soup.select(sel):
            txt = el.get_text(separator=" ", strip=True)
            if not txt or len(txt) > 600:
                continue
            key = (
                "popup_or_modal"
                if any(k in sel for k in ["popup", "modal"])
                else "hero"
                if "hero" in sel
                else "banner_or_promo"
            )
            if txt not in out[key]:
                out[key].append(txt)
    return out


def _extract_meta(soup: BeautifulSoup) -> dict[str, str | None]:
    md = lambda name: (soup.find("meta", attrs={"name": name}) or {}).get("content") \
        if soup.find("meta", attrs={"name": name}) else None
    og = lambda prop: (soup.find("meta", attrs={"property": prop}) or {}).get("content") \
        if soup.find("meta", attrs={"property": prop}) else None
    return {
        "title": (soup.title.string.strip() if soup.title and soup.title.string else None),
        "description": md("description"),
        "og_title": og("og:title"),
        "og_description": og("og:description"),
    }


def _is_placeholder(url: str) -> bool:
    """A data-URI, transparent GIF, or named placeholder used by lazy loaders."""
    if not url:
        return True
    u = url.lower()
    if u.startswith("data:"):
        return True
    return "placeholder" in u or "blank.gif" in u or "spacer.gif" in u or "transparent.gif" in u


def _resolve_image_src(img: dict) -> str | None:
    """Choose the best URL from a scraped img dict, preferring real over placeholder.
    Handles `srcset` (last entry, highest density) and `data-src` lazy patterns.
    """
    src = (img.get("src") or "").strip()
    data_src = (img.get("dataSrc") or "").strip()
    # If src is empty or a placeholder, prefer data-src.
    if (_is_placeholder(src) or not src) and data_src and not _is_placeholder(data_src):
        return data_src
    return src or data_src or None


def _is_chrome_image(
    src: str,
    alt: str | None,
    width: int,
    height: int,
    in_nav: bool,
    in_header: bool,
    in_footer: bool,
) -> tuple[bool, str]:
    """True if this image is UI chrome (icon, social, payment badge, etc.).
    Returns (is_chrome, reason) for debug output.
    """
    if not src:
        return True, "empty_src"
    s = src.split("?", 1)[0].lower()
    fname = s.rsplit("/", 1)[-1]

    # Path-token denylist
    for tok in _CHROME_PATH_TOKENS:
        if tok in s:
            return True, f"path_token:{tok}"

    # Extension-based small-icon filter
    ext = "." + (fname.rsplit(".", 1)[-1] if "." in fname else "")
    if ext in (".svg", ".ico") and width < 300:
        return True, f"small_{ext.strip('.')}"

    # Alt-text + filename token denylist
    a = (alt or "").strip().lower()
    haystack = f"{a} {fname}"
    for tok in _CHROME_ALT_TOKENS:
        if tok in haystack:
            return True, f"alt_token:{tok}"
    # "logo" is too aggressive — only flag when image is small.
    if ("logo" in haystack) and width < 200:
        return True, "small_logo"

    # Dimension filter
    if width < MIN_IMAGE_WIDTH:
        return True, f"width<{MIN_IMAGE_WIDTH}"

    # DOM position rules
    if in_nav:
        return True, "in_nav"
    if in_footer:
        return True, "in_footer"
    if in_header:
        # Keep large hero banners in <header>; drop announcement-bar icons.
        area = width * height
        aspect = width / max(height, 1)
        if not (area >= 240_000 and aspect < 4.0):
            return True, "header_chrome"

    return False, ""


def _dismiss_overlays(page) -> int:
    """Best-effort: close cookie banners + email-capture modals so they don't
    occlude the stitched screenshot. Returns count of overlays acted on.

    Strategies (in order, each guarded):
    1. Press ESC twice (closes many keyboard-friendly modals).
    2. Click common close-button selectors (aria-label='close', '×' glyphs,
       'no thanks'/'not now' buttons).
    3. Remove obvious fixed-position overlay roots from the DOM (PerimeterX-style
       captcha pages are left alone — those are the page).
    """
    acted = 0
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)
    except Exception:
        pass

    close_selectors = [
        '[aria-label="Close" i]',
        '[aria-label="close" i]',
        'button[aria-label*="close" i]',
        'button[aria-label*="dismiss" i]',
        'button[title*="close" i]',
        '[data-testid*="close" i]',
        'button:has-text("No thanks")',
        'button:has-text("No Thanks")',
        'button:has-text("Not now")',
        'button:has-text("Maybe later")',
        'button:has-text("I\'m good")',
        'a:has-text("No thanks")',
        '.modal-close, .close-modal, .popup-close',
    ]
    for sel in close_selectors:
        try:
            handles = page.query_selector_all(sel)
        except Exception:
            continue
        for h in handles[:3]:  # cap per selector
            try:
                if h.is_visible():
                    h.click(timeout=1_000, force=True)
                    acted += 1
                    page.wait_for_timeout(150)
            except Exception:
                pass

    # Accept cookie banners so they collapse (CCPA/GDPR/Onetrust).
    cookie_accepts = [
        'button#onetrust-accept-btn-handler',
        'button[id*="cookie" i][id*="accept" i]',
        'button:has-text("Accept All")',
        'button:has-text("Accept all")',
        'button:has-text("Allow all")',
        'button:has-text("Got it")',
        'button:has-text("Agree")',
    ]
    for sel in cookie_accepts:
        try:
            h = page.query_selector(sel)
            if h and h.is_visible():
                h.click(timeout=1_000, force=True)
                acted += 1
                page.wait_for_timeout(200)
                break
        except Exception:
            pass

    return acted


def _hide_fixed_overlays(page) -> list[str]:
    """Find fixed/sticky-position overlays that occlude content, set them to
    `visibility: hidden`, and return the list of CSS selector hashes so the
    caller can restore them. Idempotent.

    Note: we ignore small fixed elements (back-to-top buttons, < 200px²)
    and the very top sticky header (often the nav we WANT in the screenshot
    once at chunk 0). This runs between chunks 1..N, so chunk 0 already
    captured the sticky header in its natural position.
    """
    return page.evaluate(
        """() => {
            const hidden = [];
            for (const el of document.body.querySelectorAll('*')) {
                const s = getComputedStyle(el);
                if ((s.position !== 'fixed' && s.position !== 'sticky')) continue;
                const r = el.getBoundingClientRect();
                if (r.width * r.height < 40000) continue;  // skip small UI affordances
                if (s.visibility === 'hidden' || s.display === 'none') continue;
                // Mark + hide
                el.dataset._origVisibility = s.visibility;
                el.style.setProperty('visibility', 'hidden', 'important');
                hidden.push(el.tagName + (el.id ? '#' + el.id : '') + '.' + (el.className || '').toString().slice(0, 40));
            }
            return hidden;
        }"""
    ) or []


def _restore_fixed_overlays(page) -> None:
    try:
        page.evaluate(
            """() => {
                for (const el of document.body.querySelectorAll('[data-_orig-visibility]')) {
                    el.style.removeProperty('visibility');
                    delete el.dataset._origVisibility;
                }
            }"""
        )
    except Exception:
        pass


def _capture_stitched_screenshot(page, *, output_path: Path,
                                  viewport_height: int = 900,
                                  max_height: int = 25_000,
                                  per_chunk_wait_ms: int = 450) -> bool:
    """GoFullPage-style: scroll viewport-by-viewport, screenshot each chunk,
    stitch with PIL. Handles lazy-loaded carousels + IntersectionObserver
    content that `full_page=True` misses on modern themes.

    Fixed/sticky elements (modals, sticky headers) would otherwise repeat in
    every chunk and produce a stripey output. Mitigations:
    - Before the first chunk: dismiss obvious modals (ESC, close-button click).
    - For chunks 1..N: hide fixed/sticky overlays, screenshot, restore.
    """
    try:
        from PIL import Image
        import io as _io
    except ImportError:
        return False

    try:
        viewport_w = page.viewport_size["width"]
    except Exception:
        viewport_w = 1440

    # Dismiss modals + cookie banners once before we start.
    dismissed = _dismiss_overlays(page)
    if dismissed:
        log.info("dismissed %d overlay(s) before stitched capture", dismissed)

    # Scroll to top and capture initial page height.
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(300)
    total_h = int(page.evaluate("document.body.scrollHeight") or viewport_height)
    total_h = min(total_h, max_height)

    chunks: list[tuple[int, bytes]] = []
    y = 0
    chunk_idx = 0
    while y < total_h:
        page.evaluate(f"window.scrollTo(0, {y})")
        page.wait_for_timeout(per_chunk_wait_ms)
        # Hide fixed/sticky overlays starting at the SECOND chunk — chunk 0 should
        # render the sticky header in its natural position (top of page).
        if chunk_idx > 0:
            try:
                _hide_fixed_overlays(page)
            except Exception:
                pass
        # Wait for in-viewport images to finish.
        try:
            page.wait_for_function(
                """() => {
                    const vh = window.innerHeight;
                    return Array.from(document.querySelectorAll('img'))
                        .filter(i => {
                            const r = i.getBoundingClientRect();
                            return r.top < vh && r.bottom > 0;
                        })
                        .every(i => i.complete);
                }""",
                timeout=3_000,
            )
        except Exception:
            pass
        try:
            png = page.screenshot(timeout=10_000)
            chunks.append((y, png))
        except Exception as e:
            log.warning("chunk screenshot failed at y=%d: %s", y, e)
        # Restore so subsequent measurements are accurate.
        if chunk_idx > 0:
            _restore_fixed_overlays(page)
        # Re-read total height — it can grow as lazy-loaded panels reveal more.
        new_total = int(page.evaluate("document.body.scrollHeight") or total_h)
        total_h = min(max(total_h, new_total), max_height)
        y += viewport_height
        chunk_idx += 1

    if not chunks:
        return False

    last_y = chunks[-1][0]
    composite_h = min(total_h, last_y + viewport_height)
    composite = Image.new("RGB", (viewport_w, composite_h), (255, 255, 255))
    for offset, png in chunks:
        try:
            with Image.open(_io.BytesIO(png)) as chunk_img:
                chunk_img = chunk_img.convert("RGB")
                slice_h = min(chunk_img.height, composite_h - offset)
                if slice_h <= 0:
                    continue
                if slice_h < chunk_img.height:
                    chunk_img = chunk_img.crop((0, 0, viewport_w, slice_h))
                composite.paste(chunk_img, (0, offset))
        except Exception as e:
            log.warning("chunk paste failed at y=%d: %s", offset, e)
    try:
        composite.save(output_path, optimize=True)
        return True
    except Exception as e:
        log.warning("composite save failed for %s: %s", output_path, e)
        return False


def _phash_from_bytes(body: bytes) -> str | None:
    try:
        from PIL import Image
        import imagehash
        with Image.open(io.BytesIO(body)) as im:
            return str(imagehash.phash(im))
    except Exception:
        return None


def _validate_image_body(body: bytes, content_type: str) -> bool:
    if not body or len(body) < 2_000:
        return False
    if not content_type.startswith("image/"):
        return False
    head = body[:4]
    return any(head.startswith(magic) for magic in _IMAGE_MAGIC)


def _settle_page_for_capture(page, *, scroll_iterations: int) -> None:
    """Drive a loaded Playwright page so a full-page screenshot captures the
    real content: scroll to the bottom (kick IntersectionObserver lazy-loaders),
    force-load any placeholder images, wait for network settle, scroll back to
    the top. Shared by the homepage and landing-page capture paths."""
    # Scroll-to-bottom loop until scrollHeight stabilizes — kicks intersection
    # observers so lazy-loaded panels render. Most furniture themes use
    # IntersectionObserver to swap a placeholder GIF for the real image only
    # when the element enters the viewport.
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

    # Force-load any images still using a placeholder src. Intersection-observer
    # patterns won't fire for elements that never entered the viewport (e.g.,
    # display:none modals or images on tabs we never clicked). Walk every img
    # and promote data-src / srcset into src; mark loading=eager so the browser
    # actually fetches them.
    page.evaluate(
        """() => {
            const isPlaceholder = (s) => !s || s.startsWith('data:')
                || s.includes('placeholder') || s.includes('blank.gif')
                || s.includes('spacer.gif');
            for (const img of document.querySelectorAll('img')) {
                img.loading = 'eager';
                const lazy = img.dataset.src || img.dataset.lazySrc
                          || img.dataset.original || '';
                if (lazy && isPlaceholder(img.src)) {
                    img.src = lazy;
                }
                if (img.srcset && isPlaceholder(img.src)) {
                    const last = img.srcset.split(',').map(s => s.trim()).pop();
                    if (last) img.src = last.split(' ')[0];
                }
            }
        }"""
    )

    # Wait for the forced-load images to actually fetch, then settle + scroll
    # back to top so the full-page screenshot starts at the hero.
    try:
        page.wait_for_load_state("networkidle", timeout=8_000)
    except Exception:
        page.wait_for_timeout(2_000)
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(400)


def scrape_landing_page(
    url: str,
    *,
    out_dir: Path,
    headless: bool = True,
    scroll_iterations: int = 10,
) -> ScrapedPage:
    """Capture a whole-page stitched screenshot of an ad's landing page.

    Screenshot-only twin of scrape_homepage — no per-<img> extraction. Writes
    `out_dir/landing.png` at a STABLE path (re-runs overwrite, so the creative
    dedups to one row per URL). Returns a ScrapedPage with `screenshot_path`
    set, or `error` set on a captcha wall / goto failure / 5xx (caller skips
    those — never persists a captcha page as the landing screenshot).
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise FileNotFoundError(
            "playwright not installed — run `pip install -e '.[browser]' && playwright install chromium`"
        ) from e

    page_key = _domain_slug(url)
    out_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, args=STEALTH_ARGS)
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 900},
            **NYC_GEO_CONTEXT,
        )
        try:
            page = context.new_page()
            try:
                return _capture_landing_screenshot(
                    page, url, page_key=page_key, out_dir=out_dir,
                    scroll_iterations=scroll_iterations,
                )
            finally:
                page.close()
        finally:
            context.close()
            browser.close()


def _capture_landing_screenshot(
    page, url: str, *, page_key: str, out_dir: Path, scroll_iterations: int,
) -> ScrapedPage:
    """Navigate to a landing page and stitched-screenshot it (no image extraction)."""
    def _err(msg: str) -> ScrapedPage:
        return ScrapedPage(
            page_key=page_key, url=url, title=None, hero_text=None,
            visible_text="", image_urls=[], image_local_paths=[],
            subpage_links=[], screenshot_path=None, html_path=None, error=msg,
        )

    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    except Exception as e:
        log.warning("landing goto failed for %s: %s", url, e)
        return _err(f"goto failed: {type(e).__name__}: {e}")

    status = resp.status if resp is not None else 0
    if status and status >= 500:
        log.warning("landing page returned %s for %s — no retry", status, url)
        return _err(f"http_{status}")

    # Let SPA frameworks hydrate, then bail cleanly on an anti-bot wall.
    page.wait_for_timeout(2_000)
    blocker = _detect_captcha_block(page)
    if blocker:
        log.warning("landing %s: blocked by '%s' anti-bot wall — skipping", page_key, blocker)
        return _err(f"captcha_wall:{blocker}")

    _settle_page_for_capture(page, scroll_iterations=scroll_iterations)

    title = (page.title() or "").strip() or None
    screenshot_path = out_dir / "landing.png"
    stitched_ok = _capture_stitched_screenshot(page, output_path=screenshot_path)
    if not stitched_ok:
        try:
            page.screenshot(path=str(screenshot_path), full_page=True, timeout=30_000)
        except Exception as e:
            log.warning("landing screenshot failed for %s: %s", url, e)
            return _err(f"screenshot failed: {type(e).__name__}: {e}")

    return ScrapedPage(
        page_key=page_key, url=url, title=title, hero_text=None,
        visible_text="", image_urls=[], image_local_paths=[],
        subpage_links=[], screenshot_path=str(screenshot_path), html_path=None,
    )


def scrape_homepage(
    url: str,
    *,
    asset_dir: Path,
    raw_dir: Path,
    max_images: int = MAX_IMAGES_DEFAULT,
    headless: bool = True,
    scroll_iterations: int = 10,
) -> ScrapedPage:
    """Open the homepage, capture screenshot + HTML + downloaded images.

    Raises FileNotFoundError if Playwright isn't installed.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise FileNotFoundError(
            "playwright not installed — run `pip install -e '.[browser]' && playwright install chromium`"
        ) from e

    page_key = _domain_slug(url)
    page_asset_dir = asset_dir / page_key
    page_asset_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    page_raw_dir = raw_dir / f"{page_key}__{ts}"
    page_raw_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, args=STEALTH_ARGS)
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 900},
            **NYC_GEO_CONTEXT,
        )
        try:
            page = context.new_page()
            try:
                return _capture_homepage(
                    page,
                    url,
                    page_key=page_key,
                    page_asset_dir=page_asset_dir,
                    page_raw_dir=page_raw_dir,
                    request_context=context.request,
                    max_images=max_images,
                    scroll_iterations=scroll_iterations,
                )
            finally:
                page.close()
        finally:
            context.close()
            browser.close()


def _capture_homepage(
    page,
    url: str,
    *,
    page_key: str,
    page_asset_dir: Path,
    page_raw_dir: Path,
    request_context,
    max_images: int,
    scroll_iterations: int,
) -> ScrapedPage:
    """Drive a single Playwright page through load → lazy-scroll → capture."""
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

    status = resp.status if resp is not None else 0
    if status and status >= 500:
        log.warning("homepage returned %s for %s — no retry", status, url)
        return ScrapedPage(
            page_key=page_key, url=url, title=None, hero_text=None,
            visible_text="", image_urls=[], image_local_paths=[],
            subpage_links=[], screenshot_path=None, html_path=None,
            error=f"http_{status}",
        )

    # Let SPA frameworks hydrate before scrolling.
    page.wait_for_timeout(2_000)

    # Anti-bot detection — bail out cleanly if we hit a captcha/block page.
    # No point scrolling, downloading images, or screenshotting the wall.
    blocker = _detect_captcha_block(page)
    if blocker:
        log.warning("homepage %s: blocked by '%s' anti-bot wall — recording as blocked observation", page_key, blocker)
        # Save a small diagnostic screenshot + raw HTML so we can verify the
        # detection later (and so the captcha page itself isn't lost). These
        # are NOT exposed as the brand's landing.
        diag_screenshot: Path | None = None
        diag_html: Path | None = None
        try:
            diag_screenshot = page_raw_dir / "blocked.png"
            page.screenshot(path=str(diag_screenshot), full_page=False, timeout=10_000)
        except Exception:
            diag_screenshot = None
        try:
            diag_html = page_raw_dir / "blocked.html"
            diag_html.write_text(page.content(), encoding="utf-8")
        except Exception:
            diag_html = None
        sp = ScrapedPage(
            page_key=page_key, url=url, title=None, hero_text=None,
            visible_text="", image_urls=[], image_local_paths=[],
            subpage_links=[], screenshot_path=None, html_path=None,
            error=f"captcha_wall:{blocker}",
        )
        # Attach a soft-block parsed dict so WebsiteAdapter.fetch() can record
        # a normal observation tagged blocked=True (instead of returning ok=False
        # and losing the signal that we attempted + were blocked).
        sp.parsed = {  # type: ignore[attr-defined]
            "url": url,
            "blocked": True,
            "block_vendor": blocker,
            "block_message": f"captcha_wall:{blocker}",
            "diag_screenshot": str(diag_screenshot) if diag_screenshot else None,
            "diag_html": str(diag_html) if diag_html else None,
            "regions": {"hero": [], "banner_or_promo": [], "popup_or_modal": []},
            "image_count": 0,
            "screenshot_path": None,
            "hero_image_path": None,
        }
        return sp

    # Scroll the page, force-load lazy images, settle, and return to the top so
    # the full-page screenshot starts at the hero.
    _settle_page_for_capture(page, scroll_iterations=scroll_iterations)

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

    rendered_html = page.content()
    soup = BeautifulSoup(rendered_html, "html.parser")
    visible = _extract_visible_text(soup)
    regions = _extract_candidate_regions(soup)
    meta = _extract_meta(soup)

    # Collect image candidates from the live DOM. Capture natural AND rendered
    # dimensions — natural can be 0 for lazy images whose load hasn't finished,
    # but the CSS-layout bounding box still gives us a real-world size signal.
    raw_images = page.evaluate(
        """() => Array.from(document.querySelectorAll('img')).map(img => {
            const r = img.getBoundingClientRect();
            const srcset = img.srcset || img.dataset.srcset || '';
            let candidate = img.currentSrc || img.src || '';
            if (srcset) {
                const last = srcset.split(',').map(s => s.trim()).pop();
                if (last) candidate = last.split(' ')[0] || candidate;
            }
            return {
                src: candidate,
                dataSrc: img.dataset.src || img.dataset.lazySrc || img.dataset.original || '',
                alt: img.alt || '',
                naturalWidth: img.naturalWidth || 0,
                naturalHeight: img.naturalHeight || 0,
                renderedWidth: Math.round(r.width),
                renderedHeight: Math.round(r.height),
                inNav: !!img.closest('nav'),
                inHeader: !!img.closest('header'),
                inFooter: !!img.closest('footer'),
            };
        })"""
    ) or []

    # Chrome filter + dedup-by-URL. Use max(natural, rendered) for dimensions —
    # lazy-loaded images that haven't finished loading have naturalWidth=0 but
    # still have CSS-layout dimensions in the rendered bounding box.
    candidates: list[dict] = []
    seen_url: set[str] = set()
    chrome_drops: dict[str, int] = {}
    for img in raw_images:
        src = _resolve_image_src(img)
        if not src or not src.startswith(("http://", "https://")):
            continue
        canon = src.split("?", 1)[0]
        if canon in seen_url:
            continue
        seen_url.add(canon)
        eff_w = max(int(img.get("naturalWidth") or 0), int(img.get("renderedWidth") or 0))
        eff_h = max(int(img.get("naturalHeight") or 0), int(img.get("renderedHeight") or 0))
        is_chrome, reason = _is_chrome_image(
            src,
            img.get("alt"),
            eff_w,
            eff_h,
            bool(img.get("inNav")),
            bool(img.get("inHeader")),
            bool(img.get("inFooter")),
        )
        if is_chrome:
            chrome_drops[reason] = chrome_drops.get(reason, 0) + 1
            continue
        candidates.append({"src": src, "width": eff_w, "height": eff_h, **img})

    if chrome_drops:
        log.info(
            "homepage %s: dropped %d chrome image(s): %s",
            page_key, sum(chrome_drops.values()),
            ", ".join(f"{k}={v}" for k, v in sorted(chrome_drops.items())),
        )

    # Persist screenshot + raw HTML.
    screenshot_path = page_raw_dir / "landing.png"
    html_path = page_raw_dir / "landing.html"
    hero_image_path: Path | None = None
    # Prefer the GoFullPage-style stitched capture so lazy-loaded carousels
    # are actually rendered into the screenshot. Fall back to the native
    # full-page screenshot if anything fails (PIL missing, etc.).
    stitched_ok = _capture_stitched_screenshot(page, output_path=screenshot_path)
    if not stitched_ok:
        try:
            page.screenshot(path=str(screenshot_path), full_page=True, timeout=30_000)
        except Exception as e:
            log.warning("screenshot failed for %s: %s", url, e)
            screenshot_path = None  # type: ignore[assignment]
    try:
        html_path.write_text(rendered_html, encoding="utf-8")
    except Exception as e:
        log.warning("html save failed for %s: %s", url, e)
        html_path = None  # type: ignore[assignment]

    # Crop the hero region (top ~1500px) for the structured promo extractor.
    # Vision cost scales with image area; the full-page screenshot can be 5000+
    # px tall but the promotional posture (headline, hero banner, primary CTA)
    # lives above the fold.
    if screenshot_path:
        try:
            from PIL import Image
            with Image.open(screenshot_path) as im:
                w, h = im.size
                crop_h = min(1500, h)
                if crop_h < h:
                    hero = im.crop((0, 0, w, crop_h))
                else:
                    hero = im.copy()
                hero_path = page_raw_dir / "landing_hero.png"
                hero.save(hero_path, optimize=True)
                hero_image_path = hero_path
        except Exception as e:
            log.warning("hero crop failed for %s: %s", url, e)

    # Individual page-image extraction was intentionally removed: the per-<img>
    # downloads were noisy (site chrome leaking in, broken/irrelevant crops) and
    # nothing downstream consumes them anymore. The artifacts we keep are the
    # whole-page stitched screenshot (above) and the hero crop that drives the
    # site-content analysis. `candidates` is still computed above purely for the
    # `chrome_dropped` diagnostic.
    image_urls: list[str] = []
    image_local_paths: list[str] = []

    # The runner consumes parsed for offer extraction + diff; mirror prior shape
    # so the existing offer extractor and diff logic keep working unchanged.
    page_parsed = {
        "url": url,
        "http_status": status,
        "meta": meta,
        "regions": regions,
        "visible_text_excerpt": visible[:4_000],
        "screenshot_path": str(screenshot_path) if screenshot_path else None,
        "hero_image_path": str(hero_image_path) if hero_image_path else None,
        "html_path": str(html_path) if html_path else None,
        "image_count": len(image_local_paths),
        "chrome_dropped": sum(chrome_drops.values()),
    }

    # Stash parsed dict on the ScrapedPage via attribute? No — keep ScrapedPage
    # generic. The adapter builds IngestResult.parsed below from this captured
    # data + the page object's text.
    sp = ScrapedPage(
        page_key=page_key,
        url=url,
        title=title,
        hero_text=hero_text,
        visible_text=visible[:50_000],
        image_urls=image_urls,
        image_local_paths=image_local_paths,
        subpage_links=[],  # website adapter doesn't crawl subpages
        screenshot_path=str(screenshot_path) if screenshot_path else None,
        html_path=str(html_path) if html_path else None,
    )
    # Attach the page-parsed dict for the adapter to consume.
    sp.parsed = page_parsed  # type: ignore[attr-defined]
    return sp


class WebsiteAdapter(Adapter):
    """Captures a brand homepage via Playwright. Produces:
    - Full-page screenshot + rendered HTML (raw artifacts)
    - Downloaded homepage images (creative assets)
    - The same hero/banner/popup text regions the prior adapter produced
      (so the text offer extractor in analysis/offers.py keeps working)

    Per-brand opt-in is implicit: every brand has a `website` source already;
    they all gain the upgraded capture transparently. Cadence is enforced
    upstream by the scheduler.
    """

    source_type = "website"

    def fetch(self) -> IngestResult:
        if not self.source.url:
            return IngestResult(
                competitor_id=self.competitor.id,
                source_key=self.source.stable_key(),
                ok=False,
                error="website source missing url",
            )

        url = self.source.url
        # Layouts (path-stability matters; see plan):
        #   data/raw/<comp>/website/<slug>__<ts>/{landing.png, landing.html}
        #   data/creative/<comp>/website/<slug>/image_NN.<ext>     (no timestamp)
        raw_dir = asset_path("raw", self.competitor.id, "website")
        asset_root = asset_path("creative", self.competitor.id, "website")

        try:
            sp = scrape_homepage(url, asset_dir=asset_root, raw_dir=raw_dir)
        except FileNotFoundError as e:
            return IngestResult(
                competitor_id=self.competitor.id,
                source_key=self.source.stable_key(),
                ok=False,
                error=str(e),
            )
        except Exception as e:
            log.exception("homepage scrape crashed: %s", e)
            return IngestResult(
                competitor_id=self.competitor.id,
                source_key=self.source.stable_key(),
                ok=False,
                error=f"scrape crashed: {type(e).__name__}: {e}",
            )

        if sp.error:
            # Soft block (captcha wall, anti-bot challenge): record the attempt
            # so the dashboard surfaces the block + last-attempted timestamp,
            # but don't pass the captcha page off as the brand's homepage.
            # Hard errors (goto crash, http 5xx, scrape crash) still fall through
            # to ok=False.
            if sp.error.startswith("captcha_wall:"):
                vendor = sp.error.split(":", 1)[1]
                soft_parsed = getattr(sp, "parsed", None) or {
                    "url": url, "blocked": True, "block_vendor": vendor,
                    "block_message": sp.error, "regions": {"hero": [], "banner_or_promo": [], "popup_or_modal": []},
                    "image_count": 0, "screenshot_path": None, "hero_image_path": None,
                }
                return IngestResult(
                    competitor_id=self.competitor.id,
                    source_key=self.source.stable_key(),
                    ok=True,
                    # Hash on the block message so re-blocks dedup (no spurious "changed").
                    content_hash=content_hash(f"BLOCKED:{vendor}:{url}"),
                    raw_path=soft_parsed.get("diag_screenshot") or soft_parsed.get("diag_html"),
                    parsed=soft_parsed,
                    extras={"pages": []},  # no images, no hero — downstream skips enrichment
                )
            return IngestResult(
                competitor_id=self.competitor.id,
                source_key=self.source.stable_key(),
                ok=False,
                raw_path=sp.screenshot_path,
                error=sp.error,
            )

        page_parsed: dict = getattr(sp, "parsed", {}) or {
            "url": url,
            "regions": {"hero": [], "banner_or_promo": [], "popup_or_modal": []},
        }

        # Hash on the visible text (same semantics as the prior adapter) so
        # the existing change-detection threshold + diff logic carry over.
        h = content_hash(sp.visible_text)

        return IngestResult(
            competitor_id=self.competitor.id,
            source_key=self.source.stable_key(),
            ok=True,
            content_hash=h,
            raw_path=sp.screenshot_path or sp.html_path,
            parsed=page_parsed,
            extras={"pages": [sp]},  # uniform with amazon_brand_store path
        )
