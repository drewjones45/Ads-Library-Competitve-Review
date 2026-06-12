"""intel — CLI for agentic competitive intelligence.

Examples:
  intel init                           # create db, register competitors from yaml
  intel ingest                         # run all sources for all competitors
  intel ingest --competitor glossier   # one competitor
  intel ads --days 7                   # show new ads in window
  intel offers --days 14
  intel brief --days 7                 # write a weekly briefing
  intel whitespace --vertical skincare
  intel analyze-creative path/to.jpg
  intel dashboard --open-after         # generate + open HTML dashboard
  intel agent "what's the biggest competitive move this week?"
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from .agent import run_agent
from .analysis.creative import analyze_creative_image
from .analysis.creative_batch import analyze_pending
from .analysis.offers import extract_offers_from_text
from .analysis.themes import cluster_hooks
from .config import DATA_DIR, DB_PATH, load_competitors
from .runner import ingest_all, ingest_competitor
from .storage import connect, init_db, upsert_competitor
from .synthesis.briefing import MissingApiKey, generate_briefing
from .synthesis.creative_readout import cross_set_comparison, per_brand_readout
from .synthesis.dashboard import DEFAULT_PRODUCT, build_dashboard
from .synthesis.dashboard_v2 import DEFAULT_PRODUCT_V2, build_dashboard_v2
from .synthesis.whitespace import detect_whitespace

console = Console()


@click.group()
def cli() -> None:
    """Agentic competitive intelligence tools."""


@cli.command()
def status() -> None:
    """Print db location, row counts per table, and api-key presence."""
    import os
    t = Table(title="intel status")
    t.add_column("key"); t.add_column("value")
    t.add_row("data_dir", str(DATA_DIR))
    t.add_row("db_path", str(DB_PATH))
    t.add_row("ANTHROPIC_API_KEY", "set" if os.environ.get("ANTHROPIC_API_KEY") else "[red]missing[/red]")
    t.add_row(
        "META_AD_LIBRARY_ACCESS_TOKEN",
        "set" if os.environ.get("META_AD_LIBRARY_ACCESS_TOKEN") else "[yellow]missing[/yellow]",
    )
    # Whisper API key — optional, gates voice-over transcription on video ads.
    has_whisper = bool(os.environ.get("OPENAI_API_KEY") or os.environ.get("INTEL_WHISPER_API_KEY"))
    t.add_row(
        "OPENAI_API_KEY / INTEL_WHISPER_API_KEY",
        "set (video voice-over transcription on)" if has_whisper
        else "[yellow]unset — video analysis runs visual-only[/yellow]",
    )
    # ffmpeg / ffprobe — required for video analysis pipeline.
    try:
        from .analysis.video import check_ffmpeg
        ok, detail = check_ffmpeg()
        t.add_row(
            "ffmpeg/ffprobe",
            f"[green]found[/green] · {detail}" if ok else f"[red]{detail}[/red]",
        )
    except Exception as e:
        t.add_row("ffmpeg/ffprobe", f"[red]check failed: {e}[/red]")
    if DB_PATH.exists():
        with connect() as conn:
            for tbl in ["competitors", "sources", "observations", "ads", "creatives", "offers", "briefings", "audit_log"]:
                n = conn.execute(f"SELECT COUNT(*) c FROM {tbl}").fetchone()["c"]
                t.add_row(f"db.{tbl}", str(n))
    else:
        t.add_row("db", "[yellow]not initialized — run `intel init`[/yellow]")
    console.print(t)


@cli.command()
def init() -> None:
    """Create the database and register competitors from config/competitors.yaml."""
    init_db()
    comps = load_competitors()
    with connect() as conn:
        for c in comps:
            upsert_competitor(conn, c)
    console.print(f"[green]ok[/green] registered {len(comps)} competitors")
    for c in comps:
        console.print(f"  - {c.id} ({c.name}) — {len(c.sources)} sources")


@cli.command()
@click.option("--competitor", "competitor_id", default=None, help="single competitor id; else all")
def ingest(competitor_id: str | None) -> None:
    """Run adapters → diff → enrich for one or all competitors."""
    reports = ingest_competitor(competitor_id) if competitor_id else ingest_all()
    t = Table(title="Ingestion report", show_lines=False)
    t.add_column("competitor"); t.add_column("source"); t.add_column("ok"); t.add_column("changed")
    t.add_column("sev"); t.add_column("new_ads"); t.add_column("offers"); t.add_column("summary")
    for r in reports:
        t.add_row(
            r.competitor_id,
            r.source_key[:60],
            "✓" if r.ok else "✗",
            "yes" if r.changed else "no",
            f"{r.severity:.2f}",
            str(len(r.new_ads)),
            str(r.offers_extracted),
            (r.change_summary or r.error or "")[:60],
        )
    console.print(t)


@cli.command()
@click.option("--days", default=7, type=int)
@click.option("--competitor", "competitor_id", default=None)
def ads(days: int, competitor_id: str | None) -> None:
    """List new ads detected in the last N days."""
    q = "SELECT competitor_id, ad_archive_id, page_name, first_seen, body_text FROM ads WHERE first_seen >= datetime('now', ?)"
    params: list = [f"-{days} days"]
    if competitor_id:
        q += " AND competitor_id=?"; params.append(competitor_id)
    q += " ORDER BY first_seen DESC LIMIT 50"
    with connect() as conn:
        rows = conn.execute(q, params).fetchall()
    t = Table(title=f"New ads — last {days}d ({len(rows)} rows)")
    t.add_column("competitor"); t.add_column("archive_id"); t.add_column("page"); t.add_column("first_seen"); t.add_column("body")
    for r in rows:
        t.add_row(r["competitor_id"], r["ad_archive_id"], r["page_name"] or "", r["first_seen"], (r["body_text"] or "")[:80])
    console.print(t)


@cli.command()
@click.option("--days", default=14, type=int)
@click.option("--competitor", "competitor_id", default=None)
def offers(days: int, competitor_id: str | None) -> None:
    """List offers detected in the last N days."""
    q = "SELECT competitor_id, observed_at, kind, value, threshold, description FROM offers WHERE observed_at >= datetime('now', ?)"
    params: list = [f"-{days} days"]
    if competitor_id:
        q += " AND competitor_id=?"; params.append(competitor_id)
    q += " ORDER BY observed_at DESC LIMIT 50"
    with connect() as conn:
        rows = conn.execute(q, params).fetchall()
    t = Table(title=f"Offers — last {days}d ({len(rows)} rows)")
    t.add_column("competitor"); t.add_column("when"); t.add_column("kind"); t.add_column("value"); t.add_column("threshold"); t.add_column("desc")
    for r in rows:
        t.add_row(r["competitor_id"], r["observed_at"], r["kind"] or "", r["value"] or "", r["threshold"] or "", (r["description"] or "")[:60])
    console.print(t)


@cli.command()
@click.option("--days", default=7, type=int)
@click.option("--scope", default=None, help="label, e.g. 'weekly'")
@click.option("--print-corpus", is_flag=True, help="print the structured corpus, not the briefing")
@click.option("--no-llm", is_flag=True, help="render a deterministic templated briefing (no Anthropic key needed)")
def brief(days: int, scope: str | None, print_corpus: bool, no_llm: bool) -> None:
    """Generate (and persist) a competitive briefing."""
    if print_corpus:
        from .synthesis.briefing import build_corpus
        corpus = build_corpus(days)
        console.print_json(json.dumps(corpus, default=str))
        return
    try:
        result = generate_briefing(days=days, scope=scope, use_llm=not no_llm)
    except MissingApiKey as e:
        console.print(f"[red]error:[/red] {e}")
        sys.exit(1)
    console.rule(f"[bold]{result['title']}[/bold] (id={result['briefing_id']})")
    console.print(Markdown(result["body_md"]))


@cli.command()
@click.option("--vertical", default=None)
def whitespace(vertical: str | None) -> None:
    """Surface angles/formats/offers the competitive set is NOT using."""
    res = detect_whitespace(vertical=vertical)
    if "error" in res:
        console.print(f"[red]error[/red]: {res['error']}")
        return
    t = Table(title="Whitespace candidates")
    t.add_column("angle"); t.add_column("rationale"); t.add_column("hypothesis"); t.add_column("effort"); t.add_column("conf")
    for w in res.get("whitespace", []):
        t.add_row(
            w.get("angle", ""), w.get("rationale", "")[:80],
            w.get("testable_hypothesis", "")[:80], w.get("effort", ""),
            f"{w.get('confidence', 0):.2f}",
        )
    console.print(t)


@cli.command("analyze-creative")
@click.argument("image_path")
def analyze_creative_cmd(image_path: str) -> None:
    """Vision-classify a single creative image."""
    res = analyze_creative_image(image_path)
    console.print_json(json.dumps(res, default=str, indent=2))


@cli.command("analyze-creatives")
@click.option("--competitor", "competitor_id", default=None, help="single competitor; else all brands")
@click.option("--limit", default=50, type=int, help="max NEW model calls (skips already-analyzed)")
@click.option("--no-dedupe-phash", is_flag=True, help="don't reuse analysis for identical images")
@click.option("--force-reanalyze", is_flag=True,
              help="re-run analysis on already-analyzed creatives (e.g. after a taxonomy expansion). "
                   "Skips phash dedup since the old phash-matched analysis was produced under the prior schema.")
def analyze_creatives_cmd(
    competitor_id: str | None, limit: int, no_dedupe_phash: bool, force_reanalyze: bool,
) -> None:
    """Batch-analyze unanalyzed creative images (vision + taxonomy).

    Idempotent — re-running only analyzes net-new creatives. Reuses prior
    analysis for visually identical images (perceptual-hash match) unless
    --no-dedupe-phash is set. Use --force-reanalyze to re-run on already-
    analyzed creatives (intended for taxonomy upgrades).
    """
    report = analyze_pending(
        competitor_id=competitor_id,
        limit=limit,
        dedupe_by_phash=not no_dedupe_phash,
        force_reanalyze=force_reanalyze,
    )
    console.print(str(report))
    if report.errors:
        console.print("\n[red]errors:[/red]")
        for e in report.errors[:5]:
            console.print(f"  - {e}")
        if len(report.errors) > 5:
            console.print(f"  …+{len(report.errors)-5} more")


@cli.command("prune-videos")
@click.option("--competitor", "competitor_id", default=None, help="single competitor; else all brands")
@click.option("--keep-n", default=12, type=int, help="number of top-popularity videos to keep mp4s for, per brand")
def prune_videos_cmd(competitor_id: str | None, keep_n: int) -> None:
    """Delete mp4 files for video ads outside the top-N most-popular per brand.

    Frames + transcripts + analysis_json are KEPT — only the bulky mp4 is evicted.
    The dashboard renders evicted videos with first-frame + "Watch on Meta Ad
    Library" click-out instead of inline <video> playback.

    Run automatically at the end of every `intel ingest` for meta_ads sources;
    expose this command for manual reruns (e.g., after lowering keep-N).
    """
    from .storage import prune_videos, connect
    with connect() as conn:
        if competitor_id:
            comps = [competitor_id]
        else:
            comps = [r["id"] for r in conn.execute("SELECT id FROM competitors").fetchall()]
        total = 0
        for cid in comps:
            evicted = prune_videos(conn, cid, keep_n=keep_n)
            total += evicted
            if evicted:
                console.print(f"  {cid}: evicted {evicted} mp4(s)")
        console.print(f"[green]total evicted: {total}[/green]")


@cli.command("reanalyze-videos")
@click.option("--competitor", "competitor_id", default=None, help="single competitor; else all brands")
@click.option("--limit", default=50, type=int, help="cap on model calls")
def reanalyze_videos_cmd(competitor_id: str | None, limit: int) -> None:
    """Force the video analyzer to re-run on every video creative (regardless of
    whether it was already analyzed). Useful when the video schema/prompt
    changes — the existing analyses are stale but the frames + transcript are
    fine to reuse.

    Image creatives are NOT touched (use --force-reanalyze on `analyze-creatives`
    for that case).
    """
    from .storage import connect
    from .analysis.creative_batch import analyze_pending
    with connect() as conn:
        # Reset analyzed_at for video creatives so they show up in _pending again.
        q = (
            "UPDATE creatives SET analyzed_at=NULL WHERE asset_type IN ('video','video_evicted')"
        )
        params: tuple = ()
        if competitor_id:
            q += (
                " AND id IN (SELECT cr.id FROM creatives cr "
                "JOIN ads a ON a.id=cr.ad_id WHERE a.competitor_id = ?)"
            )
            params = (competitor_id,)
        cur = conn.execute(q, params)
        console.print(f"reset analyzed_at on {cur.rowcount} video creative(s)")
    report = analyze_pending(
        competitor_id=competitor_id,
        limit=limit,
        dedupe_by_phash=False,  # bypass dedup so every video gets fresh analysis
        force_reanalyze=False,
    )
    console.print(str(report))


@cli.command("creative-readout")
@click.option("--competitor", "competitor_id", required=True, help="brand id from competitors.yaml")
@click.option("--days", default=7, type=int, help="window for 'net new' (ads first_seen)")
@click.option("--save", default=None, type=click.Path(), help="also write markdown to a file")
def creative_readout_cmd(competitor_id: str, days: int, save: str | None) -> None:
    """Per-brand creative readout — net new + attribute distribution."""
    res = per_brand_readout(competitor_id, window_days=days)
    console.print(Markdown(res["body_md"]))
    if save:
        from pathlib import Path
        Path(save).parent.mkdir(parents=True, exist_ok=True)
        Path(save).write_text(res["body_md"], encoding="utf-8")
        console.print(f"\n[green]wrote[/green] {save}")


@cli.command("creative-comparison")
@click.option("--days", default=30, type=int, help="window for ads first_seen")
@click.option("--save", default=None, type=click.Path(), help="also write markdown to a file")
def creative_comparison_cmd(days: int, save: str | None) -> None:
    """Cross-competitor creative comparison — popularity, distinctiveness, whitespace."""
    res = cross_set_comparison(window_days=days)
    console.print(Markdown(res["body_md"]))
    if save:
        from pathlib import Path
        Path(save).parent.mkdir(parents=True, exist_ok=True)
        Path(save).write_text(res["body_md"], encoding="utf-8")
        console.print(f"\n[green]wrote[/green] {save}")


@cli.command("landing-pages")
@click.option("--competitor", "competitor_id", default=None,
              help="single brand id; default: all brands with link_urls")
@click.option("--top-n", default=3, type=int,
              help="how many top destinations per section to show (default 3)")
def landing_pages_cmd(competitor_id: str | None, top_n: int) -> None:
    """Where each brand's ads are sending traffic — per-section breakdown.

    Snapshot view: each ad's link_url is bucketed by what it *is* (product
    browse, samples, quote/lead, where-to-buy, off-brand-tracker, etc.).
    Useful as a one-shot inspection without rendering the full dashboard.
    """
    from .analysis.landing import (
        SECTION_LABELS,
        aggregate_landing_pages,
        brand_host_for,
        classify_url,
        detect_brand_host,
        parse_url,
    )
    from .config import get_competitor
    from .storage import popularity_score

    with connect() as conn:
        if competitor_id:
            brand_rows = conn.execute(
                "SELECT id, name FROM competitors WHERE id=?", (competitor_id,),
            ).fetchall()
            if not brand_rows:
                console.print(f"[red]unknown competitor id:[/red] {competitor_id}")
                sys.exit(2)
        else:
            brand_rows = conn.execute(
                "SELECT id, name FROM competitors ORDER BY priority DESC, name"
            ).fetchall()

        any_rendered = False
        for br in brand_rows:
            cid = br["id"]
            comp = get_competitor(cid)
            brand_host = brand_host_for(comp) if comp else ""
            max_rank_row = conn.execute(
                "SELECT MAX(serp_position_rank) FROM ads WHERE competitor_id=? "
                "AND serp_position_rank IS NOT NULL", (cid,),
            ).fetchone()
            max_rank = (max_rank_row[0] or 0) if max_rank_row else 0
            ad_rows = conn.execute(
                "SELECT id, ad_archive_id, link_url, serp_position_rank, "
                "start_date, last_seen, active FROM ads "
                "WHERE competitor_id=? AND link_url IS NOT NULL", (cid,),
            ).fetchall()
            if not ad_rows:
                if competitor_id:
                    console.print(f"[yellow]no link_urls for[/yellow] {cid}")
                continue
            if not brand_host:
                brand_host = detect_brand_host([r["link_url"] for r in ad_rows])
            enriched = []
            for r in ad_rows:
                parsed = parse_url(r["link_url"])
                bucket = classify_url(parsed, brand_host)
                score = popularity_score(
                    r["serp_position_rank"], r["start_date"], r["last_seen"],
                    r["active"] or 0, max_rank,
                )
                enriched.append({
                    "id": r["id"], "ad_archive_id": r["ad_archive_id"],
                    "link_url": r["link_url"], "section": bucket,
                    "popularity_score": score, **parsed,
                })
            agg = aggregate_landing_pages(enriched, brand_host)
            if not agg.get("by_section"):
                continue
            any_rendered = True

            header = (
                f"\n[bold]{br['name']}[/bold]  "
                f"[dim]{agg['with_link']} of {agg['total_ads']} ads · "
                f"{agg['on_brand_share']:.0%} on-brand · host={brand_host or '?'}[/dim]"
            )
            console.print(header)

            tbl = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
            tbl.add_column("Section", style="cyan", no_wrap=True)
            tbl.add_column("Ads", justify="right", style="white")
            tbl.add_column("Share", justify="right", style="white")
            tbl.add_column("Pop. share", justify="right", style="dim")
            tbl.add_column("Top destination(s)", overflow="fold")
            for s in agg["by_section"]:
                sec = s["section"]
                label = SECTION_LABELS.get(sec, sec)
                top_dests = []
                for tu in (s.get("top_urls") or [])[:top_n]:
                    cu = tu.get("clean_url") or ""
                    if cu:
                        top_dests.append(f"{cu} [dim]({tu['ad_count']})[/dim]")
                if not top_dests and s.get("example_raw_urls"):
                    top_dests = [
                        f"[dim italic]{u[:90]}[/dim italic]"
                        for u in s["example_raw_urls"][:top_n]
                    ]
                tbl.add_row(
                    label,
                    str(s["ad_count"]),
                    f"{s['ad_share']:.0%}",
                    f"{s['popularity_share']:.0%}",
                    "\n".join(top_dests) or "—",
                )
            console.print(tbl)

        if not any_rendered:
            console.print("[yellow]no brands with link_urls found.[/yellow]")


# On-brand landing sections worth screenshotting. Excludes trackers, short-links,
# unfilled Dynamic-Creative templates, and unknown/off-brand destinations — those
# either redirect, don't resolve, or aren't the brand's own pages.
_SAFE_LANDING_SECTIONS = {
    "homepage", "product_browse", "product_detail", "samples",
    "quote_lead", "where_to_buy", "inspiration_content", "brand_story",
}


@cli.command("capture-landing-pages")
@click.option("--competitor", "competitor_id", default=None,
              help="single brand id; default: all brands with link_urls")
@click.option("--limit-per-brand", default=8, type=int,
              help="max distinct landing pages to screenshot per brand (default 8)")
@click.option("--days", default=90, type=int,
              help="only consider ads seen within this many days (default 90)")
@click.option("--delay", default=2.0, type=float,
              help="seconds to wait between page captures (default 2.0)")
@click.option("--headed", is_flag=True, help="run Playwright headed (debug anti-bot)")
def capture_landing_pages_cmd(competitor_id: str | None, limit_per_brand: int,
                              days: int, delay: float, headed: bool) -> None:
    """Screenshot the on-brand landing pages that ads send traffic to.

    Enumerates each brand's real on-brand destination URLs (from ad link_urls,
    classified by the landing-section taxonomy), captures one whole-page
    screenshot per URL, and stores it as a `landing_page_image` creative with a
    sidecar recording its URL + section. Run `intel analyze-creatives` afterward
    to attach the dedicated landing-page analysis. Idempotent: stable per-URL
    paths mean re-runs refresh in place rather than piling up rows.
    """
    import time
    from datetime import datetime as _dt, timedelta, timezone as _tz

    from .adapters.website import _domain_slug, scrape_landing_page
    from .analysis.creative import perceptual_hash
    from .analysis.landing import (
        aggregate_landing_pages,
        brand_host_for,
        classify_url,
        detect_brand_host,
        parse_url,
    )
    from .config import get_competitor
    from .storage import asset_path, popularity_score, upsert_creative

    since = (_dt.now(_tz.utc) - timedelta(days=days)).isoformat()

    with connect() as conn:
        if competitor_id:
            brand_rows = conn.execute(
                "SELECT id, name FROM competitors WHERE id=?", (competitor_id,),
            ).fetchall()
            if not brand_rows:
                console.print(f"[red]unknown competitor id:[/red] {competitor_id}")
                sys.exit(2)
        else:
            brand_rows = conn.execute(
                "SELECT id, name FROM competitors ORDER BY priority DESC, name"
            ).fetchall()

        summary = Table(show_header=True, header_style="bold", box=None)
        summary.add_column("Brand", style="cyan")
        summary.add_column("Selected", justify="right")
        summary.add_column("Captured", justify="right", style="green")
        summary.add_column("Blocked/err", justify="right", style="yellow")

        for br in brand_rows:
            cid = br["id"]
            comp = get_competitor(cid)
            brand_host = brand_host_for(comp) if comp else ""
            max_rank_row = conn.execute(
                "SELECT MAX(serp_position_rank) FROM ads WHERE competitor_id=? "
                "AND serp_position_rank IS NOT NULL", (cid,),
            ).fetchone()
            max_rank = (max_rank_row[0] or 0) if max_rank_row else 0
            ad_rows = conn.execute(
                "SELECT id, ad_archive_id, link_url, serp_position_rank, "
                "start_date, last_seen, active FROM ads "
                "WHERE competitor_id=? AND link_url IS NOT NULL AND last_seen >= ?",
                (cid, since),
            ).fetchall()
            if not ad_rows:
                continue
            if not brand_host:
                brand_host = detect_brand_host([r["link_url"] for r in ad_rows])
            enriched = []
            for r in ad_rows:
                parsed = parse_url(r["link_url"])
                bucket = classify_url(parsed, brand_host)
                score = popularity_score(
                    r["serp_position_rank"], r["start_date"], r["last_seen"],
                    r["active"] or 0, max_rank,
                )
                enriched.append({
                    "id": r["id"], "ad_archive_id": r["ad_archive_id"],
                    "link_url": r["link_url"], "section": bucket,
                    "popularity_score": score, **parsed,
                })
            agg = aggregate_landing_pages(enriched, brand_host)

            # Flatten the top destination URLs across safe on-brand sections,
            # dedup by clean_url (keep the higher ad_count), rank, and cap.
            by_url: dict[str, dict] = {}
            for s in agg.get("by_section", []):
                if s["section"] not in _SAFE_LANDING_SECTIONS:
                    continue
                for tu in s.get("top_urls") or []:
                    cu = tu.get("clean_url")
                    if not cu:
                        continue
                    prev = by_url.get(cu)
                    if prev is None or tu["ad_count"] > prev["ad_count"]:
                        by_url[cu] = {
                            "clean_url": cu,
                            "section": s["section"],
                            "ad_count": tu["ad_count"],
                            "ad_archive_ids": tu.get("ad_archive_ids") or [],
                        }
            selected = sorted(by_url.values(), key=lambda d: d["ad_count"], reverse=True)[:limit_per_brand]
            if not selected:
                continue

            captured = 0
            blocked = 0
            for item in selected:
                cu = item["clean_url"]
                out_dir = asset_path("creative", cid, "landing", _domain_slug(cu))
                try:
                    sp = scrape_landing_page(cu, out_dir=out_dir, headless=not headed)
                except FileNotFoundError as e:
                    console.print(f"[red]{e}[/red]")
                    sys.exit(2)
                except Exception as e:
                    console.print(f"  [yellow]capture crashed[/yellow] {cu}: {e}")
                    blocked += 1
                    continue
                if sp.error or not sp.screenshot_path:
                    console.print(f"  [yellow]skip[/yellow] {cu} ({sp.error or 'no screenshot'})")
                    blocked += 1
                    time.sleep(delay)
                    continue
                # Sidecar: links the screenshot back to its URL + section so the
                # analyzer and dashboard can join on clean_url (no DB url column).
                sidecar = Path(sp.screenshot_path).parent / "landing_meta.json"
                sidecar.write_text(json.dumps({
                    "clean_url": cu,
                    "section": item["section"],
                    "ad_count": item["ad_count"],
                    "ad_archive_ids": item["ad_archive_ids"][:3],
                    "captured_at": _dt.now(_tz.utc).isoformat(timespec="seconds"),
                }), encoding="utf-8")
                try:
                    phash = perceptual_hash(Path(sp.screenshot_path))
                except Exception:
                    phash = None
                upsert_creative(
                    conn,
                    ad_id=None,
                    asset_type="landing_page_image",
                    asset_path=sp.screenshot_path,
                    phash=phash,
                    competitor_id=cid,
                )
                captured += 1
                console.print(f"  [green]captured[/green] [{item['section']}] {cu}")
                time.sleep(delay)

            summary.add_row(br["name"], str(len(selected)), str(captured), str(blocked))

        console.print(summary)
        console.print("[dim]next:[/dim] intel analyze-creatives  [dim](analyzes the new landing screenshots)[/dim]")


@cli.command("cluster-hooks")
@click.option("--competitor", "competitor_id", default=None)
@click.option("--limit", default=50, type=int)
def cluster_hooks_cmd(competitor_id: str | None, limit: int) -> None:
    """Cluster recent ads' body copy into hook themes."""
    q = "SELECT ad_archive_id, body_text FROM ads WHERE active=1"
    params: list = []
    vertical = ""
    if competitor_id:
        q += " AND competitor_id=?"; params.append(competitor_id)
        with connect() as conn:
            row = conn.execute("SELECT vertical FROM competitors WHERE id=?", (competitor_id,)).fetchone()
            vertical = (row["vertical"] if row else "") or ""
    q += " ORDER BY first_seen DESC LIMIT ?"; params.append(int(limit))
    with connect() as conn:
        rows = conn.execute(q, params).fetchall()
    ads = [{"ad_archive_id": r["ad_archive_id"], "body_text": r["body_text"]} for r in rows]
    res = cluster_hooks(ads, vertical=vertical)
    console.print_json(json.dumps(res, default=str, indent=2))


