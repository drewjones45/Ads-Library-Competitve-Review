#!/usr/bin/env python
"""Render strategist briefs into editorial HTML — consulting-deck aesthetic.

Design system: dark backdrop + chartreuse accent, magazine-grade hero typography,
eyebrow-labeled sections, inline brand/data highlighting, SVG charts + curated
reference board with base64 thumbnails.

Per the adology-brand-marketing-mode skill: captions are hand-written strategic
interpretations, not data summaries.

Usage:
    python scripts/render_strategy_html.py <deployment-key> \\
        --md <input.md> --out <output.html> --brand "..." --date YYYY-MM-DD
"""
from __future__ import annotations

import argparse
import base64
import html as _html
import io
import json
import math
import mimetypes
import re
import sqlite3
import sys

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
from collections import Counter
from pathlib import Path

import markdown

sys.path.insert(0, str(Path(__file__).parent))
from strategy_visual_data import DEPLOYMENTS

# Reach into the intel package for the shared popularity scorer.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from intel.storage import popularity_score  # noqa: E402

# Shared accent — Adology design system, consistent across deployments
ACCENT = "#d4f057"   # chartreuse
ACCENT_DIM = "#9ab33d"
BG = "#0a0a0a"
BG_CARD = "#161616"
BG_CARD_BORDER = "#252525"
INK = "#f5f5f5"
INK_DIM = "#b8b8b8"
INK_MUTED = "#7a7a7a"
INK_FAINT = "#4a4a4a"

# Palette for stacked charts (dark mode)
DNA_PALETTE = {
    "lifestyle":            "#4ea8b8",
    "studio_product_only":  "#d4f057",  # accent — highlights the rational/spec mode
    "mixed":                "#d4a14a",
    "text_only":            "#7a7a7a",
    "flat_lay":             "#9d8189",
    "screenshot_ui":        "#5a6b80",
    "model_on_figure":      "#5f7a61",
}
APPEAL_PALETTE = {
    "emotional": "#e85b3a",
    "rational":  "#4ea8b8",
    "mixed":     "#9d8189",
}


# =============== inline SVG charts ===============

def chart_brand_volume(conn: sqlite3.Connection, d: dict) -> str:
    brand_order = d["brand_order"]; labels = d["brand_labels"]; subject = d["subject_brand"]
    rows = conn.execute("SELECT competitor_id, COUNT(*) FROM ads GROUP BY competitor_id").fetchall()
    counts = {r[0]: r[1] for r in rows}
    data = [(cid, counts.get(cid, 0)) for cid in brand_order if cid in labels]
    if not data: return ""
    max_n = max(d[1] for d in data) or 1

    row_h = 38; label_w = 180; chart_w = 620
    bar_w_max = chart_w - label_w - 80
    H = row_h * len(data) + 50

    parts = []
    for i, (cid, n) in enumerate(data):
        y = 24 + i * row_h
        bw = (n / max_n) * bar_w_max
        fill = ACCENT if cid == subject else "#383838"
        label_color = ACCENT if cid == subject else INK_DIM
        weight = "700" if cid == subject else "500"
        parts.append(f'<text x="{label_w-12}" y="{y+22}" text-anchor="end" font-size="13" '
                     f'font-family="Inter,sans-serif" font-weight="{weight}" fill="{label_color}">{labels[cid]}</text>')
        parts.append(f'<rect x="{label_w}" y="{y+8}" width="{bw:.1f}" height="22" rx="1" fill="{fill}"/>')
        parts.append(f'<text x="{label_w + bw + 10:.1f}" y="{y+25}" font-size="13" '
                     f'font-family="Inter,sans-serif" font-weight="700" fill="{INK}">{n}</text>')

    return f'''<figure class="chart">
      <figcaption class="chart-caption">Total ads observed per brand · Meta Ad Library · 7-day window</figcaption>
      <svg viewBox="0 0 {chart_w} {H}" xmlns="http://www.w3.org/2000/svg">{"".join(parts)}</svg>
    </figure>'''


def _attr_distribution(conn, attr, brands):
    out = {b: Counter() for b in brands}
    rows = conn.execute(
        f"SELECT COALESCE(c.competitor_id, a.competitor_id) AS cid, "
        f"json_extract(c.analysis_json, '$.{attr}') AS val "
        f"FROM creatives c LEFT JOIN ads a ON a.id = c.ad_id "
        f"WHERE c.analysis_json IS NOT NULL"
    ).fetchall()
    for cid, val in rows:
        if cid in out and val:
            out[cid][val] += 1
    return out


def chart_creative_dna(conn, d):
    labels = d["brand_labels"]; subject = d["subject_brand"]
    brand_order = [b for b in d["brand_order"] if b in labels]
    dist = _attr_distribution(conn, "photography_style", brand_order)
    brand_order = [b for b in brand_order if sum(dist[b].values()) >= 3]
    if not brand_order: return ""

    seen = set()
    for b in brand_order: seen.update(dist[b].keys())
    cats = [(c, col) for c, col in DNA_PALETTE.items() if c in seen]

    row_h = 38; label_w = 180; chart_w = 620
    bar_w = chart_w - label_w - 20
    H = row_h * len(brand_order) + 70

    parts = []
    for i, b in enumerate(brand_order):
        y = 28 + i * row_h
        total = sum(dist[b].values())
        x = label_w
        label_color = ACCENT if b == subject else INK_DIM
        weight = "700" if b == subject else "500"
        parts.append(f'<text x="{label_w-12}" y="{y+18}" text-anchor="end" font-size="13" '
                     f'font-family="Inter,sans-serif" font-weight="{weight}" fill="{label_color}">{labels[b]}</text>')
        parts.append(f'<text x="{label_w-12}" y="{y+31}" text-anchor="end" font-size="10" '
                     f'font-family="Inter,sans-serif" fill="{INK_MUTED}">n={total}</text>')
        for cat, color in cats:
            c = dist[b].get(cat, 0)
            if c == 0: continue
            w = (c / total) * bar_w
            pct = round(100 * c / total)
            parts.append(f'<rect x="{x:.1f}" y="{y+6}" width="{w:.1f}" height="22" fill="{color}"/>')
            if w >= 32:
                text_color = "#0a0a0a" if cat in ("studio_product_only", "mixed") else "white"
                parts.append(f'<text x="{x + w/2:.1f}" y="{y+21}" text-anchor="middle" font-size="11" '
                             f'font-family="Inter,sans-serif" font-weight="700" fill="{text_color}">{pct}%</text>')
            x += w

    # Legend
    lx = label_w; ly = H - 18
    leg = []
    for cat, color in cats:
        leg.append(f'<rect x="{lx}" y="{ly}" width="11" height="11" fill="{color}"/>'
                   f'<text x="{lx+16}" y="{ly+9.5}" font-size="11" font-family="Inter,sans-serif" fill="{INK_DIM}">{cat}</text>')
        lx += len(cat) * 6.5 + 30

    return f'''<figure class="chart">
      <figcaption class="chart-caption">Creative DNA · photography-style mix per brand (% of analyzed creatives)</figcaption>
      <svg viewBox="0 0 {chart_w} {H}" xmlns="http://www.w3.org/2000/svg">{"".join(parts)}{"".join(leg)}</svg>
    </figure>'''


