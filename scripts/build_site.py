#!/usr/bin/env python3
"""build_site.py — assemble a self-contained static site (dist/) for Netlify.

The HTML dashboards `intel dashboard` emits are NOT portable: they embed
absolute filesystem paths (relativized in JS for file:// browsing) and pull
images from the gitignored data/*_assets/ tree. That works when opened locally
but 404s on any web host. This script makes them hostable:

  * discovers built dashboards under reports/**,
  * copies ONLY the assets each one references into a shared per-(deployment,
    date) `assets/` folder under dist/,
  * rewrites every reference — absolute paths, ../ dotted-relative paths,
    data-asset-path / data-thumb-path attributes, and the embedded JSON
    `imgPath`s — to portable `../assets/...`,
  * generates a dist/index.html landing page linking everything.

Because the assets are local + large + gitignored, the build runs HERE (not in
Netlify CI). Deploy the resulting dist/ with `netlify deploy --dir dist` or
drag-and-drop. See NETLIFY.md.

Usage:
  python3 scripts/build_site.py                 # latest date per deployment
  python3 scripts/build_site.py --all           # every date
  python3 scripts/build_site.py --out dist       # output dir (default dist/)
"""
from __future__ import annotations

import argparse
import html
import re
import shutil
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"

# Any quoted path that points at an asset file under a data/ tree, in either
# absolute or ../-relative form. Filenames/dirs may contain spaces (the repo
# path does), so we only stop at the surrounding quote.
ASSET_RE = re.compile(
    r"""(["'])([^"']*?/?data/[^"']+?\.(?:jpg|jpeg|png|webp|gif|mp4|svg))\1""",
    re.IGNORECASE,
)

VARIANT_LABELS = {
    "dashboard": "Dashboard",
    "dashboard-v2": "Dashboard v2 (dark)",
    "with-google-dashboard": "Dashboard + Google ATC",
    "with-google-dashboard-v2": "Dashboard + Google ATC (v2)",
}


def discover() -> dict[str, dict[str, dict[str, Path]]]:
    """Return {deployment: {date: {variant_slug: index.html path}}}.

    Layouts handled:
      reports/<date>/dashboard/index.html                    -> deployment 'bobs'
      reports/<date>/with-google/dashboard/index.html        -> 'bobs', variant 'with-google-dashboard'
      reports/<name>/<date>/dashboard/index.html             -> deployment '<name>'
      reports/<name>/<date>/with-google/dashboard-v2/index.html
    """
    out: dict[str, dict[str, dict[str, Path]]] = defaultdict(lambda: defaultdict(dict))
    date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    for idx in REPORTS.rglob("index.html"):
        parts = idx.relative_to(REPORTS).parts
        if "dashboard" not in " ".join(parts):
            continue
        # locate the date segment
        date = next((p for p in parts if date_re.match(p)), None)
        if not date:
            continue
        di = parts.index(date)
        deployment = "bobs" if di == 0 else parts[0]
        tail = parts[di + 1 : -1]  # between date and index.html
        # tail is like ('dashboard',) or ('with-google','dashboard-v2')
        variant = "-".join(tail)
        out[deployment][date][variant] = idx
    return out


def rewrite_html(idx: Path, dest_dir: Path, assets_dir: Path) -> tuple[int, int]:
    """Copy referenced assets into assets_dir and rewrite refs to ../assets/...
    Returns (n_refs_rewritten, n_assets_copied)."""
    text = idx.read_text(encoding="utf-8")
    html_dir = idx.parent
    seen: dict[str, str] = {}  # original path string -> replacement
    copied = 0

    for m in ASSET_RE.finditer(text):
        raw = m.group(2)
        if raw in seen:
            continue
        # Resolve to a real file on disk.
        if raw.startswith("/"):
            src = Path(raw)
        else:
            src = (html_dir / raw).resolve()
        # Portable path = everything after the LAST 'data/' segment.
        marker = raw.rsplit("/data/", 1)
        if len(marker) != 2:
            continue
        rel_under_data = marker[1]  # e.g. 'philo_assets/creative/x.jpg'
        replacement = "../assets/" + rel_under_data
        seen[raw] = replacement
        if src.exists():
            dst = assets_dir / rel_under_data
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.exists():
                shutil.copy2(src, dst)
                copied += 1

    # Apply replacements (longest first to avoid partial overlaps).
    for raw in sorted(seen, key=len, reverse=True):
        text = text.replace(raw, seen[raw])

    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / "index.html").write_text(text, encoding="utf-8")
    return len(seen), copied