@cli.command("extract-offers")
@click.argument("text", required=False)
def extract_offers_cmd(text: str | None) -> None:
    """Run offer extraction on stdin or arg. e.g. echo 'FREE shipping' | intel extract-offers"""
    if not text:
        text = sys.stdin.read()
    if not text.strip():
        console.print("[red]no text provided[/red]")
        return
    res = extract_offers_from_text(text)
    console.print_json(json.dumps(res, default=str, indent=2))


@cli.command()
@click.argument("goal", nargs=-1, required=True)
@click.option("--max-iters", default=15, type=int)
@click.option("--verbose", is_flag=True, help="stream tool calls to stderr")
def agent(goal: tuple[str, ...], max_iters: int, verbose: bool) -> None:
    """Run the orchestrating agent. The model picks which tools to call.

    Example:
      intel agent "give me this week's competitive briefing"
      intel agent "did any competitor launch a sale in the last 3 days?"
    """
    g = " ".join(goal)
    log = (lambda s: click.echo(s, err=True)) if verbose else None
    result = run_agent(g, max_iters=max_iters, log=log)
    console.rule("agent answer")
    if result["final_text"]:
        # Render as markdown if it looks like markdown.
        if any(tok in result["final_text"] for tok in ("# ", "## ", "- ", "**")):
            console.print(Markdown(result["final_text"]))
        else:
            console.print(result["final_text"])
    else:
        console.print("[yellow](no final text — stop_reason="
                      f"{result['stop_reason']})[/yellow]")
    console.print(f"\n[dim]tool calls: {len(result['tool_calls'])} | "
                  f"stop: {result['stop_reason']}[/dim]")