def chart_appeal_split(conn, d):
    labels = d["brand_labels"]; subject = d["subject_brand"]
    brand_order = [b for b in d["brand_order"] if b in labels]
    dist = _attr_distribution(conn, "emotional_vs_rational", brand_order)
    brand_order = [b for b in brand_order if sum(dist[b].values()) >= 3]
    if not brand_order: return ""

    order = ["emotional", "rational", "mixed"]
    row_h = 38; label_w = 180; chart_w = 620
    bar_w = chart_w - label_w - 20
    H = row_h * len(brand_order) + 60

    parts = []
    for i, b in enumerate(brand_order):
        y = 24 + i * row_h
        total = sum(dist[b].values()) or 1
        x = label_w
        label_color = ACCENT if b == subject else INK_DIM
        weight = "700" if b == subject else "500"
        parts.append(f'<text x="{label_w-12}" y="{y+22}" text-anchor="end" font-size="13" '
                     f'font-family="Inter,sans-serif" font-weight="{weight}" fill="{label_color}">{labels[b]}</text>')
        for cat in order:
            c = dist[b].get(cat, 0)
            if c == 0: continue
            w = (c / total) * bar_w
            pct = round(100 * c / total)
            parts.append(f'<rect x="{x:.1f}" y="{y+10}" width="{w:.1f}" height="22" fill="{APPEAL_PALETTE[cat]}"/>')
            if w >= 30:
                parts.append(f'<text x="{x + w/2:.1f}" y="{y+25}" text-anchor="middle" font-size="11" '
                             f'font-family="Inter,sans-serif" font-weight="700" fill="white">{pct}%</text>')
            x += w

    lx = label_w; ly = H - 16
    leg = []
    for cat in order:
        leg.append(f'<rect x="{lx}" y="{ly}" width="11" height="11" fill="{APPEAL_PALETTE[cat]}"/>'
                   f'<text x="{lx+16}" y="{ly+9.5}" font-size="11" font-family="Inter,sans-serif" fill="{INK_DIM}">{cat}</text>')
        lx += len(cat) * 7 + 32

    return f'''<figure class="chart">
      <figcaption class="chart-caption">Emotional vs. rational appeal per brand (% of analyzed creatives)</figcaption>
      <svg viewBox="0 0 {chart_w} {H}" xmlns="http://www.w3.org/2000/svg">{"".join(parts)}{"".join(leg)}</svg>
    </figure>'''


def chart_quadrant(conn, d):
    """2x2 positioning quadrant with brand bubbles, sized by ad volume."""
    q = d.get("quadrant"); labels = d["brand_labels"]; subject = d["subject_brand"]
    if not q: return ""
    rows = conn.execute("SELECT competitor_id, COUNT(*) FROM ads GROUP BY competitor_id").fetchall()
    ads_n = {r[0]: r[1] for r in rows}

    W = 620; H = 460
    pad_l = 60; pad_r = 50; pad_t = 60; pad_b = 70
    plot_w = W - pad_l - pad_r
    plot_h = H - pad_t - pad_b

    # Background + gridlines
    parts = [
        f'<rect x="0" y="0" width="{W}" height="{H}" fill="{BG_CARD}"/>',
        # Outer plot area
        f'<rect x="{pad_l}" y="{pad_t}" width="{plot_w}" height="{plot_h}" fill="none" stroke="{INK_FAINT}" stroke-width="1"/>',
        # Center cross gridlines
        f'<line x1="{pad_l + plot_w/2}" y1="{pad_t}" x2="{pad_l + plot_w/2}" y2="{pad_t + plot_h}" stroke="{INK_FAINT}" stroke-dasharray="3,4" stroke-width="0.8"/>',
        f'<line x1="{pad_l}" y1="{pad_t + plot_h/2}" x2="{pad_l + plot_w}" y2="{pad_t + plot_h/2}" stroke="{INK_FAINT}" stroke-dasharray="3,4" stroke-width="0.8"/>',
    ]
    # Title (within card)
    parts.append(f'<text x="{pad_l - 8}" y="24" font-size="10" letter-spacing="1.2" '
                 f'font-family="Inter,sans-serif" font-weight="700" fill="{INK_MUTED}" text-transform="uppercase">CATEGORY POSITIONING</text>')

    # Axis labels
    (x_left, x_right) = q["x_axis"]
    (y_bottom, y_top) = q["y_axis"]
    parts.append(f'<text x="{pad_l + plot_w/2}" y="{pad_t - 12}" text-anchor="middle" font-size="11" font-family="Inter,sans-serif" font-weight="600" fill="{INK_DIM}" letter-spacing="0.5">{y_top.upper()}</text>')
    parts.append(f'<text x="{pad_l + plot_w/2}" y="{H - pad_b + 28}" text-anchor="middle" font-size="11" font-family="Inter,sans-serif" font-weight="600" fill="{INK_DIM}" letter-spacing="0.5">{y_bottom.upper()}</text>')
    # Y-axis side labels — rotated
    parts.append(f'<text x="{pad_l - 14}" y="{pad_t + plot_h/2}" text-anchor="middle" font-size="11" font-family="Inter,sans-serif" font-weight="600" fill="{INK_DIM}" letter-spacing="0.5" transform="rotate(-90 {pad_l - 14} {pad_t + plot_h/2})">{x_left.upper()}</text>')
    parts.append(f'<text x="{pad_l + plot_w + 18}" y="{pad_t + plot_h/2}" text-anchor="middle" font-size="11" font-family="Inter,sans-serif" font-weight="600" fill="{INK_DIM}" letter-spacing="0.5" transform="rotate(90 {pad_l + plot_w + 18} {pad_t + plot_h/2})">{x_right.upper()}</text>')

    # Bubbles
    max_n = max(ads_n.values()) if ads_n else 1
    for cid, (x_rel, y_rel) in q["brands"].items():
        if cid not in labels: continue
        cx = pad_l + x_rel * plot_w
        cy = pad_t + (1 - y_rel) * plot_h  # SVG y is flipped
        n = ads_n.get(cid, 0)
        # bubble radius scaled by sqrt(volume) so area is proportional
        r = 18 + math.sqrt(max(n, 1) / max_n) * 38
        is_subject = (cid == subject)
        if is_subject:
            # Halo + filled
            parts.append(f'<circle cx="{cx}" cy="{cy}" r="{r+8}" fill="{ACCENT}" opacity="0.18"/>')
            parts.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{ACCENT}" opacity="0.95"/>')
            text_fill = "#0a0a0a"
            text_weight = "700"
        else:
            parts.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{INK_FAINT}" opacity="0.45" stroke="{INK_MUTED}" stroke-width="1"/>')
            text_fill = INK_DIM
            text_weight = "500"
        # Label inside bubble
        label = labels[cid]
        if " " in label and len(label) > 10:
            # break into two lines
            parts_label = label.split(" ", 1)
            parts.append(f'<text x="{cx}" y="{cy - 4}" text-anchor="middle" font-size="11" font-family="Inter,sans-serif" font-weight="{text_weight}" fill="{text_fill}">{parts_label[0]}</text>')
            parts.append(f'<text x="{cx}" y="{cy + 9}" text-anchor="middle" font-size="11" font-family="Inter,sans-serif" font-weight="{text_weight}" fill="{text_fill}">{parts_label[1]}</text>')
        else:
            parts.append(f'<text x="{cx}" y="{cy + 4}" text-anchor="middle" font-size="11.5" font-family="Inter,sans-serif" font-weight="{text_weight}" fill="{text_fill}">{label}</text>')

    return f'''<figure class="chart quadrant-chart">
      <svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg">{"".join(parts)}</svg>
      <figcaption class="quadrant-caption">{d.get("quadrant_caption", "")}</figcaption>
    </figure>'''


# =============== reference board ===============

def _renderable_thumb_path(asset_path, asset_type):
    """For video creatives, the asset_path is an mp4 — useless for HTML
    embedding. Resolve the first extracted frame in the same directory; fall
    back to the asset_path if no frames exist (lets evicted/legacy rows still
    render their first-frame paths)."""
    if not asset_path:
        return None
    p = Path(asset_path)
    if (asset_type in ("video", "video_evicted")) and p.suffix.lower() == ".mp4":
        try:
            frames = sorted(p.parent.glob("frame_*_t*.jpg"))
            if frames:
                return str(frames[0])
        except Exception:
            pass
    return str(p)


