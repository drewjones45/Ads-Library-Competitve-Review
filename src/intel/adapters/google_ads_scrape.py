"""Best-effort headless-browser scrape of Google Ads Transparency Center (ATC).

ATC has no public API. This is the FALLBACK path — the *supported* path is the
SerpApi provider in `google_ads.py`. `adstransparency.google.com` is an
undocumented React SPA backed by an internal RPC (`SearchService/SearchCreatives`)
that returns `batchexecute`-style nested-array JSON. The exact array indices are
version-dependent and change without notice, so everything here is deliberately
defensive: it captures the RPC response, extracts per-entry conservatively, falls
back to the rendered DOM, saves the raw payload for inspection, and returns
partial data + logs rather than crashing the run (mirrors `meta_ads_scrape.py`).

Returns a list of "raw creative" dicts in the shape `google_ads._normalize_creative`
expects:
  {creative_id, ad_format, advertiser_name, headlines[], descriptions[],
   image_urls[], video_urls[], link_url, first_shown, last_shown, regions[]}
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("intel.google_ads_scrape")

ATC_URL = "https://adstransparency.google.com/advertiser/{advertiser_id}?region={region}"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# ATC ids: advertiser 'AR<digits>', creative 'CR<digits>'.
_CREATIVE_ID_RE = re.compile(r"\bCR\d{6,}\b")
# Image/video CDNs Google serves creatives from.
_IMG_HOST_RE = re.compile(
    r"https?://[^\s\"']*(?:googleusercontent\.com|tpc\.googlesyndication\.com|"
    r"gstatic\.com|s0\.2mdn\.net|ytimg\.com)[^\s\"']*",
    re.I,
)
_VIDEO_HOST_RE = re.compile(r"https?://[^\s\"']*(?:youtube\.com|youtu\.be)[^\s\"']*", re.I)
# An http(s) link that isn't a Google asset/host — the ad's landing destination.
_EXTERNAL_LINK_RE = re.compile(r"https?://[^\s\"']+", re.I)
_GOOGLE_HOST = re.compile(r"(google\.com|gstatic\.com|googlesyndication\.com|googleusercontent\.com|2mdn\.net|youtube)", re.I)


def scrape_advertiser_ads(
    advertiser_id: str,
    region: str = "US",
    *,
    asset_dir: Path,
    max_creatives: int = 200,
    max_scrolls: int = 40,
    headless: bool = True,
) -> list[dict[str, Any]]:
    """Open ATC for `advertiser_id`, scroll until the creative grid stops growing,
    and return raw-creative dicts. Brittle by nature — returns [] on a block/empty
    page rather than raising. Image bytes are NOT downloaded here; the adapter does
    that (Google creative CDNs are public, so a plain httpx GET suffices)."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:  # pragma: no cover - import guard
        raise RuntimeError(f"playwright not installed: {e}") from e

    asset_dir.mkdir(parents=True, exist_ok=True)
    url = ATC_URL.format(advertiser_id=advertiser_id, region=region)
    rpc_payloads: list[Any] = []

    def _on_response(resp) -> None:
        try:
            u = resp.url
            if "SearchCreatives" in u or "LookupCreative" in u or "GetCreativeById" in u:
                body = resp.text()
                parsed = _safe_json(body)
                if parsed is not None:
                    rpc_payloads.append(parsed)
        except Exception as e:
            log.debug("rpc capture failed: %s", e)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(user_agent=USER_AGENT, viewport={"width": 1366, "height": 900})
        page = context.new_page()
        page.on("response", _on_response)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(3_000)  # let the SPA hydrate + fire SearchCreatives
            # Scroll until the creative count stabilizes (same idea as the Meta scrape).
            stable = 0
            seen = 0
            for _ in range(max_scrolls):
                page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
                time.sleep(1.2)
                n = len(set(_CREATIVE_ID_RE.findall(page.content())))
                if n == seen:
                    stable += 1
                    if stable >= 3:
                        break
                else:
                    stable, seen = 0, n
                if n >= max_creatives:
                    break

            # Primary: parse the captured SearchCreatives RPC payloads.
            raw: dict[str, dict[str, Any]] = {}
            for payload in rpc_payloads:
                for c in _extract_from_rpc(payload):
                    cid = c.get("creative_id")
                    if cid and cid not in raw:
                        raw[cid] = c
            # Save the raw RPC for inspection / index-mapping refinement.
            if rpc_payloads:
                try:
                    (asset_dir / "_atc_search_raw.json").write_text(
                        json.dumps(rpc_payloads, default=str)[:5_000_000], encoding="utf-8"
                    )
                except Exception:
                    pass

            # Fallback / complement: harvest rendered DOM tiles.
            if len(raw) < 1:
                for c in _harvest_dom(page):
                    cid = c.get("creative_id")
                    if cid and cid not in raw:
                        raw[cid] = c

            if not raw:
                # Distinguish a real block from an empty advertiser.
                body = (page.inner_text("body") or "").lower()
                if "no ads" in body or "doesn't have any ads" in body or "no results" in body:
                    log.info("advertiser %s has no ATC ads in %s", advertiser_id, region)
                else:
                    log.warning(
                        "ATC scrape for %s returned 0 creatives (possible block or shape change). "
                        "Raw RPC saved to %s for inspection.", advertiser_id, asset_dir / "_atc_search_raw.json",
                    )
                return []
            return list(raw.values())[:max_creatives]
        finally:
            context.close()
            browser.close()


def _safe_json(text: str) -> Any | None:
    """Parse a Google RPC body, stripping the XSSI prefix `)]}'` if present."""
    if not text:
        return None
    t = text.lstrip()
    if t.startswith(")]}'"):
        t = t.split("\n", 1)[1] if "\n" in t else t[4:]
    try:
        return json.loads(t)
    except Exception:
        return None