@cli.command()
@click.option("--limit", default=10, type=int)
def briefings(limit: int) -> None:
    """List recent briefings stored in the db."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, created_at, scope, title FROM briefings ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    t = Table(title="Briefings")
    t.add_column("id"); t.add_column("created_at"); t.add_column("scope"); t.add_column("title")
    for r in rows:
        t.add_row(str(r["id"]), r["created_at"], r["scope"], r["title"])
    console.print(t)


@cli.command()
@click.option("--out", default=None, type=click.Path(),
              help="output dir; default reports/<UTC-date>/dashboard (or dashboard-v2 with --v2)")
@click.option("--days", default=30, type=int)
@click.option("--org-name", default="Horizon Commerce", help="organization label in header eyebrow")
@click.option("--product-name", default=None,
              help='product/page title; default "Creative & Competitive Intelligence" '
                   '(v1) or "Intelligence" (v2 — brands as "Horizon Commerce Intelligence")')
@click.option("--v2", "use_v2", is_flag=True,
              help="render the redesigned dark-mode dashboard (with light-mode toggle); v1 stays the default")
@click.option("--only", "only", multiple=True, metavar="BRAND_ID",
              help="restrict the dashboard to these competitor ids (repeatable). "
                   "Mutually combinable with --exclude.")
@click.option("--exclude", "exclude", multiple=True, metavar="BRAND_ID",
              help="drop these competitor ids from the dashboard (repeatable), "
                   "e.g. --exclude revlon to keep a control brand out of the main set.")
@click.option("--open-after", is_flag=True, help="open the dashboard in your default browser after build")
def dashboard(out: str | None, days: int, org_name: str, product_name: str | None,
              use_v2: bool, only: tuple[str, ...], exclude: tuple[str, ...],
              open_after: bool) -> None:
    """Render a static HTML dashboard from the current DB + creatives."""
    from datetime import datetime as _dt
    if out is None:
        out = f"reports/{_dt.utcnow().date().isoformat()}/{'dashboard-v2' if use_v2 else 'dashboard'}"
    if product_name is None:
        product_name = DEFAULT_PRODUCT_V2 if use_v2 else DEFAULT_PRODUCT

    # Resolve --only / --exclude into a concrete allow-list of competitor ids.
    # None = all brands (default). --only sets the base set; --exclude removes
    # from it (or from all brands when --only is absent).
    brand_ids: set[str] | None = None
    if only or exclude:
        with connect() as conn:
            all_ids = {r["id"] for r in conn.execute("SELECT id FROM competitors")}
        unknown = (set(only) | set(exclude)) - all_ids
        if unknown:
            console.print(f"[yellow]warning:[/yellow] unknown competitor id(s): "
                          f"{', '.join(sorted(unknown))}. Known: {', '.join(sorted(all_ids))}")
        brand_ids = (set(only) & all_ids) if only else set(all_ids)
        brand_ids -= set(exclude)
        if not brand_ids:
            console.print("[red]no brands left after --only/--exclude — nothing to render[/red]")
            raise SystemExit(1)

    builder = build_dashboard_v2 if use_v2 else build_dashboard
    result = builder(out, days=days, org_name=org_name, product_name=product_name,
                     brand_ids=brand_ids)
    console.print(f"[green]wrote[/green] {result['path']}  "
                  f"({result['n_brands']} brands · {result['n_analyzed']} analyzed creatives · "
                  f"{result['size_bytes']//1024} KB)")
    if open_after:
        import webbrowser
        webbrowser.open(f"file://{Path(result['path']).resolve()}")


@cli.command()
@click.argument("briefing_id", type=int)
def show_briefing(briefing_id: int) -> None:
    """Render a stored briefing."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM briefings WHERE id=?", (briefing_id,)).fetchone()
    if not row:
        console.print(f"[red]no briefing {briefing_id}[/red]")
        return
    console.rule(f"[bold]{row['title']}[/bold]")
    console.print(Markdown(row["body_md"]))