def _video_duration_chip(analysis_json):
    """Return an HTML chip with formatted duration for video creatives, or ''."""
    if not analysis_json:
        return ""
    try:
        if isinstance(analysis_json, str):
            analysis_json = json.loads(analysis_json)
    except Exception:
        return ""
    vm = analysis_json.get("video_meta") or {}
    sec = vm.get("duration_sec")
    if not sec:
        return ""
    s = int(round(float(sec)))
    return f'<span class="dur-chip">{s // 60}:{s % 60:02d}</span>'


def _b64_image(path, max_width=None, max_height=None):
    p = Path(path)
    if not p.exists(): return None
    mime, _ = mimetypes.guess_type(str(p))
    # Resize big images on the fly (full-page screenshots can be 6+ MB)
    if HAS_PIL and (max_width or max_height):
        try:
            with Image.open(p) as im:
                im = im.convert("RGB") if im.mode in ("RGBA", "P") else im
                w, h = im.size
                target_w = max_width or w
                target_h = max_height or h
                ratio = min(target_w / w, target_h / h, 1.0)
                if ratio < 1.0:
                    new_size = (int(w * ratio), int(h * ratio))
                    im = im.resize(new_size, Image.LANCZOS)
                buf = io.BytesIO()
                im.save(buf, format="JPEG", quality=82, optimize=True)
                b64 = base64.b64encode(buf.getvalue()).decode("ascii")
                return f"data:image/jpeg;base64,{b64}"
        except Exception:
            pass  # fall through to raw bytes
    return f"data:{mime or 'image/jpeg'};base64,{base64.b64encode(p.read_bytes()).decode('ascii')}"


def top_creatives_by_reach(conn, d):
    """Auto-populated companion to reference_board(): each brand's top served
    ads, ranked by the popularity_score proxy (SERP rank × run duration).
    Editorial captions live in reference_board; this section is hard data."""
    labels = d["brand_labels"]
    brand_order = d["brand_order"]
    blocks = []
    for cid in brand_order:
        if cid not in labels:
            continue
        max_rank_row = conn.execute(
            "SELECT MAX(serp_position_rank) FROM ads WHERE competitor_id=? "
            "AND serp_position_rank IS NOT NULL",
            (cid,),
        ).fetchone()
        max_rank = (max_rank_row[0] or 0) if max_rank_row else 0
        # Prefer a video creative's asset_path when one exists (we swap to the
        # first frame below); fall back to the first image creative.
        rows = conn.execute(
            "SELECT a.ad_archive_id, a.serp_position_rank, a.start_date, "
            "a.last_seen, a.active, a.body_text, a.cta_type, "
            "COALESCE(c_video.asset_path, c_img.asset_path) AS thumb_path, "
            "COALESCE(c_video.asset_type, c_img.asset_type) AS thumb_asset_type, "
            "COALESCE(c_video.analysis_json, c_img.analysis_json) AS thumb_analysis_json "
            "FROM ads a "
            "LEFT JOIN creatives c_video ON c_video.id = ("
            "  SELECT id FROM creatives WHERE ad_id=a.id "
            "  AND asset_type IN ('video','video_evicted') ORDER BY id LIMIT 1) "
            "LEFT JOIN creatives c_img ON c_img.id = ("
            "  SELECT id FROM creatives WHERE ad_id=a.id "
            "  AND asset_type='image' ORDER BY id LIMIT 1) "
            "WHERE a.competitor_id=?",
            (cid,),
        ).fetchall()
        if not rows:
            continue
        scored = []
        for r in rows:
            d_row = dict(r)
            d_row["popularity_score"] = popularity_score(
                d_row.get("serp_position_rank"),
                d_row.get("start_date"),
                d_row.get("last_seen"),
                d_row.get("active") or 0,
                max_rank,
            )
            if d_row.get("start_date") and d_row.get("last_seen"):
                try:
                    from datetime import datetime as _dt
                    s = _dt.fromisoformat(d_row["start_date"][:10])
                    e = _dt.fromisoformat(d_row["last_seen"][:10])
                    d_row["run_days"] = max((e - s).days, 0)
                except (ValueError, TypeError):
                    d_row["run_days"] = 0
            else:
                d_row["run_days"] = 0
            scored.append(d_row)
        scored = [s for s in scored if s["popularity_score"] > 0]
        scored.sort(key=lambda r: r["popularity_score"], reverse=True)
        top = scored[:4]
        if not top:
            continue
        cards = []
        for ad in top:
            # Resolve the renderable thumb (first frame for videos, asset_path
            # for images). The strategy report is a static HTML doc — we
            # always embed images, never mp4 (would balloon to multi-MB).
            asset_type = ad.get("thumb_asset_type") or "image"
            renderable = _renderable_thumb_path(ad.get("thumb_path"), asset_type)
            b64 = _b64_image(renderable, max_width=400, max_height=400) if renderable else ""
            dur_chip = _video_duration_chip(ad.get("thumb_analysis_json"))
            img_html = (
                f'<img src="{b64}" alt="ad {_html.escape(str(ad["ad_archive_id"]))}"/>'
                if b64
                else '<div class="tcr-noimg">no thumbnail</div>'
            )
            rank_label = (
                f"#{int(ad['serp_position_rank']) + 1}"
                if ad.get("serp_position_rank") is not None
                else "—"
            )
            body = (ad.get("body_text") or "").replace("\n", " ").strip()[:140]
            cta = ad.get("cta_type") or ""
            cards.append(f"""
            <div class="tcr-card">
              <a class="thumb-wrap" data-asset-type="{_html.escape(asset_type)}"
                 href="https://www.facebook.com/ads/library/?id={_html.escape(str(ad['ad_archive_id']))}"
                 target="_blank" rel="noopener">
                {img_html}
                {dur_chip}
              </a>
              <div class="tcr-chips">
                <span class="tcr-chip tcr-rank">RANK {rank_label}</span>
                <span class="tcr-chip tcr-days">{int(ad['run_days'])}d</span>
              </div>
              <p class="tcr-body">{_html.escape(body)}</p>
              <div class="tcr-meta">
                <span class="tcr-cta">{_html.escape(cta)}</span>
                <code class="tcr-id">#{_html.escape(str(ad['ad_archive_id']))}</code>
              </div>
            </div>""")
        blocks.append(f"""
        <section class="tcr-brand">
          <h3 class="tcr-brand-title">{_html.escape(labels[cid])}</h3>
          <div class="tcr-grid">{"".join(cards)}</div>
        </section>""")
    if not blocks:
        return ""
    return f"""<section class="top-creatives-reach">
      <div class="eyebrow">Top served ads</div>
      <h2 class="board-title">What's actually carrying spend</h2>
      <p class="tcr-intro">Ranked by Meta Ad Library's "most-served" sort × run duration. <strong>Proxy, not raw impressions</strong> — Meta does not expose impression numbers for commercial US advertisers. Intra-brand only; a brand with 200 ads and a brand with 5 ads are not comparable. Auto-populated from the corpus; see Reference Board below for hand-curated picks.</p>
      {"".join(blocks)}
    </section>"""


