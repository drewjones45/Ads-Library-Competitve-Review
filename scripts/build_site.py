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

Asset refs are resolved against THIS repo's data/ tree, not the absolute paths
baked into the HTML by the machine that generated it — so a Netlify CI build
from a git checkout produces the same dist/ as a local build, as long as the
referenced data/*_assets/ are committed. (They are for philo/trex/wegmans; the
bobs deployment's data/creative/ is gitignored, so those refs 404 in CI — the
build warns about any it cannot resolve.) See NETLIFY.md.

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
    "with-tv-dashboard": "Dashboard + Google + TV",
    "with-tv-dashboard-v2": "Dashboard + Google + TV (v2)",
    # Owned-account creative performance (first-party spend/ROAS joined to
    # creative attributes). Self-contained HTML — no asset refs to rewrite.
    "performance-dashboard": "Creative performance (owned accounts)",
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


def rewrite_html(idx: Path, dest_dir: Path, assets_dir: Path) -> tuple[int, int, list[str]]:
    """Copy referenced assets into assets_dir and rewrite refs to ../assets/...
    Returns (n_refs_rewritten, n_assets_copied, unresolved_asset_paths)."""
    text = idx.read_text(encoding="utf-8")
    html_dir = idx.parent
    seen: dict[str, str] = {}  # original path string -> replacement
    copied = 0
    missing: list[str] = []

    for m in ASSET_RE.finditer(text):
        raw = m.group(2)
        if raw in seen:
            continue
        # Portable path = everything after the LAST 'data/' segment.
        marker = raw.rsplit("/data/", 1)
        if len(marker) != 2:
            continue
        rel_under_data = marker[1]  # e.g. 'philo_assets/creative/x.jpg'

        # Resolve to a real file on disk. The dashboards embed ABSOLUTE paths
        # from the machine that generated them, so an absolute `raw` resolves
        # only on that machine — in a fresh checkout (Netlify CI) it does not
        # exist. Falling back to this repo's own data/ tree is what lets a CI
        # build produce the same dist/ as a local one; without it the ref is
        # still rewritten but the file is never copied, and the site 404s.
        candidates = [Path(raw) if raw.startswith("/") else (html_dir / raw).resolve()]
        candidates.append(ROOT / "data" / rel_under_data)
        src = next((c for c in candidates if c.exists()), None)

        replacement = "../assets/" + rel_under_data
        seen[raw] = replacement
        if src is None:
            missing.append(rel_under_data)
            continue
        dst = assets_dir / rel_under_data
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists():
            shutil.copy2(src, dst)
            copied += 1

    # Apply replacements (longest first to avoid partial overlaps).
    for raw in sorted(seen, key=len, reverse=True):
        text = text.replace(raw, seen[raw])

    text = inject_noindex(text)

    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / "index.html").write_text(text, encoding="utf-8")
    return len(seen), copied, missing


# Belt-and-braces with the X-Robots-Tag header in netlify.toml: the header is
# authoritative, but the meta tag travels with the file, so a page that gets
# copied, mirrored or opened from disk still declares itself un-indexable.
NOINDEX_META = (
    '<meta name="robots" content="noindex, nofollow, noarchive, nosnippet, noimageindex">'
    '<meta name="googlebot" content="noindex, nofollow">'
)
_HEAD_RE = re.compile(r"<head[^>]*>", re.IGNORECASE)


def inject_noindex(text: str) -> str:
    """Insert the robots meta tags right after <head>, idempotently.

    These dashboards hold client competitive analysis and first-party ad spend,
    and the host has no auth — so nothing here should ever land in a search
    index. Falls back to prepending when a document has no <head> at all.
    """
    if 'name="robots"' in text:
        return text
    m = _HEAD_RE.search(text)
    if m:
        return text[: m.end()] + NOINDEX_META + text[m.end():]
    return NOINDEX_META + text


def _copy_strategy(date_root: Path, dest: Path) -> int:
    """Copy every strategy .html/.pdf under a report date into dest (flat).

    The strategy HTML is self-contained (inline base64 thumbnails), so no ref
    rewriting is needed. Both a top-level strategy/ and a nested
    with-google/strategy/ (bobs) flatten into the same dest folder; their
    filenames differ, so they coexist."""
    copied = 0
    for strat in date_root.rglob("strategy"):
        if not strat.is_dir():
            continue
        for f in sorted(strat.iterdir()):
            if f.is_file() and f.suffix.lower() in (".html", ".pdf"):
                dest.mkdir(parents=True, exist_ok=True)
                if f.suffix.lower() == ".html":
                    # Stamp the robots meta in rather than copying verbatim, so
                    # strategy docs carry the same no-index declaration as the
                    # dashboards. (PDFs can't carry a meta tag — the
                    # X-Robots-Tag header in netlify.toml is what covers those.)
                    (dest / f.name).write_text(
                        inject_noindex(f.read_text(encoding="utf-8")), encoding="utf-8"
                    )
                else:
                    shutil.copy2(f, dest / f.name)
                copied += 1
    return copied


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


def write_robots(out: Path) -> None:
    """Blanket crawl disallow for the deployed site.

    Third line of defence after the X-Robots-Tag header and the per-page meta
    tag. Well-behaved crawlers read this before requesting anything, so it stops
    the fetch rather than just the indexing. `Disallow: /` applies to every
    user-agent, including the AI crawlers that honour robots.txt.
    """
    (out / "robots.txt").write_text(
        "# Client competitive analysis + first-party ad performance. Not for indexing.\n"
        "User-agent: *\n"
        "Disallow: /\n",
        encoding="utf-8",
    )


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
{NOINDEX_META}
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
    all_missing: list[str] = []

    total_strategy = 0
    for dep, dates in discovered.items():
        chosen_dates = sorted(dates) if args.all else [sorted(dates)[-1]]
        for date in chosen_dates:
            assets_dir = out / dep / date / "assets"
            date_root = None
            for variant, idx in dates[date].items():
                dest_dir = out / dep / date / variant
                refs, copied, missing = rewrite_html(idx, dest_dir, assets_dir)
                all_missing.extend(missing)
                href = f"{dep}/{date}/{variant}/index.html"
                site[dep][date][variant] = href
                total_refs += refs
                total_assets += copied
                total_dash += 1
                warn = f"  \u26a0 {len(missing)} UNRESOLVED" if missing else ""
                print(f"  {dep}/{date}/{variant}: {refs} refs, {copied} assets copied{warn}")
                if date_root is None:
                    date_root = next((p for p in idx.parents if p.name == date), None)

            # Strategy docs (HTML + PDF) live in a `strategy/` folder under the
            # report date. The dashboards link them via ../strategy/<name>.(html|pdf)
            # \u2014 but build_site only discovers index.html dashboards, so without this
            # the link 404s on the deployed site (true for bobs/trex too until now).
            # The strategy HTML is self-contained (base64 thumbnails), so a verbatim
            # copy into dist/<dep>/<date>/strategy/ is enough; the flattened variant
            # dirs all sit one level up, so ../strategy/ resolves for every one.
            if date_root is not None:
                strat_dest = out / dep / date / "strategy"
                n = _copy_strategy(date_root, strat_dest)
                if n:
                    total_strategy += n
                    print(f"  {dep}/{date}/strategy: {n} file(s) copied")

    build_landing(site, out, args.date_label)
    write_robots(out)
    size_mb = sum(f.stat().st_size for f in out.rglob("*") if f.is_file()) / (1024 * 1024)
    print(
        f"\nbuilt {total_dash} dashboard(s) across {len(site)} deployment(s) → {out}\n"
        f"  {total_refs} refs rewritten · {total_assets} asset files · "
        f"{total_strategy} strategy file(s) · {size_mb:.1f} MB total\n"
        f"  landing page: {out}/index.html"
    )

    # Never fail silently: an unresolved ref is still rewritten to ../assets/...,
    # so it becomes a 404 on the deployed site. Assets under a gitignored tree
    # (e.g. the bobs deployment's data/creative/) are legitimately absent in a CI
    # checkout, so warn loudly rather than failing the whole build.
    if all_missing:
        by_tree: dict[str, int] = defaultdict(int)
        for rel in all_missing:
            by_tree[rel.split("/")[0]] += 1
        print(f"\n  ⚠ {len(all_missing)} asset ref(s) could not be resolved — these WILL 404:")
        for tree, n in sorted(by_tree.items(), key=lambda kv: -kv[1]):
            print(f"      {tree}: {n}")
        print("    (not in this checkout — gitignored, or not committed)")


if __name__ == "__main__":
    main()