@cli.command("test-amazon-store")
@click.argument("store_url")
@click.option("--out-dir", default=None, type=click.Path(),
              help="output dir for screenshots/HTML/images; default reports/<date>/amazon-test/")
@click.option("--max-subpages", default=0, type=int,
              help="0 = landing only (Phase B1 default); higher = follow sub-pages (Phase B2)")
@click.option("--headed", is_flag=True, help="run Playwright in headed mode (helpful for debugging anti-bot)")
def test_amazon_store_cmd(store_url: str, out_dir: str | None, max_subpages: int, headed: bool) -> None:
    """Smoke-test the Amazon Brand Store adapter against a single URL.

    Doesn't touch the db — just runs the scrape and reports what was captured.
    Useful for verifying a store_url before adding it to competitors.yaml, or
    diagnosing 503/anti-bot issues.

    Example:
      intel test-amazon-store https://www.amazon.com/stores/page/<UUID>
    """
    from datetime import datetime as _dt
    from .adapters.amazon_brand_store import scrape_brand_store
    if out_dir is None:
        out_dir = f"reports/{_dt.utcnow().date().isoformat()}/amazon-test"
    out = Path(out_dir).resolve()
    asset_root = out / "creative"
    raw_root = out / "raw"
    console.print(f"[bold]scraping[/bold] {store_url}")
    console.print(f"  asset_root: {asset_root}")
    console.print(f"  raw_root:   {raw_root}")
    pages = scrape_brand_store(
        store_url,
        asset_dir=asset_root, raw_dir=raw_root,
        max_subpages=max_subpages, headless=not headed,
    )
    if not pages:
        console.print("[red]no pages returned[/red]")
        return
    t = Table(title=f"{len(pages)} page(s) captured")
    t.add_column("page_key"); t.add_column("title"); t.add_column("images"); t.add_column("sublinks"); t.add_column("error")
    for p in pages:
        t.add_row(
            p.page_key[:14],
            (p.title or "")[:50],
            str(len(p.image_local_paths)),
            str(len(p.subpage_links)),
            (p.error or "")[:60],
        )
    console.print(t)
    if pages[0].screenshot_path:
        console.print(f"\n[green]open landing screenshot:[/green] {pages[0].screenshot_path}")