def reference_board(conn, d):
    labels = d["brand_labels"]
    themes_html = []
    for theme in d["reference_themes"]:
        items_html = []
        for item in theme["items"]:
            ad_id = item["ad_archive_id"]
            row = conn.execute(
                "SELECT a.competitor_id, a.page_name, c.asset_path, c.asset_type, c.analysis_json "
                "FROM ads a LEFT JOIN creatives c ON c.ad_id = a.id "
                "WHERE a.ad_archive_id = ? AND c.asset_path IS NOT NULL "
                # Prefer video creative rows (they carry duration/transcript);
                # if absent the image row wins.
                "ORDER BY CASE c.asset_type WHEN 'video' THEN 0 WHEN 'video_evicted' THEN 1 ELSE 2 END "
                "LIMIT 1",
                (ad_id,),
            ).fetchone()
            if not row: continue
            cid = item.get("comp_override") or row[0]
            asset_type = row[3] or "image"
            renderable = _renderable_thumb_path(row[2], asset_type)
            b64 = _b64_image(renderable)
            if not b64: continue
            dur_chip = _video_duration_chip(row[4])
            items_html.append(f'''
            <div class="ref-item">
              <a class="thumb-wrap" data-asset-type="{_html.escape(asset_type)}"
                 href="https://www.facebook.com/ads/library/?id={_html.escape(str(ad_id))}" target="_blank" rel="noopener">
                <img src="{b64}" alt="ad {_html.escape(str(ad_id))}"/>
                {dur_chip}
              </a>
              <div class="ref-meta">
                <span class="ref-brand">{_html.escape(labels.get(cid, cid))}</span>
                <code class="ref-id">#{_html.escape(str(ad_id))}</code>
              </div>
              <p class="ref-caption">{_html.escape(item["caption"])}</p>
            </div>''')
        if not items_html: continue
        themes_html.append(f'''
        <section class="ref-theme">
          <h3 class="ref-theme-title">{_html.escape(theme["title"])}</h3>
          <p class="ref-theme-intro">{_html.escape(theme["intro"])}</p>
          <div class="ref-grid">{"".join(items_html)}</div>
        </section>''')

    if not themes_html: return ""
    return f'''<section class="reference-board">
      <div class="eyebrow">Reference Board</div>
      <h2 class="board-title">The evidence, organized by strategic theme</h2>
      <p class="ref-board-intro">Curated thumbnails from the analyzed corpus. Captions are hand-written interpretations — what each reference illustrates strategically, not what the ad itself says. Click any thumbnail to open the original in Meta Ad Library.</p>
      {"".join(themes_html)}
    </section>'''


# =============== site content deep-dive ===============

def site_content_section(conn, d):
    """Dedicated section: per-brand homepage site-content cards using
    the structured analysis stored in homepage_promos.raw_json. Also surfaces
    capture limitations honestly."""
    labels = d["brand_labels"]
    brand_order = d["brand_order"]
    subject = d["subject_brand"]

    # Pull latest homepage_promos row per brand + the screenshot path from the
    # joined website observation (so we can show a thumbnail).
    rows = conn.execute("""
        SELECT hp.competitor_id, hp.headline, hp.subhead, hp.primary_cta_text,
               hp.offer_claim, hp.expiration, hp.confidence, hp.raw_json,
               o.parsed_json
        FROM homepage_promos hp
        LEFT JOIN observations o ON o.id = hp.observation_id
        GROUP BY hp.competitor_id
        HAVING MAX(hp.observed_at)
    """).fetchall()
    by_brand = {}
    for r in rows:
        try:
            raw = json.loads(r["raw_json"] or "{}")
        except Exception:
            raw = {}
        screenshot_path = None
        try:
            parsed = json.loads(r["parsed_json"] or "{}")
            screenshot_path = parsed.get("screenshot_path")
        except Exception:
            pass
        by_brand[r["competitor_id"]] = {
            "headline": r["headline"],
            "subhead": r["subhead"],
            "primary_cta": r["primary_cta_text"],
            "offer_claim": r["offer_claim"],
            "expiration": r["expiration"],
            "confidence": r["confidence"],
            "raw": raw,
            "screenshot_path": screenshot_path,
        }
    # Also need to detect captcha-blocked brands so we can list them in limitations
    blocked_brands = []
    blocked_rows = conn.execute("""
        SELECT s.competitor_id, json_extract(o.parsed_json, '$.block_vendor') AS vendor
        FROM observations o JOIN sources s ON s.id = o.source_id
        WHERE s.type = 'website' AND json_extract(o.parsed_json, '$.blocked') = 1
        GROUP BY s.competitor_id
    """).fetchall()
    blocked_brands = [(r["competitor_id"], r["vendor"]) for r in blocked_rows]

    if not by_brand:
        return ""

    cards = []
    for cid in brand_order:
        if cid not in labels: continue
        if cid not in by_brand: continue
        entry = by_brand[cid]; raw = entry["raw"]
        is_subject = (cid == subject)
        # Thumbnail — resize aggressively (these are full-page captures, can be 6 MB+)
        thumb_html = ""
        if entry["screenshot_path"]:
            b64 = _b64_image(entry["screenshot_path"], max_width=640, max_height=960)
            if b64:
                thumb_html = f'<div class="sc-thumb"><img src="{b64}" alt="{labels[cid]} homepage"></div>'
        # Strategist one-liner banner
        one_liner = raw.get("strategist_one_liner") or ""
        # Headline + subhead
        head = entry["headline"] or ""
        sub = entry["subhead"] or ""
        # Primary CTA
        pcta = entry["primary_cta"] or ""
        # Secondary CTAs
        sec_ctas = raw.get("secondary_ctas") or []
        secondary_chips = "".join(
            f'<code class="sc-cta-chip">{_html.escape(str(s))}</code>'
            for s in sec_ctas[:6]
        ) if sec_ctas else ""
        # Messaging stance
        stance = raw.get("messaging_stance") or {}
        stance_html = ""
        if stance:
            stance_tags = []
            for label_key, value in [
                ("appeal", stance.get("rational_vs_emotional")),
                ("signal", stance.get("value_vs_premium_signaling")),
                ("leads", stance.get("lead_with")),
                ("tone", stance.get("tone_one_word")),
            ]:
                if value:
                    stance_tags.append(
                        f'<span class="sc-stance"><span class="sc-stance-k">{label_key}</span>'
                        f'<span class="sc-stance-v">{_html.escape(str(value))}</span></span>'
                    )
            stance_html = '<div class="sc-stance-row">' + "".join(stance_tags) + '</div>'
        # Positioning
        positioning = raw.get("positioning_statement") or ""
        # Design critique notable choices (1-2 bullets, not the full critique)
        design = raw.get("design_critique") or {}
        notable = design.get("notable_design_choices") or []
        # Works / misses
        works = raw.get("what_this_homepage_does_well") or []
        misses = raw.get("what_it_misses") or []
        # Offer badge
        offer_badge = (f'<div class="sc-offer-badge">{_html.escape(str(entry["offer_claim"]))}</div>'
                       if entry["offer_claim"] else "")
        # Confidence
        conf_pill = ""
        if entry["confidence"] is not None:
            conf_pill = f'<div class="sc-conf-pill">CONF {int(round(entry["confidence"]*100))}%</div>'

        works_html = (
            '<div class="sc-ww"><div class="sc-ww-label sc-ww-good">What works</div>'
            '<ul>' + "".join(f'<li>{_html.escape(str(w))}</li>' for w in works[:3]) + '</ul></div>'
        ) if works else ""
        misses_html = (
            '<div class="sc-ww"><div class="sc-ww-label sc-ww-bad">What it misses</div>'
            '<ul>' + "".join(f'<li>{_html.escape(str(m))}</li>' for m in misses[:3]) + '</ul></div>'
        ) if misses else ""

        notable_html = ""
        if notable:
            notable_html = (
                '<details class="sc-design"><summary>Design notes</summary>'
                '<ul>' + "".join(f'<li>{_html.escape(str(n))}</li>' for n in notable[:4]) + '</ul></details>'
            )

        positioning_html = (
            f'<div class="sc-positioning"><div class="sc-section-label">Positioning read</div>'
            f'<p>{_html.escape(positioning)}</p></div>'
        ) if positioning else ""

        primary_cta_html = (
            f'<div class="sc-cta-row"><span class="sc-cta-label">Primary CTA</span>'
            f'<code class="sc-cta-primary">{_html.escape(str(pcta))}</code></div>'
        ) if pcta else ""

        secondary_cta_html = (
            f'<div class="sc-cta-row"><span class="sc-cta-label">Secondary ({len(sec_ctas)})</span>'
            f'<div class="sc-cta-chips">{secondary_chips}</div></div>'
        ) if sec_ctas else ""

        subject_class = ' sc-subject' if is_subject else ''
        # Build the conditional snippets up-front so they're real values, not nested
        # f-string conditional expressions (which were behaving oddly inside the
        # outer triple-quoted f-string).
        oneliner_html = f'<div class="sc-oneliner">{_html.escape(one_liner)}</div>' if one_liner else ''
        headline_html = f'<div class="sc-headline">{_html.escape(str(head))}</div>' if head else ''
        subhead_html = f'<div class="sc-subhead">{_html.escape(str(sub))}</div>' if sub else ''
        ww_row_html = f'<div class="sc-ww-row">{works_html}{misses_html}</div>' if (works_html or misses_html) else ''
        cards.append(f'''
        <article class="sc-card{subject_class}">
          <header class="sc-card-head">
            <div class="sc-brand">{_html.escape(labels[cid])}</div>
            {conf_pill}
          </header>
          {thumb_html}
          <div class="sc-card-body">
            {oneliner_html}
            {offer_badge}
            {headline_html}
            {subhead_html}
            {primary_cta_html}
            {secondary_cta_html}
            {stance_html}
            {positioning_html}
            {ww_row_html}
            {notable_html}
          </div>
        </article>''')

    # Limitations footer for the section
    blocked_text = ""
    if blocked_brands:
        bvs = ", ".join(f"{labels.get(b, b)} ({v or 'unknown'})" for b, v in blocked_brands)
        blocked_text = f'<li><strong>Captcha-blocked brands:</strong> {bvs} — anti-bot walls (PerimeterX, etc.) returned a challenge page instead of the homepage. No site-content analysis is possible until we route around the block.</li>'

    limitations_html = f'''
    <div class="sc-limitations">
      <div class="sc-section-label">Capture limitations to know about</div>
      <ul>
        {blocked_text}
        <li><strong>Hero crop is bounded at 1500px tall</strong> — multi-fold sections below the visible hero (testimonials, FAQs, secondary product modules) are not in the homepage analysis. They <em>are</em> in the per-image creative taxonomy, but framed as image-only rather than as full-context site content.</li>
        <li><strong>Regional selectors block first-visit captures</strong> — Deckorators's English/Français selector and similar interstitials mean some brands' "first hero" is actually a returning-user view. The captured hero may differ from a cold visitor's experience.</li>
        <li><strong>Page-level CTA buttons aren't always in the captured images</strong> — the homepage_image taxonomy reads CTAs only when they appear inside a captured creative tile. Header / floating / sticky-bar CTAs visible on the live site can be missing.</li>
        <li><strong>Hand-authored fallback in use</strong> — the automated homepage-analysis LLM pipeline (homepage_promos via Anthropic API) is offline pending an API key. The analyses surfaced here were authored inline by Claude from direct reads of each landing_hero.png + the captured visible-text / regions / per-image taxonomy — re-runnable manually but not nightly.</li>
        <li><strong>Live A/B tests and personalization are invisible</strong> — most sites in the set run experiments. The captured hero is a single observation, not a representative sample of what every visitor sees.</li>
      </ul>
    </div>'''

    return f'''<section class="site-content-section">
      <div class="eyebrow">Site Content Deep-Dive</div>
      <h2 class="board-title">What each brand is actually saying on its homepage</h2>
      <p class="ref-board-intro">Per-brand structured read of the live homepage — headline, primary CTA, messaging stance, what's working, what's missing. Grounded in direct reads of each landing hero and the captured visible-text excerpts.</p>
      <div class="sc-grid">{"".join(cards)}</div>
      {limitations_html}
    </section>'''


