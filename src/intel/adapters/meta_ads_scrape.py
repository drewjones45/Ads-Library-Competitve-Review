"""Headless-browser scrape of the public Meta Ad Library — used as a fallback when
the Graph API doesn't return current commercial ads (a known coverage gap).

The Ad Library is a React SPA with obfuscated class names. The only stable anchor
across redesigns is the "Library ID: NNNNN" label that appears once per ad card.
We locate each library-id text node and walk up to find its card container, then
extract everything inside.

Defensive on purpose — selectors break; we'd rather return partial data and log
than crash the whole run.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from playwright.sync_api import Browser, Page, sync_playwright


log = logging.getLogger("intel.meta_ads_scrape")

LIB_URL = (
    "https://www.facebook.com/ads/library/"
    "?active_status=active&ad_type=all&country={country}"
    "&is_targeted_country=false&media_type=all"
    "&search_type=page&sort_data[direction]=desc&sort_data[mode]=total_impressions"
    "&view_all_page_id={page_id}"
)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

LIBRARY_ID_RE = re.compile(r"Library\s+ID[:\s]+(\d+)", re.I)
STARTED_RE = re.compile(r"Started running on\s+([A-Za-z]+ \d+,\s+\d{4})", re.I)
STATUS_RE = re.compile(r"^(Active|Inactive)\b", re.I | re.M)
PLATFORM_NAMES = ("Facebook", "Instagram", "Audience Network", "Messenger", "Threads")


def scrape_page_ads(
    page_id: str,
    country: str = "US",
    *,
    asset_dir: Path,
    max_cards: int = 200,
    max_scrolls: int = 40,
    headless: bool = True,
    download_images: bool = True,
) -> list[dict[str, Any]]:
    """Open the public Ad Library for `page_id`, scroll until done, return a list
    of ad dicts shaped like the Graph API output (so the rest of the pipeline is
    unchanged). Images are downloaded to `asset_dir / <archive_id> / *.jpg`."""

    url = LIB_URL.format(country=country, page_id=page_id)
    asset_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser: Browser = p.chromium.launch(headless=headless)
        context = browser.new_context(user_agent=USER_AGENT, viewport={"width": 1366, "height": 900})
        page: Page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            _dismiss_cookie_banner(page)
            # Let the SPA hydrate — the ad grid is rendered after a few async fetches.
            page.wait_for_timeout(3_000)
            try:
                page.wait_for_function(
                    "() => (document.body.innerText || '').indexOf('Library ID') !== -1",
                    timeout=45_000,
                )
            except Exception:
                # Distinguish "blocked/timeout" from "page loaded but has no ads".
                body = (page.inner_text("body") or "").lower()
                if "no ads match your search criteria" in body:
                    log.info("page %s has no active ads in %s — returning empty", page_id, country)
                    return []
                # Real failure: dump artifacts for inspection.
                try:
                    debug_html = asset_dir / "_last_failed_page.html"
                    debug_png = asset_dir / "_last_failed_page.png"
                    debug_html.write_text(page.content(), encoding="utf-8")
                    page.screenshot(path=str(debug_png), full_page=False)
                    log.warning(
                        "scrape failed (no Library ID within 45s, no 'no ads' marker). "
                        "Saved %s and %s for inspection.", debug_html, debug_png,
                    )
                except Exception:
                    log.warning("scrape failed and screenshot dump also failed")
                return []

            seen_count = 0
            stable = 0
            for _ in range(max_scrolls):
                page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
                time.sleep(1.2)
                n = len(LIBRARY_ID_RE.findall(page.inner_text("body")))
                if n == seen_count:
                    stable += 1
                    if stable >= 3:
                        break
                else:
                    stable = 0
                    seen_count = n
                if n >= max_cards:
                    break

            cards = _extract_cards(page, max_cards=max_cards)
            if download_images:
                _download_card_images_playwright(cards, asset_dir, context)
                _process_card_videos_playwright(cards, asset_dir, context)
            return cards
        finally:
            context.close()
            browser.close()


def _dismiss_cookie_banner(page: Page) -> None:
    """Best-effort dismissal — different copy in different regions."""
    for label in ("Allow all cookies", "Accept all", "Only allow essential cookies", "Decline optional cookies"):
        try:
            btn = page.get_by_role("button", name=re.compile(label, re.I))
            if btn.count() > 0:
                btn.first.click(timeout=2_000)
                page.wait_for_timeout(500)
                return
        except Exception:
            continue


def _extract_cards(page: Page, *, max_cards: int) -> list[dict[str, Any]]:
    """For each 'Library ID: NNN' text node, climb up to a stable container
    and pull every child the LLM/downstream will care about."""
    locator = page.get_by_text(re.compile(r"Library ID[:\s]"))
    count = locator.count()
    log.info("found %d Library ID anchors", count)
    seen: dict[str, dict[str, Any]] = {}
    for i in range(min(count, max_cards * 2)):  # over-iterate; we dedup
        try:
            anchor = locator.nth(i)
            # Walk up to the FULL card. The Ad Library card has the "Library ID"
            # label in a header section and the creative (body + CTA + image) in
            # a sibling section — they share an ancestor that contains BOTH
            # "Library ID" and "Sponsored". Keep climbing until both appear.
            card_data = anchor.evaluate(
                """el => {
                    let node = el;
                    for (let i = 0; i < 25 && node && node.parentElement; i++) {
                        node = node.parentElement;
                        const t = node.innerText || '';
                        if (t.match(/Library ID/) && t.match(/Sponsored/i)) {
                            const imgs = Array.from(node.querySelectorAll('img'))
                                .map(im => ({ src: im.src, w: im.naturalWidth || 0, h: im.naturalHeight || 0 }))
                                .filter(o => o.src && o.src.startsWith('http'));
                            const videos = Array.from(node.querySelectorAll('video'))
                                .map(v => v.src || (v.querySelector('source') && v.querySelector('source').src)).filter(Boolean);
                            // Video poster frames — these are the thumbnails for video ads.
                            // Without them, all video ads show "no thumbnail" in the dashboard.
                            const posters = Array.from(node.querySelectorAll('video[poster]'))
                                .map(v => v.poster).filter(p => p && p.startsWith('http'));
                            const buttons = Array.from(node.querySelectorAll('a, [role="button"], button'))
                                .map(b => (b.innerText || '').trim()).filter(Boolean);
                            const links = Array.from(node.querySelectorAll('a[href]'))
                                .map(a => a.href).filter(h => h && !h.includes('facebook.com/ads/library'));
                            return { text: t, imgs: imgs, videos: videos, posters: posters, buttons: buttons, links: links };
                        }
                    }
                    return null;
                }"""
            )
            if not card_data:
                continue
            parsed = _parse_card(card_data)
            if not parsed:
                continue
            if parsed["ad_archive_id"] in seen:
                continue
            seen[parsed["ad_archive_id"]] = parsed
            if len(seen) >= max_cards:
                break
        except Exception as e:
            log.debug("card %d extraction failed: %s", i, e)
            continue
    return list(seen.values())


# Buttons that aren't real CTAs (controls, UI chrome).
_NON_CTA = {
    "see ad details", "see summary details", "see details", "open drop-down menu",
    "open dropdown", "report ad", "see more", "see less", "show more", "show less",
    "active", "inactive", "all", "active ads", "inactive ads",
    "this ad has multiple versions",
}

# Known Meta CTA labels (this is the canonical list per Ads Manager).
_KNOWN_CTAS = {
    "shop now", "learn more", "sign up", "subscribe", "book now", "contact us",
    "get offer", "get quote", "get directions", "download", "apply now",
    "request time", "save", "watch more", "play game", "use app", "install now",
    "install app", "play now", "use now", "get access", "send message",
    "send whatsapp message", "call now", "send email", "order now", "donate now",
    "watch video", "listen now",
}


def _parse_card(d: dict[str, Any]) -> dict[str, Any] | None:
    text: str = d["text"]
    m = LIBRARY_ID_RE.search(text)
    if not m:
        return None
    archive_id = m.group(1)

    started = None
    sm = STARTED_RE.search(text)
    if sm:
        started = sm.group(1)

    status = None
    stm = STATUS_RE.search(text)
    if stm:
        status = stm.group(1).lower()  # 'active' | 'inactive'

    platforms = [name for name in PLATFORM_NAMES if name in text]

    # CTA: choose the first button label that's a known Meta CTA or, failing that,
    # the first non-chrome button. Normalize whitespace/zero-width chars on the
    # button text — Meta inlines them inside a11y labels.
    def _norm_btn(s: str) -> str:
        return re.sub(r"\s+", " ", s.replace("​", "").replace("﻿", "")).strip()
    cta = None
    norm_buttons = [(_norm_btn(b), _norm_btn(b).lower()) for b in d.get("buttons", [])]
    for raw, low in norm_buttons:
        if low in _KNOWN_CTAS:
            cta = raw
            break
    if cta is None:
        for raw, low in norm_buttons:
            if low and low not in _NON_CTA and len(low) < 40:
                cta = raw
                break

    # Body text: strip away the chrome lines and isolate the actual ad copy.
    body = _extract_body(text, archive_id=archive_id, started=started, platforms=platforms, cta=cta)

    # External link (landing-page destination — drops fb.com tracking-only links).
    link_url = None
    for href in d.get("links", []):
        if "l.facebook.com" in href or "lm.facebook.com" in href:
            # Meta wraps outbound links — try to pull the `u=` param
            mu = re.search(r"[?&]u=([^&]+)", href)
            if mu:
                from urllib.parse import unquote
                link_url = unquote(mu.group(1))
                break
        elif "facebook.com" not in href and "fbcdn.net" not in href:
            link_url = href
            break

    # Filter creative images: drop avatars (~40px), icons, profile pics. Keep
    # everything >=200px on the smaller axis — that's the ad creative.
    creative_imgs: list[str] = []
    for img in d.get("imgs", []):
        if isinstance(img, str):
            if "fbcdn.net" in img:
                creative_imgs.append(img)
        else:
            src = img.get("src", "")
            w, h = img.get("w") or 0, img.get("h") or 0
            if "fbcdn.net" in src and min(w, h) >= 200:
                creative_imgs.append(src)

    # Video poster frames — added at the end so explicit images win. Video ads
    # have no `<img>` for the creative; the poster IS the thumbnail. No dim
    # filter (posters arrive without naturalWidth metadata in the eval result).
    for poster in d.get("posters") or []:
        if isinstance(poster, str) and "fbcdn.net" in poster and poster not in creative_imgs:
            creative_imgs.append(poster)

    return {
        "ad_archive_id": archive_id,
        "page_name": _extract_page_name(text),
        "page_id": None,  # not exposed in card body; the URL we asked for already locked this
        "is_active_inferred": status != "inactive",
        "active_status_label": status,
        "start_date": _normalize_date(started),
        "end_date": None,  # not shown on active-cards
        "body_text": body or None,
        "cta_type": cta,
        "link_url": link_url,
        "publisher_platforms": platforms or None,
        "creative_image_urls": creative_imgs,
        "creative_video_urls": d.get("videos") or [],
        "raw_source": "browser_scrape",
    }


def _extract_page_name(text: str) -> str | None:
    """The first non-empty line is usually the advertiser/page name."""
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.lower() in {"sponsored", "active", "inactive"}:
            continue
        if LIBRARY_ID_RE.match(s) or s.lower().startswith("library id"):
            continue
        return s[:140]
    return None


_VIDEO_TIME_RE = re.compile(r"^\d+:\d{2}\s*/\s*\d+:\d{2}$")
_DOMAIN_LINE_RE = re.compile(r"^[A-Z0-9.-]+\.(COM|NET|ORG|IO|APP|US|CO)$")  # url-preview chrome


def _extract_body(text: str, *, archive_id: str, started: str | None, platforms: list[str], cta: str | None = None) -> str:
    """Strip the chrome lines from the card text and return the ad copy.

    Chrome includes: zero-width chars, video timestamps, URL-preview domains,
    the advertiser name (first line), the CTA (which appears as plain text inside
    the card), and Meta's UI controls.
    """
    chrome_starts = (
        "sponsored", "active", "inactive",
        "open drop-down menu", "open dropdown",
        "see ad details", "see summary details", "report ad",
        "started running on", f"library id:", "platforms",
        "this ad has multiple versions",
    )
    chrome_eq = {p.lower() for p in PLATFORM_NAMES} | {"sponsored", "active", "inactive"}
    if cta:
        chrome_eq.add(cta.strip().lower())

    out: list[str] = []
    for raw in text.splitlines():
        # Strip BOM/zero-width chars then normal whitespace.
        s = raw.replace("​", "").replace("﻿", "").strip()
        if not s:
            continue
        low = s.lower()
        if any(low.startswith(t) for t in chrome_starts):
            continue
        if low in chrome_eq:
            continue
        if LIBRARY_ID_RE.match(s):
            continue
        if _VIDEO_TIME_RE.match(s):
            continue
        if _DOMAIN_LINE_RE.match(s):
            continue
        out.append(s)
    # The first non-chrome line is the advertiser name — drop it; what follows is the copy.
    body = "\n".join(out[1:]).strip()
    # Collapse repeated identical lines (Meta sometimes echoes the headline).
    seen_lines: list[str] = []
    for line in body.splitlines():
        if not seen_lines or seen_lines[-1] != line:
            seen_lines.append(line)
    return "\n".join(seen_lines)


def _normalize_date(s: str | None) -> str | None:
    """'Jun 1, 2026' → '2026-06-01'."""
    if not s:
        return None
    try:
        import datetime
        return datetime.datetime.strptime(s.strip(), "%b %d, %Y").date().isoformat()
    except Exception:
        try:
            import datetime
            return datetime.datetime.strptime(s.strip(), "%B %d, %Y").date().isoformat()
        except Exception:
            return s


def _download_card_images_playwright(cards: list[dict[str, Any]], asset_dir: Path, context) -> None:
    """Download creative images using Playwright's APIRequestContext — inherits
    the browser session cookies + Origin headers that fbcdn checks. A plain
    httpx GET often 403s; this path works because Meta sees a "live" session."""
    api = context.request
    for card in cards:
        urls = card.get("creative_image_urls") or []
        paths: list[str] = []
        for idx, u in enumerate(urls):
            try:
                r = api.get(u, timeout=15_000)
                if not r.ok:
                    log.debug("image %s returned %d", u, r.status)
                    continue
                body = r.body()
                if len(body) < 2000:
                    continue
                ct = (r.headers.get("content-type") or "").lower()
                ext = ".png" if "png" in ct else ".webp" if "webp" in ct else ".jpg"
                out = asset_dir / card["ad_archive_id"] / f"image_{idx}{ext}"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(body)
                paths.append(str(out))
            except Exception as e:
                log.debug("image download failed %s: %s", u, e)
        card["local_creative_paths"] = paths


def _process_card_videos_playwright(cards: list[dict[str, Any]], asset_dir: Path, context) -> None:
    """Download mp4s + extract frames + (optionally) transcribe audio for every
    card with `creative_video_urls`. Runs INLINE with the scrape because Meta's
    fbcdn video URLs expire within hours — defer to later cron = 403 forever.

    On per-video failure, logs and continues. Stamps `card["local_video_path"]`
    and `card["video_meta"]` so the runner can persist a `creatives` row.
    """
    # Import inside the function so the scraper still loads when ffmpeg is
    # missing — we'll just no-op on video processing in that case.
    try:
        from ..analysis.video import process_video, check_ffmpeg
    except Exception as e:
        log.debug("video module unavailable: %s", e)
        return
    ok, detail = check_ffmpeg()
    if not ok:
        log.info("ffmpeg not available (%s); skipping video processing this run", detail)
        return
    api = context.request
    for card in cards:
        urls = card.get("creative_video_urls") or []
        if not urls:
            continue
        try:
            sidecar = process_video(
                api_request_context=api,
                video_urls=urls,
                asset_dir=asset_dir,
                ad_archive_id=card["ad_archive_id"],
            )
            if sidecar:
                card["local_video_path"] = sidecar.get("video_path")
                card["video_meta"] = sidecar
        except Exception as e:
            log.warning("video pipeline failed for ad %s: %s", card.get("ad_archive_id"), e)