@cli.command("test-homepage")
@click.argument("url")
@click.option("--out-dir", default=None, type=click.Path(),
              help="output dir for screenshot/HTML/images; default reports/<date>/homepage-test/")
@click.option("--max-images", default=30, type=int, help="cap captured images per page")
@click.option("--headed", is_flag=True, help="run Playwright in headed mode (helpful for debugging)")
def test_homepage_cmd(url: str, out_dir: str | None, max_images: int, headed: bool) -> None:
    """Smoke-test the website adapter against a single URL.

    Doesn't touch the db — runs Playwright + chrome filter + image capture and
    reports what was captured + dropped. Useful for verifying a new brand
    homepage before wiring it through ingest.

    Example:
      intel test-homepage https://www.mybobs.com
    """
    from datetime import datetime as _dt
    from .adapters.website import scrape_homepage
    if out_dir is None:
        out_dir = f"reports/{_dt.utcnow().date().isoformat()}/homepage-test"
    out = Path(out_dir).resolve()
    asset_root = out / "creative"
    raw_root = out / "raw"
    console.print(f"[bold]scraping[/bold] {url}")
    console.print(f"  asset_root: {asset_root}")
    console.print(f"  raw_root:   {raw_root}")
    sp = scrape_homepage(
        url,
        asset_dir=asset_root, raw_dir=raw_root,
        max_images=max_images, headless=not headed,
    )
    if sp.error:
        console.print(f"[red]error:[/red] {sp.error}")
        return
    parsed = getattr(sp, "parsed", {}) or {}
    t = Table(title=f"capture: {sp.page_key}")
    t.add_column("field"); t.add_column("value")
    t.add_row("title",          (sp.title or "")[:80])
    t.add_row("hero_text",      (sp.hero_text or "")[:80])
    t.add_row("images saved",   str(len(sp.image_local_paths)))
    t.add_row("chrome dropped", str(parsed.get("chrome_dropped", 0)))
    t.add_row("regions/hero",   str(len((parsed.get("regions") or {}).get("hero") or [])))
    t.add_row("regions/banner", str(len((parsed.get("regions") or {}).get("banner_or_promo") or [])))
    t.add_row("screenshot",     sp.screenshot_path or "")
    t.add_row("hero crop",      parsed.get("hero_image_path") or "")
    console.print(t)
    if sp.image_local_paths:
        console.print("\n[bold]first 5 saved images:[/bold]")
        for p in sp.image_local_paths[:5]:
            console.print(f"  {p}")