# =============== body transforms (eyebrows + highlights) ===============

# Section eyebrow scheme — sequential, applied in order
SECTION_STAGES = [
    "01 · The Read",
    "02 · What We See",
    "03 · The Map",
    "04 · The Opening",
    "05 · The Push",
]


def add_section_eyebrows(html: str) -> str:
    """Wrap each <h2> in a section block with an eyebrow above it, sequentially."""
    h2_iter = list(re.finditer(r'(<h2[^>]*>)', html))
    if not h2_iter: return html
    out = []
    last_end = 0
    for i, m in enumerate(h2_iter):
        if i >= len(SECTION_STAGES): break
        eyebrow = SECTION_STAGES[i]
        out.append(html[last_end:m.start()])
        out.append(f'<div class="eyebrow">{eyebrow}</div>')
        last_end = m.start()
    out.append(html[last_end:])
    return "".join(out)


def apply_inline_highlights(html: str, brand_labels: dict, phrases: list[str]) -> str:
    """Wrap brand names and key phrases in <span class="hl">.
    Avoid double-wrapping or hitting text inside HTML tags/code blocks."""
    # Skip-zone protection — collect all tag spans + code spans; we'll splice around them
    # Simple approach: split on tags, only transform text nodes.
    tag_re = re.compile(r'(<[^>]+>)')
    parts = tag_re.split(html)

    # Targets sorted longest-first so longer phrases match before substrings
    targets = sorted(set(list(brand_labels.values()) + list(phrases)),
                     key=len, reverse=True)
    # Filter trivially short / risky targets
    targets = [t for t in targets if t and len(t) >= 3]

    # Compile a regex with word-boundary-ish matching
    def make_pattern(t):
        # don't require word boundaries for punctuation-heavy phrases
        if re.search(r'[^\w\s]', t):
            return re.escape(t)
        return rf'\b{re.escape(t)}\b'
    big_re = re.compile("|".join(f"({make_pattern(t)})" for t in targets))

    # Track context — don't highlight inside <h1>, <h2>, eyebrow, ref-brand, ref-id, code
    inside_skip = False
    skip_tags = ("<h1", "<h2", "<h3", "<code", '<span class="ref-brand"',
                 '<code class="ref-id"', '<div class="eyebrow"',
                 '<figcaption', '<text ', "<svg")
    skip_close = ("</h1>", "</h2>", "</h3>", "</code>", "</span>",
                  "</div>", "</figcaption>", "</text>", "</svg>")

    depth_stack = []  # stack of "open skip tag" markers

    def is_skip_open(tag):
        low = tag.lower().lstrip()
        return any(low.startswith(s) for s in skip_tags)

    def is_skip_close(tag):
        low = tag.lower().strip()
        return low in [s for s in skip_close]

    out = []
    for part in parts:
        if part.startswith("<"):
            out.append(part)
            if is_skip_open(part):
                depth_stack.append("skip")
            elif is_skip_close(part) and depth_stack:
                depth_stack.pop()
            continue
        if depth_stack:
            out.append(part); continue
        # Apply highlight in text nodes
        def repl(m):
            return f'<span class="hl">{m.group(0)}</span>'
        out.append(big_re.sub(repl, part))
    return "".join(out)


# =============== HTML / CSS template ===============

