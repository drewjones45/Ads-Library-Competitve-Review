#!/usr/bin/env python
"""Resolve + verify Meta Ad Library numeric page IDs from public Facebook page slugs.

Codifies the only resolution method that actually works, as documented in
config/competitors_jdsports.yaml:

  1. ✗ Graph free-text `search_terms` — spam-ranked, unusable for discovery.
  2. ✗ Graph `ads_archive` + `search_page_ids` — reports 0 ACTIVE US ads for most
       real pages (commercial-coverage gap). Never use it to conclude "no ads".
  3. ✓ Load facebook.com/<slug> in Playwright, read the numeric id out of the
       embedded page JSON, then CONFIRM by running the real ingest path
       (meta_ads_scrape.scrape_page_ads) against that id.

Step 3's confirmation is the point. A page id that parses but returns no ad cards
under the expected brand name is NOT verified — that is exactly how the JD Sports
deployment nearly benchmarked UK creative against US retailers.

Requires: pip install -e '.[browser]' && playwright install chromium

Usage:
  python scripts/resolve_meta_page_ids.py edwardjones charlesschwab merrilllynch
  python scripts/resolve_meta_page_ids.py --verify-only 202226521097
  python scripts/resolve_meta_page_ids.py --slugs-file slugs.txt --json out.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Ordered by trustworthiness. delegate_page.id is the one the JD run relied on.
ID_PATTERNS = [
    ("delegate_page", r'"delegate_page"\s*:\s*\{\s*"id"\s*:\s*"(\d{6,})"'),
    ("pageID", r'"pageID"\s*:\s*"(\d{6,})"'),
    ("entity_id", r'"entity_id"\s*:\s*"(\d{6,})"'),
    ("page_id", r'"page_id"\s*:\s*"(\d{6,})"'),
    ("fb://page", r'fb://page/(\d{6,})'),
]


def extract_page_id(html: str) -> tuple[str | None, str | None]:
    """Return (page_id, which_pattern_matched)."""
    for label, pat in ID_PATTERNS:
        m = re.search(pat, html)
        if m:
            return m.group(1), label
    return None, None


def extract_page_title(html: str) -> str | None:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    if not m:
        return None
    return re.sub(r"\s+", " ", m.group(1)).strip() or None


def resolve_slug(slug: str, *, timeout_ms: int = 45_000) -> dict:
    """Load facebook.com/<slug> and pull the numeric page id out of embedded JSON."""
    from playwright.sync_api import sync_playwright

    out: dict = {"slug": slug, "page_id": None, "matched_via": None,
                 "page_title": None, "error": None}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=UA, locale="en-US")
            page = ctx.new_page()
            page.goto(f"https://www.facebook.com/{slug}",
                      wait_until="domcontentloaded", timeout=timeout_ms)
            # Cookie/login interstitials do not block the embedded JSON, so no
            # dismissal is needed here — we only need the HTML payload.
            html = page.content()
            browser.close()
    except Exception as e:  # noqa: BLE001 — report, never abort the whole batch
        out["error"] = f"{type(e).__name__}: {e}"
        return out

    out["page_id"], out["matched_via"] = extract_page_id(html)
    out["page_title"] = extract_page_title(html)
    if not out["page_id"]:
        out["error"] = (
            "no numeric id in page HTML — page may be login-gated, renamed, or "
            "not exist. Try the Ad Library UI: search the brand, open any card, "
            "and read view_all_page_id= from the URL."
        )
    return out


def verify_page_id(page_id: str, *, competitor_id: str = "_verify",
                   max_cards: int = 5) -> dict:
    """Confirm a page id by running the REAL ingest path against it.

    This is the step that makes an id 'VERIFIED' rather than merely parsed.

    Verification is deliberately cheap and side-effect-free: images are NOT
    downloaded and assets land in a throwaway temp dir, so this never pollutes
    INTEL_DATA_DIR with a wrong page's creative.
    """
    import tempfile

    out: dict = {"page_id": page_id, "cards": 0, "page_names": [],
                 "currencies": [], "error": None}
    try:
        from intel.adapters.meta_ads_scrape import scrape_page_ads
    except Exception as e:  # noqa: BLE001
        out["error"] = f"cannot import scrape path ({type(e).__name__}: {e})"
        return out

    try:
        with tempfile.TemporaryDirectory(prefix=f"pageid-verify-{competitor_id}-") as td:
            cards = scrape_page_ads(
                page_id,
                country="US",
                asset_dir=Path(td),
                max_cards=max_cards,
                max_scrolls=2,
                download_images=False,
            ) or []
        out["cards"] = len(cards)
        out["page_names"] = sorted({c.get("page_name") for c in cards if c.get("page_name")})
        out["currencies"] = sorted({c.get("currency") for c in cards if c.get("currency")})
        if not cards:
            out["error"] = (
                "id parsed but returned ZERO ad cards — NOT verified. Either the "
                "page runs no ads in this country, or it is the wrong page."
            )
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("slugs", nargs="*", help="facebook.com/<slug> page slugs")
    ap.add_argument("--slugs-file", help="file with one slug per line (# comments ok)")
    ap.add_argument("--verify-only", nargs="+", metavar="PAGE_ID",
                    help="skip resolution; just verify these numeric ids")
    ap.add_argument("--no-verify", action="store_true",
                    help="resolve ids but skip the scrape confirmation step")
    ap.add_argument("--max-cards", type=int, default=5,
                    help="cards to pull during verification (default 5 — keep small)")
    ap.add_argument("--json", metavar="PATH", help="also write results as JSON")
    args = ap.parse_args()

    slugs = list(args.slugs)
    if args.slugs_file:
        for line in Path(args.slugs_file).read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                slugs.append(line)

    if not slugs and not args.verify_only:
        ap.error("give at least one slug, --slugs-file, or --verify-only")

    results: list[dict] = []

    for slug in slugs:
        print(f"\n── resolving {slug} ──", flush=True)
        r = resolve_slug(slug)
        if r["page_id"]:
            print(f"   page_id   {r['page_id']}  (via {r['matched_via']})")
            print(f"   title     {r['page_title']}")
        else:
            print(f"   ✗ {r['error']}")
        if r["page_id"] and not args.no_verify:
            print("   verifying via live scrape…", flush=True)
            v = verify_page_id(r["page_id"], competitor_id=slug,
                               max_cards=args.max_cards)
            r["verification"] = v
            if v["error"]:
                print(f"   ⚠ NOT VERIFIED: {v['error']}")
            else:
                print(f"   ✓ VERIFIED — {v['cards']} cards, "
                      f"page_names={v['page_names']}, currencies={v['currencies']}")
        results.append(r)

    for pid in args.verify_only or []:
        print(f"\n── verifying {pid} ──", flush=True)
        v = verify_page_id(pid, max_cards=args.max_cards)
        if v["error"]:
            print(f"   ⚠ NOT VERIFIED: {v['error']}")
        else:
            print(f"   ✓ VERIFIED — {v['cards']} cards, "
                  f"page_names={v['page_names']}, currencies={v['currencies']}")
        results.append({"page_id": pid, "verification": v})

    print("\n" + "=" * 62)
    print("SUMMARY — paste VERIFIED ids into the competitors yaml")
    print("=" * 62)
    for r in results:
        pid = r.get("page_id") or "—"
        v = r.get("verification") or {}
        if v and not v.get("error"):
            status = f"VERIFIED ({v['cards']} cards; {', '.join(v['page_names']) or 'no name'})"
        elif v:
            status = "NOT VERIFIED"
        elif r.get("error"):
            status = "UNRESOLVED"
        else:
            status = "resolved, unverified"
        print(f"  {r.get('slug', '(verify-only)'):<24} {pid:<20} {status}")

    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2))
        print(f"\nwrote {args.json}")

    # Non-zero if anything failed to reach VERIFIED — makes this gate-able in a script.
    ok = all((r.get("verification") or {}).get("error") is None
             and r.get("page_id") for r in results) if results else False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