# ---- evals ----------------------------------------------------------------

@cli.group()
def evals() -> None:
    """Hill-climbing eval harness (bible §6). Tasks live in src/intel/evals/tasks/."""


@evals.command("build-seed")
def evals_build_seed() -> None:
    """Snapshot the live db into src/intel/evals/fixtures/seed.db."""
    from .evals.seed import build_seed
    dest, summary = build_seed()
    console.print(f"[green]wrote[/green] {dest}  ({summary['size_bytes']//1024} KB)")
    for k, v in summary["counts"].items():
        console.print(f"  {k}: {v}")


@evals.command("list")
def evals_list() -> None:
    """List discovered eval tasks."""
    from .evals import discover_tasks
    tasks = discover_tasks()
    t = Table(title=f"{len(tasks)} tasks")
    t.add_column("id"); t.add_column("category"); t.add_column("title"); t.add_column("needs key")
    for tk in tasks:
        t.add_row(tk.id, tk.category, tk.title, "yes" if tk.requires_anthropic_key else "no")
    console.print(t)


@evals.command("run")
@click.option("--task", "task_filter", default=None,
              help="comma-separated task ids or categories; default = all")
@click.option("--out", default=None, type=click.Path(),
              help="output dir for HTML dashboard; default reports/<date>/evals")