CSS = f"""
:root {{
  --accent: {ACCENT};
  --accent-dim: {ACCENT_DIM};
  --bg: {BG};
  --bg-card: {BG_CARD};
  --bg-card-border: {BG_CARD_BORDER};
  --ink: {INK};
  --ink-dim: {INK_DIM};
  --ink-muted: {INK_MUTED};
  --ink-faint: {INK_FAINT};
}}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; padding: 0; }}
html {{ background: var(--bg); }}
body {{
  background: var(--bg);
  color: var(--ink-dim);
  font-family: "Inter", -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
  font-size: 15px;
  line-height: 1.65;
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}}

.wrapper {{ max-width: 920px; margin: 0 auto; padding: 32px 48px 96px; }}

/* ---- top header band ---- */
.brief-band {{
  display: flex; justify-content: space-between; align-items: baseline;
  padding-bottom: 14px;
  border-bottom: 1px solid var(--ink-faint);
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: 1.5px;
  text-transform: uppercase;
  color: var(--accent);
  margin-bottom: 56px;
}}
.brief-band .meta-right {{ color: var(--ink-muted); }}

/* ---- magazine hero ---- */
.hero h1 {{
  font-family: "Charter", "Iowan Old Style", "Source Serif Pro", Georgia, serif;
  font-weight: 400;
  font-size: 64px;
  line-height: 1.05;
  letter-spacing: -0.02em;
  color: var(--ink);
  margin: 0 0 32px;
  max-width: 760px;
}}
.hero h1 em {{
  font-style: italic;
  color: var(--accent);
  font-weight: 400;
}}
.hero .subtitle {{
  font-family: "Charter", "Source Serif Pro", Georgia, serif;
  font-style: italic;
  font-size: 19px;
  line-height: 1.5;
  color: var(--ink-muted);
  max-width: 640px;
  margin: 0 0 64px;
}}

/* ---- section eyebrows ---- */
.eyebrow {{
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 1.8px;
  text-transform: uppercase;
  color: var(--accent);
  margin: 56px 0 14px;
}}
.eyebrow + h2 {{ margin-top: 0 !important; }}

/* ---- body type ---- */
.brief h2 {{
  font-family: "Charter", "Source Serif Pro", Georgia, serif;
  font-weight: 400;
  font-size: 30px;
  line-height: 1.2;
  letter-spacing: -0.01em;
  color: var(--ink);
  margin: 56px 0 24px;
}}
.brief h3 {{
  font-family: "Inter", sans-serif;
  font-weight: 700;
  font-size: 17px;
  margin: 36px 0 12px;
  color: var(--ink);
  letter-spacing: -0.005em;
}}
.brief h4 {{
  font-family: "Inter", sans-serif;
  font-weight: 700;
  font-size: 12px;
  margin: 28px 0 10px;
  text-transform: uppercase;
  letter-spacing: 1.2px;
  color: var(--accent);
}}
.brief p {{
  margin: 0 0 18px;
  color: var(--ink-dim);
}}
/* Pull-quote lead paragraph after each section title */
.brief h2 + p {{
  font-size: 17px;
  line-height: 1.65;
  color: var(--ink);
}}

.brief strong {{ font-weight: 700; color: var(--ink); }}
.brief em {{ font-style: italic; }}

/* Inline highlights for brand names + key data */
.brief .hl {{
  color: var(--accent);
  font-weight: 600;
}}

/* Inline ad-id pills */
.brief code {{
  font-family: "JetBrains Mono", "SF Mono", ui-monospace, Menlo, monospace;
  font-size: 12.5px;
  background: rgba(212, 240, 87, 0.10);
  color: var(--accent);
  padding: 1px 6px;
  border-radius: 3px;
  border: 1px solid rgba(212, 240, 87, 0.25);
  word-break: break-all;
}}
.brief pre {{
  background: var(--bg-card); padding: 14px 16px; border-radius: 4px;
  border: 1px solid var(--bg-card-border); overflow-x: auto;
  font-size: 13px;
}}
.brief pre code {{ background: none; border: none; padding: 0; color: var(--ink-dim); }}

/* Lists */
.brief ul, .brief ol {{ margin: 0 0 22px; padding-left: 24px; }}
.brief li {{ margin: 10px 0; }}
.brief li::marker {{ color: var(--accent-dim); }}

/* Blockquote */
.brief blockquote {{
  border-left: 2px solid var(--accent);
  margin: 24px 0;
  padding: 6px 0 6px 20px;
  color: var(--ink-muted);
  font-style: italic;
}}

/* Tables */
.brief table {{
  width: 100%;
  border-collapse: collapse;
  margin: 28px 0;
  font-size: 14px;
  background: var(--bg-card);
  border: 1px solid var(--bg-card-border);
  border-radius: 4px;
  overflow: hidden;
}}
.brief th {{
  text-align: left;
  font-weight: 700;
  font-size: 10.5px;
  text-transform: uppercase;
  letter-spacing: 1.2px;
  color: var(--accent);
  padding: 14px 18px;
  border-bottom: 1px solid var(--accent);
  vertical-align: bottom;
  background: rgba(212, 240, 87, 0.04);
}}
.brief td {{
  padding: 14px 18px;
  border-bottom: 1px solid var(--bg-card-border);
  vertical-align: top;
  color: var(--ink-dim);
}}
.brief tr:last-child td {{ border-bottom: none; }}
.brief td:first-child {{
  font-weight: 700;
  color: var(--accent);
  white-space: nowrap;
  width: 22%;
}}

/* Links */
.brief a {{
  color: var(--accent);
  text-decoration: none;
  border-bottom: 1px solid rgba(212, 240, 87, 0.3);
  transition: border-color 0.15s;
}}
.brief a:hover {{ border-bottom-color: var(--accent); }}
.brief code a {{ border-bottom: none; }}

/* Horizontal rule */
.brief hr {{
  border: none; height: 1px;
  background: var(--ink-faint);
  margin: 56px 0;
}}

/* ---- charts ---- */
figure.chart {{
  margin: 36px 0;
  padding: 22px 22px 18px;
  background: var(--bg-card);
  border: 1px solid var(--bg-card-border);
  border-radius: 6px;
}}
figure.chart svg {{ width: 100%; height: auto; display: block; }}
figcaption.chart-caption {{
  font-family: "Inter", sans-serif;
  font-size: 10.5px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 1.5px;
  color: var(--ink-muted);
  margin-bottom: 18px;
}}
figure.quadrant-chart {{ padding: 0; }}
figure.quadrant-chart svg {{ border-radius: 6px 6px 0 0; }}
.quadrant-caption {{
  font-family: "Charter", Georgia, serif;
  font-style: italic;
  font-size: 14px;
  color: var(--ink-dim);
  padding: 14px 24px 18px;
  margin: 0;
  background: var(--bg-card);
  border-top: 1px solid var(--bg-card-border);
  border-radius: 0 0 6px 6px;
  line-height: 1.5;
}}

/* ---- video thumbnail treatment (shared by top-served + reference board) ---- */
.thumb-wrap {{ position: relative; display: block; }}
.thumb-wrap[data-asset-type="video"]::after,
.thumb-wrap[data-asset-type="video_evicted"]::after {{
  content: ""; position: absolute; inset: 0; pointer-events: none;
  background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 60 60'><circle cx='30' cy='30' r='26' fill='rgba(0,0,0,0.6)'/><polygon points='24,18 24,42 44,30' fill='%23d4f057'/></svg>");
  background-repeat: no-repeat; background-position: center; background-size: 36px 36px;
}}
.thumb-wrap .dur-chip {{
  position: absolute; bottom: 6px; left: 6px; background: rgba(0,0,0,0.78);
  color: var(--accent); font-family: "Inter", sans-serif; font-size: 10px;
  font-weight: 700; padding: 2px 6px; border-radius: 2px; letter-spacing: 0.4px;
  pointer-events: none;
}}

/* ---- top served creatives (auto-populated) ---- */
section.top-creatives-reach {{
  margin-top: 88px;
  padding-top: 36px;
  border-top: 2px solid var(--accent);
}}
section.top-creatives-reach > .eyebrow {{ margin-top: 0; }}
section.top-creatives-reach .board-title {{
  font-family: "Charter", Georgia, serif;
  font-weight: 400;
  font-size: 32px;
  line-height: 1.2;
  letter-spacing: -0.01em;
  color: var(--ink);
  margin: 0 0 14px;
  max-width: 640px;
}}
.tcr-intro {{
  color: var(--ink-muted);
  font-style: italic;
  margin: 0 0 40px;
  font-size: 14.5px;
  font-family: "Charter", Georgia, serif;
  line-height: 1.55;
  max-width: 720px;
}}
.tcr-intro strong {{ color: var(--accent); font-weight: 700; font-style: normal; }}
.tcr-brand {{ margin-bottom: 48px; }}
.tcr-brand-title {{
  font-family: "Inter", sans-serif;
  font-weight: 700;
  font-size: 13px;
  letter-spacing: 1.2px;
  text-transform: uppercase;
  color: var(--accent);
  margin: 0 0 16px;
}}
.tcr-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 18px;
}}
.tcr-card {{
  background: var(--bg-card);
  border: 1px solid var(--bg-card-border);
  border-radius: 4px;
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}}
.tcr-card a {{ display: block; border: none; }}
.tcr-card img {{
  width: 100%;
  aspect-ratio: 1 / 1;
  object-fit: cover;
  background: #1a1a1a;
  border-radius: 3px;
  display: block;
}}
.tcr-noimg {{
  width: 100%;
  aspect-ratio: 1 / 1;
  background: #1a1a1a;
  border-radius: 3px;
  display: flex; align-items: center; justify-content: center;
  color: var(--ink-muted);
  font-size: 11px;
}}
.tcr-chips {{ display: flex; gap: 5px; flex-wrap: wrap; }}
.tcr-chip {{
  font-family: "Inter", sans-serif;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.5px;
  padding: 2px 7px;
  border-radius: 3px;
}}
.tcr-chip.tcr-rank {{
  background: rgba(212, 240, 87, 0.13);
  color: var(--accent);
  border: 1px solid rgba(212, 240, 87, 0.3);
}}
.tcr-chip.tcr-days {{
  background: rgba(255, 255, 255, 0.04);
  color: var(--ink-dim);
  border: 1px solid var(--bg-card-border);
}}
.tcr-body {{
  font-family: "Charter", Georgia, serif;
  font-size: 13px;
  line-height: 1.45;
  color: var(--ink-dim);
  margin: 0;
  flex: 1;
}}
.tcr-meta {{
  display: flex; align-items: baseline; justify-content: space-between; gap: 8px;
  font-size: 11px;
}}
.tcr-cta {{ color: var(--accent); font-weight: 700; }}
.tcr-id {{ color: var(--ink-muted); font-family: monospace; font-size: 10px; }}

/* ---- reference board ---- */
section.reference-board {{
  margin-top: 88px;
  padding-top: 36px;
  border-top: 2px solid var(--accent);
}}
section.reference-board > .eyebrow {{ margin-top: 0; }}
section.reference-board .board-title {{
  font-family: "Charter", Georgia, serif;
  font-weight: 400;
  font-size: 32px;
  line-height: 1.2;
  letter-spacing: -0.01em;
  color: var(--ink);
  margin: 0 0 14px;
  max-width: 640px;
}}
.ref-board-intro {{
  color: var(--ink-muted);
  font-style: italic;
  margin: 0 0 40px;
  font-size: 15px;
  font-family: "Charter", Georgia, serif;
  max-width: 640px;
}}
.ref-theme {{ margin-bottom: 56px; }}
.ref-theme-title {{
  font-family: "Inter", sans-serif;
  font-weight: 700;
  font-size: 16px;
  margin: 0 0 6px;
  color: var(--ink);
  letter-spacing: -0.005em;
}}
.ref-theme-intro {{
  color: var(--ink-muted);
  font-size: 14px;
  margin: 0 0 22px;
  line-height: 1.55;
  max-width: 640px;
  font-family: "Charter", Georgia, serif;
  font-style: italic;
}}
.ref-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 24px;
}}
.ref-item {{ display: flex; flex-direction: column; }}
.ref-item a {{ display: block; border: none; }}
.ref-item img {{
  width: 100%;
  aspect-ratio: 1 / 1;
  object-fit: cover;
  background: var(--bg-card);
  border-radius: 4px;
  border: 1px solid var(--bg-card-border);
  display: block;
}}
.ref-meta {{
  display: flex; align-items: baseline; justify-content: space-between;
  margin-top: 12px; gap: 8px;
}}
.ref-brand {{
  font-size: 10.5px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 1.2px;
  color: var(--accent);
}}
.ref-id {{
  font-size: 10px;
  color: var(--ink-muted);
  background: rgba(255,255,255,0.04);
  padding: 1px 5px;
  border-radius: 3px;
  border: 1px solid var(--bg-card-border);
  word-break: break-all;
}}
.ref-caption {{
  font-family: "Charter", Georgia, serif;
  font-style: italic;
  font-size: 13.5px;
  line-height: 1.5;
  color: var(--ink-dim);
  margin: 8px 0 0;
}}

/* ---- site content deep-dive ---- */
section.site-content-section {{
  margin-top: 88px;
  padding-top: 36px;
  border-top: 2px solid var(--accent);
}}
section.site-content-section > .eyebrow {{ margin-top: 0; }}
section.site-content-section .board-title {{
  font-family: "Charter", Georgia, serif;
  font-weight: 400;
  font-size: 32px;
  line-height: 1.2;
  letter-spacing: -0.01em;
  color: var(--ink);
  margin: 0 0 14px;
  max-width: 640px;
}}
.sc-grid {{
  display: grid;
  grid-template-columns: 1fr;
  gap: 28px;
  margin-top: 36px;
}}
.sc-card {{
  background: var(--bg-card);
  border: 1px solid var(--bg-card-border);
  border-radius: 6px;
  overflow: hidden;
  display: grid;
  grid-template-columns: 320px 1fr;
}}
.sc-card.sc-subject {{
  border: 1px solid var(--accent);
  box-shadow: 0 0 0 1px rgba(212, 240, 87, 0.18);
}}
.sc-card-head {{
  grid-column: 1 / -1;
  display: flex; justify-content: space-between; align-items: center;
  padding: 14px 20px;
  border-bottom: 1px solid var(--bg-card-border);
  background: rgba(0,0,0,0.2);
}}
.sc-brand {{
  font-family: "Inter", sans-serif;
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 1.2px;
  text-transform: uppercase;
  color: var(--accent);
}}
.sc-conf-pill {{
  font-family: "Inter", sans-serif;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 1px;
  padding: 3px 8px;
  border-radius: 3px;
  background: rgba(212, 240, 87, 0.1);
  color: var(--accent);
  border: 1px solid rgba(212, 240, 87, 0.3);
}}
.sc-thumb {{
  border-right: 1px solid var(--bg-card-border);
  background: #1a1a1a;
  position: relative;
  overflow: hidden;
}}
.sc-thumb img {{
  width: 100%;
  height: 100%;
  max-height: 480px;
  object-fit: cover;
  object-position: top;
  display: block;
}}
.sc-card-body {{
  padding: 20px 22px;
  color: var(--ink-dim);
  font-size: 14px;
  line-height: 1.55;
}}
.sc-oneliner {{
  background: #14250e;
  color: var(--accent);
  border-left: 3px solid var(--accent);
  font-family: "Charter", Georgia, serif;
  font-style: italic;
  font-size: 14.5px;
  line-height: 1.5;
  padding: 12px 14px;
  margin: 0 0 16px;
  border-radius: 0 3px 3px 0;
}}
.sc-offer-badge {{
  display: inline-block;
  background: rgba(212, 240, 87, 0.13);
  border: 1px solid rgba(212, 240, 87, 0.35);
  color: var(--accent);
  padding: 4px 10px;
  border-radius: 3px;
  font-size: 11.5px;
  font-weight: 700;
  margin: 0 0 12px;
  letter-spacing: 0.3px;
}}
.sc-headline {{
  font-family: "Inter", sans-serif;
  font-weight: 700;
  font-size: 17px;
  line-height: 1.3;
  color: var(--ink);
  margin: 0 0 4px;
}}
.sc-subhead {{
  color: var(--ink-dim);
  font-size: 14px;
  line-height: 1.45;
  margin: 0 0 14px;
}}
.sc-cta-row {{
  display: flex; align-items: baseline; gap: 12px;
  margin: 10px 0;
  flex-wrap: wrap;
}}
.sc-cta-label {{
  font-family: "Inter", sans-serif;
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: 1px;
  text-transform: uppercase;
  color: var(--ink-muted);
  flex-shrink: 0;
  min-width: 110px;
}}
.sc-cta-primary {{
  background: #14250e;
  color: var(--accent);
  padding: 3px 9px;
  border-radius: 3px;
  font-weight: 700;
  font-size: 12.5px;
  border: 1px solid rgba(212, 240, 87, 0.3);
}}
.sc-cta-chips {{ display: flex; flex-wrap: wrap; gap: 5px; flex: 1; }}
.sc-cta-chip {{
  background: rgba(255,255,255,0.04);
  color: var(--ink-dim);
  padding: 2px 7px;
  border-radius: 3px;
  font-size: 11px;
  border: 1px solid var(--bg-card-border);
}}
.sc-stance-row {{
  display: flex; flex-wrap: wrap; gap: 5px;
  margin: 14px 0;
}}
.sc-stance {{
  background: rgba(255,255,255,0.03);
  border: 1px solid var(--bg-card-border);
  border-radius: 3px;
  padding: 3px 8px;
  font-size: 11px;
  display: inline-flex; gap: 4px; align-items: baseline;
}}
.sc-stance-k {{
  color: var(--ink-muted);
  font-weight: 500;
}}
.sc-stance-v {{
  color: var(--accent);
  font-weight: 700;
}}
.sc-section-label {{
  font-family: "Inter", sans-serif;
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: 1px;
  text-transform: uppercase;
  color: var(--accent);
  margin-bottom: 6px;
}}
.sc-positioning {{
  margin: 16px 0;
  padding-top: 14px;
  border-top: 1px solid var(--bg-card-border);
}}
.sc-positioning p {{
  margin: 0;
  font-family: "Charter", Georgia, serif;
  font-size: 14px;
  line-height: 1.55;
  color: var(--ink-dim);
}}
.sc-ww-row {{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  margin: 16px 0;
  padding-top: 14px;
  border-top: 1px solid var(--bg-card-border);
}}
.sc-ww-label {{
  font-family: "Inter", sans-serif;
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: 1px;
  text-transform: uppercase;
  margin-bottom: 6px;
}}
.sc-ww-good {{ color: #74c69d; }}
.sc-ww-bad {{ color: #e85b3a; }}
.sc-ww ul {{
  margin: 0; padding-left: 18px;
  font-size: 13px;
  color: var(--ink-dim);
  line-height: 1.5;
}}
.sc-ww li {{ margin: 4px 0; }}
.sc-design {{
  margin: 12px 0 0;
  border-top: 1px solid var(--bg-card-border);
  padding-top: 12px;
}}
.sc-design summary {{
  cursor: pointer;
  font-family: "Inter", sans-serif;
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: 1px;
  text-transform: uppercase;
  color: var(--accent);
}}
.sc-design ul {{
  margin: 10px 0 0; padding-left: 18px;
  font-size: 13px;
  color: var(--ink-dim);
  line-height: 1.5;
}}
.sc-design li {{ margin: 4px 0; }}
.sc-limitations {{
  margin-top: 40px;
  padding: 22px 24px;
  background: rgba(255, 100, 100, 0.04);
  border: 1px solid rgba(255, 100, 100, 0.2);
  border-radius: 6px;
}}
.sc-limitations .sc-section-label {{
  color: #e85b3a;
  margin-bottom: 10px;
}}
.sc-limitations ul {{
  margin: 0; padding-left: 20px;
  font-size: 13.5px;
  color: var(--ink-dim);
  line-height: 1.55;
  font-family: "Charter", Georgia, serif;
}}
.sc-limitations li {{ margin: 8px 0; }}
.sc-limitations strong {{ color: var(--ink); font-weight: 600; }}

/* ---- methodology footer ---- */
.brief > p:last-child > em {{
  display: block;
  font-size: 12px;
  font-style: italic;
  color: var(--ink-muted);
  border-top: 1px solid var(--ink-faint);
  padding-top: 24px;
  margin-top: 48px;
  line-height: 1.55;
}}

/* ---- print ---- */
@media print {{
  html, body {{ background: var(--bg) !important; -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
  .wrapper {{ max-width: 100%; padding: 0.4in 0.5in; }}
  .hero h1 {{ font-size: 36pt; }}
  .brief h2 {{ font-size: 22pt; page-break-after: avoid; }}
  .brief table, figure.chart, .ref-theme {{ page-break-inside: avoid; }}
  section.reference-board {{ page-break-before: always; }}
  .ref-item img {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
  a {{ color: var(--accent); }}
  pre, code {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
}}
"""