def _extract_from_rpc(payload: Any) -> list[dict[str, Any]]:
    """Best-effort per-entry extraction from a SearchCreatives payload.

    The payload is a deeply nested array. We locate the list of creative entries
    (the longest list whose items contain a 'CR…' creative id in their subtree)
    and, for EACH entry, collect ids/images/videos/text/links from *that entry's
    subtree only* — keeping fields associated with the right creative instead of
    harvesting globally. Indices aren't hardcoded, so this survives reshuffles.
    """
    entries = _find_entry_list(payload)
    out: list[dict[str, Any]] = []
    for entry in entries:
        acc: dict[str, list] = {"ids": [], "images": [], "videos": [], "links": [], "texts": []}
        _walk_collect(entry, acc)
        if not acc["ids"]:
            continue
        cid = acc["ids"][0]
        texts = [t for t in acc["texts"] if t and len(t) <= 200]
        # Heuristic title/body split: short lines → headlines, longer → descriptions.
        headlines = [t for t in texts if len(t) <= 40][:15]
        descriptions = [t for t in texts if len(t) > 40][:6]
        link = _first_external_link(acc["links"])
        out.append({
            "creative_id": cid,
            "ad_format": _classify_format(acc["images"], texts, acc["videos"]),
            "advertiser_name": None,
            "headlines": headlines,
            "descriptions": descriptions,
            "image_urls": _dedup(acc["images"]),
            "video_urls": _dedup(acc["videos"]),
            "link_url": link,
            "first_shown": None,
            "last_shown": None,
            "regions": [],
        })
    return out


def _find_entry_list(payload: Any) -> list[Any]:
    """Return the most plausible list of creative entries: the longest list whose
    elements each contain a creative id somewhere in their subtree."""
    best: list[Any] = []

    def visit(node: Any) -> None:
        nonlocal best
        if isinstance(node, list):
            if node and all(isinstance(x, (list, dict)) for x in node):
                hits = sum(1 for x in node if _contains_creative_id(x))
                if hits >= max(1, len(node) // 2) and len(node) > len(best):
                    best = node
            for x in node:
                visit(x)
        elif isinstance(node, dict):
            for v in node.values():
                visit(v)

    visit(payload)
    return best


def _contains_creative_id(node: Any) -> bool:
    if isinstance(node, str):
        return bool(_CREATIVE_ID_RE.search(node))
    if isinstance(node, list):
        return any(_contains_creative_id(x) for x in node)
    if isinstance(node, dict):
        return any(_contains_creative_id(v) for v in node.values())
    return False


def _walk_collect(node: Any, acc: dict[str, list]) -> None:
    """Recursively pull ids/images/videos/links/texts from one entry's subtree."""
    if isinstance(node, str):
        m = _CREATIVE_ID_RE.search(node)
        if m:
            acc["ids"].append(m.group(0))
        for u in _IMG_HOST_RE.findall(node):
            acc["images"].append(u)
        for u in _VIDEO_HOST_RE.findall(node):
            acc["videos"].append(u)
        if node.startswith("http") and not _GOOGLE_HOST.search(node):
            acc["links"].append(node)
        # Plain ad copy: has spaces/letters, isn't a URL or token.
        if not node.startswith("http") and re.search(r"[A-Za-z]", node) and " " in node.strip():
            acc["texts"].append(node.strip())
    elif isinstance(node, list):
        for x in node:
            _walk_collect(x, acc)
    elif isinstance(node, dict):
        for v in node.values():
            _walk_collect(v, acc)


def _harvest_dom(page) -> list[dict[str, Any]]:
    """DOM fallback: pull creative tiles when the RPC capture came up empty."""
    try:
        tiles = page.evaluate(
            """() => {
                const out = [];
                const seen = new Set();
                const anchors = Array.from(document.querySelectorAll('a[href*="/creative/"]'));
                for (const a of anchors) {
                    const m = (a.href || '').match(/creative\\/(CR\\d+)/);
                    if (!m || seen.has(m[1])) continue;
                    seen.add(m[1]);
                    const card = a.closest('[class]') || a.parentElement || a;
                    const imgs = Array.from(card.querySelectorAll('img')).map(i => i.src).filter(s => s && s.startsWith('http'));
                    const text = (card.innerText || '').trim();
                    out.push({creative_id: m[1], images: imgs, text: text});
                }
                return out;
            }"""
        )
    except Exception as e:
        log.debug("DOM harvest failed: %s", e)
        return []
    out = []
    for t in tiles or []:
        texts = [ln.strip() for ln in (t.get("text") or "").splitlines() if ln.strip()]
        out.append({
            "creative_id": t.get("creative_id"),
            "ad_format": _classify_format(t.get("images") or [], texts, []),
            "advertiser_name": None,
            "headlines": [x for x in texts if len(x) <= 40][:15],
            "descriptions": [x for x in texts if len(x) > 40][:6],
            "image_urls": _dedup([u for u in (t.get("images") or []) if _IMG_HOST_RE.search(u)]),
            "video_urls": [],
            "link_url": None,
            "first_shown": None,
            "last_shown": None,
            "regions": [],
        })
    return out


def _classify_format(images: list, texts: list, videos: list) -> str:
    if videos:
        return "video"
    if images:
        return "image"
    if texts:
        return "text"
    return "unknown"


def _first_external_link(links: list[str]) -> str | None:
    for u in links:
        if _EXTERNAL_LINK_RE.match(u) and not _GOOGLE_HOST.search(u):
            return u
    return None


def _dedup(seq: list[str]) -> list[str]:
    out, seen = [], set()
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out