def brand_count(idx: Path) -> str:
    """Best-effort: pull the brand count out of the embedded data for the card."""
    try:
        t = idx.read_text(encoding="utf-8")
        m = re.search(r'"brands?"\s*:\s*\[', t)
        # cheap: count occurrences of '"competitor_id"' unique-ish; fallback blank
        ids = set(re.findall(r'"competitorId"\s*:\s*"([^"]+)"', t))
        if ids:
            return f"{len(ids)} brands"
    except Exception:
        pass
    return ""


LANDING_CSS = """
:root { --bg:#0b0d12; --card:#151922; --fg:#e6e9ef; --muted:#8a93a3; --accent:#5b8cff; --line:#222836; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
.wrap { max-width: 980px; margin: 0 auto; padding: 48px 24px 80px; }
h1 { font-size: 30px; margin: 0 0 4px; letter-spacing:-0.02em; }
.sub { color: var(--muted); margin: 0 0 36px; }
.dep { margin: 0 0 28px; }
.dep h2 { font-size: 18px; margin: 0 0 2px; text-transform: capitalize; }
.dep .meta { color: var(--muted); font-size: 13px; margin: 0 0 12px; }
.cards { display: grid; grid-template-columns: repeat(auto-fill,minmax(240px,1fr)); gap: 12px; }
a.card { display:block; text-decoration:none; color:var(--fg); background:var(--card); border:1px solid var(--line);
         border-radius:10px; padding:16px 18px; transition: border-color .15s, transform .15s; }
a.card:hover { border-color: var(--accent); transform: translateY(-2px); }
a.card .v { font-weight:600; margin-bottom:4px; }
a.card .d { color: var(--muted); font-size: 12px; }
footer { color: var(--muted); font-size: 12px; margin-top: 40px; border-top:1px solid var(--line); padding-top:16px; }
"""


def build_landing(site: dict, out: Path, generated_for: str) -> None:
    deps_order = [d for d in ["philo", "bobs", "trex", "revlon"] if d in site]
    deps_order += [d for d in site if d not in deps_order]
    sections = []
    for dep in deps_order:
        dates = site[dep]
        latest = sorted(dates)[-1]
        cards = []
        for date in sorted(dates, reverse=True):
            for variant, dest_rel in sorted(dates[date].items()):
                label = VARIANT_LABELS.get(variant, variant)
                cards.append(
                    f'<a class="card" href="{html.escape(dest_rel)}">'
                    f'<div class="v">{html.escape(label)}</div>'
                    f'<div class="d">{html.escape(date)}</div></a>'
                )
        sections.append(
            f'<div class="dep"><h2>{html.escape(dep)}</h2>'
            f'<div class="meta">latest: {html.escape(latest)} · {len(dates)} report date(s)</div>'
            f'<div class="cards">{"".join(cards)}</div></div>'
        )
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Competitive Intelligence — Dashboards</title>
<style>{LANDING_CSS}</style></head>
<body><div class="wrap">
<h1>Competitive Intelligence Dashboards</h1>
<p class="sub">Creative &amp; competitive ad intelligence across {len(deps_order)} competitive set(s).</p>
{''.join(sections)}
<footer>Static export · generated {html.escape(generated_for)} · built by scripts/build_site.py</footer>
</div></body></html>"""
    (out / "index.html").write_text(doc, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="dist")
    ap.add_argument("--all", action="store_true", help="include every date (default: latest per deployment)")
    ap.add_argument("--date-label", default="locally", help="label shown in landing footer")
    args = ap.parse_args()

    out = (ROOT / args.out).resolve()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    discovered = discover()
    if not discovered:
        print("no dashboards found under reports/ — run a dashboard build first.")
        return

    # site[dep][date][variant] = relative href into dist
    site: dict[str, dict[str, dict[str, str]]] = defaultdict(lambda: defaultdict(dict))
    total_refs = total_assets = total_dash = 0

    for dep, dates in discovered.items():
        chosen_dates = sorted(dates) if args.all else [sorted(dates)[-1]]
        for date in chosen_dates:
            assets_dir = out / dep / date / "assets"
            for variant, idx in dates[date].items():
                dest_dir = out / dep / date / variant
                refs, copied = rewrite_html(idx, dest_dir, assets_dir)
                href = f"{dep}/{date}/{variant}/index.html"
                site[dep][date][variant] = href
                total_refs += refs
                total_assets += copied
                total_dash += 1
                print(f"  {dep}/{date}/{variant}: {refs} refs, {copied} assets copied")

    build_landing(site, out, args.date_label)
    size_mb = sum(f.stat().st_size for f in out.rglob("*") if f.is_file()) / (1024 * 1024)
    print(
        f"\nbuilt {total_dash} dashboard(s) across {len(site)} deployment(s) → {out}\n"
        f"  {total_refs} refs rewritten · {total_assets} asset files · {size_mb:.1f} MB total\n"
        f"  landing page: {out}/index.html"
    )


if __name__ == "__main__":
    main()