def render(deployment_key, md_text, brand_label, date):
    d = DEPLOYMENTS[deployment_key]

    # Drop the H1 title line from body — we render our own hero
    lines = md_text.splitlines()
    body_start = 0
    for i, line in enumerate(lines):
        if line.startswith("# "):
            body_start = i + 1; break
    body_md = "\n".join(lines[body_start:]).lstrip()

    # Drop the first blockquote (the dating caveat) — we don't need it in the new layout;
    # the methodology footer carries the same point.
    body_md = re.sub(r'^> [^\n]+(\n> [^\n]+)*\n+', '', body_md, count=1)

    html_body = markdown.markdown(
        body_md,
        extensions=["extra", "tables", "smarty", "sane_lists"],
        output_format="html5",
    )

    # Inject section eyebrows
    html_body = add_section_eyebrows(html_body)

    # Build charts (live from db)
    db_path = Path(d["db_path"]).resolve()
    conn = sqlite3.connect(db_path); conn.row_factory = sqlite3.Row
    try:
        quadrant = chart_quadrant(conn, d)
        volume = chart_brand_volume(conn, d)
        dna = chart_creative_dna(conn, d)
        appeal = chart_appeal_split(conn, d)
        refboard = reference_board(conn, d)
        sitecontent = site_content_section(conn, d)
        topcreatives = top_creatives_by_reach(conn, d)
    finally:
        conn.close()

    # Inject charts after specific section anchors
    def inject_after_section(html, anchor_text, snippet):
        pat = re.compile(r'(<h2[^>]*>[^<]*' + re.escape(anchor_text) + r'[^<]*</h2>)', re.IGNORECASE)
        m = pat.search(html)
        if not m: return html
        # Insert before the next eyebrow OR end of doc
        eb_pat = re.compile(r'<div class="eyebrow">')
        next_eb = eb_pat.search(html, m.end())
        insert_at = next_eb.start() if next_eb else len(html)
        return html[:insert_at] + snippet + html[insert_at:]

    # Place visuals at the most narratively-relevant section anchors
    if quadrant:
        html_body = inject_after_section(html_body, "category right now", quadrant)
    if volume:
        html_body = inject_after_section(html_body, "category right now", volume)
    if dna:
        html_body = inject_after_section(html_body, "competitive tone", dna)
    if appeal:
        html_body = inject_after_section(html_body, "competitive tone", appeal)

    # Apply inline highlighting (brand names + curated phrases)
    html_body = apply_inline_highlights(html_body, d["brand_labels"], d.get("highlight_phrases", []))

    # Site Content Deep-Dive + Top served ads + Reference board before methodology footer
    extras = ""
    if sitecontent:
        extras += sitecontent
    if topcreatives:
        extras += topcreatives
    if refboard:
        extras += refboard
    if extras:
        meth = re.search(r"<hr\s*/?>\s*<p><em>", html_body)
        if meth:
            html_body = html_body[:meth.start()] + extras + html_body[meth.start():]
        else:
            html_body = html_body + extras

    hero = d.get("hero", {})
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{brand_label} — Strategist's Read</title>
  <style>{CSS}</style>
</head>
<body>
  <div class="wrapper">
    <div class="brief-band">
      <span>{d.get("header_eyebrow", "Strategist Read")}</span>
      <span class="meta-right">{d.get("header_meta", "Confidential")} · {date}</span>
    </div>
    <header class="hero">
      <h1>{hero.get("lead_in", "")} <em>{hero.get("italic", brand_label)}</em>.</h1>
      <p class="subtitle">{hero.get("subtitle", "")}</p>
    </header>
    <article class="brief">
      {html_body}
    </article>
  </div>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("deployment", choices=list(DEPLOYMENTS.keys()))
    ap.add_argument("--md", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--brand", required=True)
    ap.add_argument("--date", required=True)
    args = ap.parse_args()

    html = render(args.deployment, args.md.read_text(), args.brand, args.date)
    args.out.write_text(html)
    print(f"wrote {args.out} ({len(html):,} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