@click.option("--no-dashboard", is_flag=True, help="skip the HTML dashboard step")
@click.option("--open-after", is_flag=True, help="open the dashboard in your browser")
@click.option("--notes", default=None, help="freeform notes to attach to this run")
def evals_run(task_filter: str | None, out: str | None, no_dashboard: bool,
              open_after: bool, notes: str | None) -> None:
    """Run the eval suite, score, persist, render the HTML dashboard."""
    from datetime import datetime as _dt
    from .evals import discover_tasks
    from .evals.runner import run_suite, filter_tasks, SEED_DB
    if not SEED_DB.exists():
        console.print("[yellow]seed.db not found — building from live db…[/yellow]")
        from .evals.seed import build_seed
        build_seed()
    tasks = filter_tasks(discover_tasks(), task_filter)
    if not tasks:
        console.print("[red]no tasks matched[/red]")
        sys.exit(1)
    console.rule(f"[bold]intel evals — {len(tasks)} task(s)[/bold]")
    run_id, results = run_suite(tasks, notes=notes)
    n_pass = sum(1 for r in results if r.status == "PASS")
    n_slow = sum(1 for r in results if r.status == "PASS_SLOW")
    n_fail = sum(1 for r in results if r.status in ("FAIL", "ERROR"))
    n_skip = sum(1 for r in results if r.status == "SKIP")
    scoreable = n_pass + n_slow + n_fail
    headline = (n_pass + 0.5 * n_slow) / scoreable if scoreable else 0.0
    console.print(
        f"\n[bold]run #{run_id}[/bold]: [green]{n_pass} pass[/green] · "
        f"[yellow]{n_slow} slow[/yellow] · [red]{n_fail} fail[/red] · "
        f"[dim]{n_skip} skip[/dim]  →  headline = [bold]{headline*100:.0f}%[/bold]"
    )
    if not no_dashboard:
        from .evals.dashboard import build_eval_dashboard
        if out is None:
            out = f"reports/{_dt.utcnow().date().isoformat()}/evals"
        res = build_eval_dashboard(out, run_id=run_id)
        console.print(f"[green]wrote[/green] {res['path']}  ({res['size_bytes']//1024} KB)")
        if open_after:
            import webbrowser
            webbrowser.open(f"file://{Path(res['path']).resolve()}")


@evals.command("dashboard")
@click.option("--out", default=None, type=click.Path())
@click.option("--run-id", default=None, type=int, help="render a specific run; default = latest")
@click.option("--open-after", is_flag=True)
def evals_dashboard(out: str | None, run_id: int | None, open_after: bool) -> None:
    """Rebuild the HTML dashboard without re-running tasks."""
    from datetime import datetime as _dt
    from .evals.dashboard import build_eval_dashboard
    if out is None:
        out = f"reports/{_dt.utcnow().date().isoformat()}/evals"
    res = build_eval_dashboard(out, run_id=run_id)
    console.print(f"[green]wrote[/green] {res['path']}  ({res['size_bytes']//1024} KB)")
    if open_after:
        import webbrowser
        webbrowser.open(f"file://{Path(res['path']).resolve()}")


@evals.command("history")
@click.option("--limit", default=20, type=int)
def evals_history(limit: int) -> None:
    """List recent eval runs."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, started_at, headline_score, pass_count, pass_slow_count, "
            "fail_count, skip_count, git_sha, notes FROM eval_runs "
            "ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    t = Table(title=f"eval runs (most-recent {limit})")
    t.add_column("id"); t.add_column("started"); t.add_column("score")
    t.add_column("pass/slow/fail/skip"); t.add_column("git"); t.add_column("notes")
    for r in rows:
        score = f"{(r['headline_score'] or 0)*100:.0f}%"
        psfs = f"{r['pass_count']}/{r['pass_slow_count']}/{r['fail_count']}/{r['skip_count']}"
        t.add_row(str(r["id"]), r["started_at"][:16].replace("T", " "), score,
                  psfs, r["git_sha"] or "", (r["notes"] or "")[:40])
    console.print(t)


if __name__ == "__main__":
    cli()
