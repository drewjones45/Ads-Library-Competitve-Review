"""Per-brand creative readouts + cross-set attribute comparison.

The flow:
- `pull_analyzed_creatives()` — load every analyzed creative joined with its ad,
  filtered by competitor / window. The unit of analysis is one image.
- `per_brand_readout(competitor_id)` — distribution of every attribute for ONE brand,
  plus the "net new" subset (creatives whose parent ads first_seen in the window).
- `cross_set_comparison()` — for each attribute, per-brand share matrix, the
  set-wide popularity, what's distinctive per brand, and what's whitespace.

All renderers also return a markdown body so the same function can power the CLI
output and be embedded in briefings.
"""
from __future__ import annotations

import json
import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from ..storage import connect


# Attributes we tally as Counters across creatives.
# Each tuple: (json key path, value extractor) — extractor returns either str
# or list[str] per creative.
_SCALAR_ATTRS = [
    "photography_style",
    "production_style",
    "product_emphasis",
    "hook_style",
    "emotional_vs_rational",
    "aspect_ratio_guess",
    # Phase A1 additions:
    "background_color",
    "model_gender",
    "product_in_use",
    "creative_context",
    "product_grouping",
]
_BOOL_ATTRS = [
    "logo_visible",
    # Phase A1 additions:
    "before_after_present",
    "hero_banner_present",
    "category_nav_visible",
    "shoppable_imagery",
]
_LIST_ATTRS = [
    "products_visible", "key_features", "value_props", "seasonal_tags",
    # Phase A1 additions:
    "certifications_visible",
    "awards_or_rankings",
]
_NESTED_BOOLS = [  # (label, path)
    ("text_overlay.present", ("text_overlay", "present")),
    ("urgency_cues.present", ("urgency_cues", "present")),
    ("casting.people_visible", ("casting", "people_visible")),
]


@dataclass
class CreativeRecord:
    competitor_id: str
    competitor_name: str
    ad_id: int
    ad_archive_id: str
    asset_path: str
    first_seen: str
    analyzed_at: str
    summary: str | None
    analysis: dict


@dataclass
class AttributeTallies:
    """Counts per attribute. Each is a Counter[str → int]."""
    scalar: dict[str, Counter] = field(default_factory=dict)
    boolean: dict[str, Counter] = field(default_factory=dict)  # value: 'true'/'false'/'none'
    listed: dict[str, Counter] = field(default_factory=dict)
    n_total: int = 0  # number of creatives tallied

    def share(self, attr: str, value: str) -> float:
        """% of creatives where the given attribute has the given value (or
        the list contains the value, for list attrs)."""
        if self.n_total == 0:
            return 0.0
        c = self.scalar.get(attr) or self.boolean.get(attr) or self.listed.get(attr)
        if c is None:
            return 0.0
        return c.get(value, 0) / self.n_total


def pull_analyzed_creatives(
    *,
    competitor_id: str | None = None,
    window_days: int | None = None,
) -> list[CreativeRecord]:
    """Load every analyzed creative. LEFT JOIN ads so standalone (brand-store /
    website) creatives are included alongside paid-ad creatives. The competitor
    is resolved as COALESCE(ads.competitor_id, creatives.competitor_id).

    window_days filters by ad first_seen for paid-ad creatives; standalone
    creatives use creatives.analyzed_at as the recency proxy (they have no
    first_seen). When window_days is None, all analyzed creatives are returned.
    """
    q = (
        "SELECT COALESCE(a.competitor_id, cr.competitor_id) AS competitor_id, "
        "       comp.name AS comp_name, a.id AS ad_id, "
        "       COALESCE(a.ad_archive_id, '') AS ad_archive_id, "
        "       COALESCE(a.first_seen, cr.analyzed_at) AS first_seen, "
        "       cr.asset_path, cr.analyzed_at, cr.analysis_json "
        "FROM creatives cr "
        "LEFT JOIN ads a ON a.id = cr.ad_id "
        "JOIN competitors comp ON comp.id = COALESCE(a.competitor_id, cr.competitor_id) "
        "WHERE cr.analysis_json IS NOT NULL"
    )
    params: list = []
    if competitor_id:
        q += " AND COALESCE(a.competitor_id, cr.competitor_id) = ?"
        params.append(competitor_id)
    if window_days is not None:
        since = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
        q += " AND COALESCE(a.first_seen, cr.analyzed_at) >= ?"
        params.append(since)
    with connect() as conn:
        rows = conn.execute(q, params).fetchall()
    out: list[CreativeRecord] = []
    for r in rows:
        try:
            analysis = json.loads(r["analysis_json"])
        except Exception:
            continue
        out.append(CreativeRecord(
            competitor_id=r["competitor_id"],
            competitor_name=r["comp_name"],
            ad_id=r["ad_id"] or 0,
            ad_archive_id=r["ad_archive_id"],
            asset_path=r["asset_path"],
            first_seen=r["first_seen"],
            analyzed_at=r["analyzed_at"],
            summary=analysis.get("summary_one_line"),
            analysis=analysis,
        ))
    return out


def _tally(records: Iterable[CreativeRecord]) -> AttributeTallies:
    t = AttributeTallies()
    for rec in records:
        a = rec.analysis
        t.n_total += 1
        for attr in _SCALAR_ATTRS:
            v = a.get(attr)
            if isinstance(v, str) and v:
                t.scalar.setdefault(attr, Counter())[v] += 1
        for attr in _BOOL_ATTRS:
            v = a.get(attr)
            label = "true" if v is True else "false" if v is False else "unknown"
            t.boolean.setdefault(attr, Counter())[label] += 1
        for label, path in _NESTED_BOOLS:
            cur: Any = a
            for k in path:
                cur = cur.get(k) if isinstance(cur, dict) else None
                if cur is None:
                    break
            lbl = "true" if cur is True else "false" if cur is False else "unknown"
            t.boolean.setdefault(label, Counter())[lbl] += 1
        for attr in _LIST_ATTRS:
            v = a.get(attr) or []
            if isinstance(v, list):
                for item in v:
                    if isinstance(item, str) and item:
                        t.listed.setdefault(attr, Counter())[item.lower()] += 1
    return t


def _topn(counter: Counter, n: int = 6) -> list[tuple[str, int]]:
    return counter.most_common(n)


def per_brand_readout(competitor_id: str, *, window_days: int = 7) -> dict[str, Any]:
    """Return a dict + markdown body summarizing one brand's creative posture."""
    all_recs = pull_analyzed_creatives(competitor_id=competitor_id)
    net_new = pull_analyzed_creatives(competitor_id=competitor_id, window_days=window_days)
    if not all_recs:
        return {
            "competitor_id": competitor_id,
            "total_analyzed": 0,
            "net_new_count": 0,
            "tallies": None,
            "body_md": (
                f"# Creative readout — `{competitor_id}`\n\n"
                "_No analyzed creatives yet. Run `intel analyze-creatives "
                f"--competitor {competitor_id}` first._"
            ),
        }
    tallies = _tally(all_recs)
    new_tallies = _tally(net_new) if net_new else None
    brand_name = all_recs[0].competitor_name

    lines: list[str] = []
    lines.append(f"# Creative readout — {brand_name} (`{competitor_id}`)\n")
    lines.append(f"_Window: ads first_seen in last {window_days} day(s)._\n")
    lines.append(f"- **Total analyzed creatives:** {len(all_recs)}")
    lines.append(f"- **Net new (in window):** {len(net_new)}\n")

    if net_new:
        lines.append("## Net-new creatives in window\n")
        for r in net_new[:10]:
            sm = (r.summary or "(no summary)")[:140]
            lines.append(f"- ad `{r.ad_archive_id}` — {sm}")
            lines.append(f"  - `{r.asset_path}`")
        if len(net_new) > 10:
            lines.append(f"- _… and {len(net_new) - 10} more_")
        lines.append("")

    lines.append("## Attribute distribution (all analyzed)\n")
    _render_tallies_section(lines, tallies, indent="")

    if new_tallies and new_tallies.n_total >= 3:
        lines.append("\n## Distribution among net-new only\n")
        _render_tallies_section(lines, new_tallies, indent="")

    lines.append("\n## Top dominant colors (pooled across all creatives)\n")
    color_counter: Counter = Counter()
    for rec in all_recs:
        for c in rec.analysis.get("dominant_colors_hex") or []:
            if isinstance(c, str):
                color_counter[c.lower()] += 1
    for hex_code, n in color_counter.most_common(6):
        lines.append(f"- `{hex_code}` — {n} creative(s)")

    return {
        "competitor_id": competitor_id,
        "competitor_name": brand_name,
        "total_analyzed": len(all_recs),
        "net_new_count": len(net_new),
        "tallies": tallies,
        "tallies_new": new_tallies,
        "body_md": "\n".join(lines),
    }


def _render_tallies_section(lines: list[str], t: AttributeTallies, *, indent: str = "") -> None:
    n = t.n_total
    for attr in _SCALAR_ATTRS:
        counter = t.scalar.get(attr)
        if not counter:
            continue
        lines.append(f"{indent}**{attr}** _(n={n})_")
        for val, c in _topn(counter, 6):
            lines.append(f"{indent}- `{val}`: {c}  ({c/n:.0%})")
        lines.append("")
    for attr in _BOOL_ATTRS + [lbl for lbl, _ in _NESTED_BOOLS]:
        counter = t.boolean.get(attr)
        if not counter:
            continue
        lines.append(f"{indent}**{attr}** _(true / false / unknown)_")
        for k in ("true", "false", "unknown"):
            if counter.get(k):
                lines.append(f"{indent}- `{k}`: {counter[k]}  ({counter[k]/n:.0%})")
        lines.append("")
    for attr in _LIST_ATTRS:
        counter = t.listed.get(attr)
        if not counter:
            continue
        lines.append(f"{indent}**{attr}** _(top values across {n} creatives)_")
        for val, c in _topn(counter, 8):
            lines.append(f"{indent}- `{val}`: {c}")
        lines.append("")


def cross_set_comparison(*, window_days: int = 30) -> dict[str, Any]:
    """Build the cross-competitor view:
      - per-attribute set-wide popularity
      - per-brand share (rows = competitors, cols = top values)
      - distinctiveness: which value is over-indexed for each brand
      - whitespace: values present in *no* brand
    """
    all_recs = pull_analyzed_creatives(window_days=window_days)
    if not all_recs:
        return {
            "n_creatives": 0,
            "body_md": (
                f"# Cross-competitor creative comparison (last {window_days}d)\n\n"
                "_No analyzed creatives in the window — run "
                "`intel analyze-creatives` first._"
            ),
        }

    # Tally per competitor and overall.
    by_comp: dict[str, list[CreativeRecord]] = {}
    for r in all_recs:
        by_comp.setdefault(r.competitor_id, []).append(r)
    comp_tallies: dict[str, AttributeTallies] = {cid: _tally(recs) for cid, recs in by_comp.items()}
    set_tally = _tally(all_recs)

    body: list[str] = []
    body.append(f"# Cross-competitor creative comparison\n")
    body.append(f"_Window: ads first_seen in last {window_days}d · "
                f"{len(all_recs)} analyzed creatives across {len(by_comp)} brands._\n")

    body.append("## Brand sample sizes\n")
    for cid, recs in sorted(by_comp.items(), key=lambda kv: -len(kv[1])):
        body.append(f"- `{cid}`: {len(recs)} analyzed creative(s)")
    body.append("")

    # For each attribute, show set-wide ranking + per-brand share matrix.
    body.append("## Set-wide attribute popularity\n")
    for attr in _SCALAR_ATTRS:
        counter = set_tally.scalar.get(attr)
        if not counter:
            continue
        body.append(f"### {attr}\n")
        top_vals = [v for v, _ in counter.most_common(5)]
        # Header row
        header = "| brand | " + " | ".join(f"`{v}`" for v in top_vals) + " | n |"
        body.append(header)
        body.append("|" + "|".join(["---"] * (len(top_vals) + 2)) + "|")
        for cid in sorted(by_comp.keys()):
            t = comp_tallies[cid]
            n = t.n_total
            cells = []
            for v in top_vals:
                share = t.scalar.get(attr, Counter()).get(v, 0) / n if n else 0
                cells.append(f"{share:.0%}" if n else "—")
            body.append(f"| `{cid}` | " + " | ".join(cells) + f" | {n} |")
        # Set totals row
        set_n = set_tally.n_total
        set_row = ["**set**"] + [
            f"**{set_tally.scalar.get(attr, Counter()).get(v, 0) / set_n:.0%}**"
            for v in top_vals
        ] + [f"**{set_n}**"]
        body.append("| " + " | ".join(set_row) + " |")
        body.append("")

    # List attributes — same shape but values from the listed counter.
    for attr in _LIST_ATTRS:
        counter = set_tally.listed.get(attr)
        if not counter:
            continue
        body.append(f"### {attr} (top values, % of creatives containing)\n")
        top_vals = [v for v, _ in counter.most_common(7)]
        header = "| brand | " + " | ".join(f"`{v}`" for v in top_vals) + " | n |"
        body.append(header)
        body.append("|" + "|".join(["---"] * (len(top_vals) + 2)) + "|")
        for cid in sorted(by_comp.keys()):
            t = comp_tallies[cid]
            n = t.n_total
            cells = []
            for v in top_vals:
                share = t.listed.get(attr, Counter()).get(v, 0) / n if n else 0
                cells.append(f"{share:.0%}" if n else "—")
            body.append(f"| `{cid}` | " + " | ".join(cells) + f" | {n} |")
        set_n = set_tally.n_total
        set_row = ["**set**"] + [
            f"**{set_tally.listed.get(attr, Counter()).get(v, 0) / set_n:.0%}**"
            for v in top_vals
        ] + [f"**{set_n}**"]
        body.append("| " + " | ".join(set_row) + " |")
        body.append("")

    # Distinctiveness: for each brand, find the attribute value where the brand's
    # share most exceeds the set average. Surfaces "what's uniquely Bob's".
    body.append("## Distinctiveness — where each brand over-indexes vs the set\n")
    distinct_rows = _distinctiveness(comp_tallies, set_tally)
    if distinct_rows:
        body.append("| brand | attribute | value | brand share | set share | delta |")
        body.append("|---|---|---|---|---|---|")
        for row in distinct_rows:
            body.append(
                f"| `{row['brand']}` | {row['attribute']} | `{row['value']}` "
                f"| {row['brand_share']:.0%} | {row['set_share']:.0%} | +{row['delta']:.0%} |"
            )
        body.append("")

    # Whitespace — top values present in the set but NOT used by some brand
    # (highlight ‘the only one not playing here’).
    body.append("## Per-brand whitespace — top set values this brand has 0% of\n")
    for cid in sorted(by_comp.keys()):
        gaps = _whitespace_for_brand(cid, comp_tallies, set_tally)
        if not gaps:
            continue
        body.append(f"**`{cid}`** — present in the set, absent for this brand:")
        for g in gaps[:6]:
            body.append(f"  - {g['attribute']} = `{g['value']}` (used by {g['set_share']:.0%} of set)")
        body.append("")

    return {
        "n_creatives": len(all_recs),
        "n_brands": len(by_comp),
        "set_tally": set_tally,
        "comp_tallies": comp_tallies,
        "body_md": "\n".join(body),
    }


def _distinctiveness(
    comp_tallies: dict[str, AttributeTallies],
    set_tally: AttributeTallies,
    *,
    min_delta: float = 0.20,
    min_brand_share: float = 0.30,
) -> list[dict[str, Any]]:
    """For each brand, find the (attribute, value) where brand share most
    exceeds set share. Threshold: at least +20pp delta AND ≥30% within brand."""
    out: list[dict[str, Any]] = []
    set_n = set_tally.n_total
    if set_n == 0:
        return out
    for cid, t in comp_tallies.items():
        if t.n_total < 3:
            continue
        best: dict[str, Any] | None = None
        for attr_kind, attr_list in (("scalar", _SCALAR_ATTRS), ("listed", _LIST_ATTRS)):
            counters = t.scalar if attr_kind == "scalar" else t.listed
            set_counters = set_tally.scalar if attr_kind == "scalar" else set_tally.listed
            for attr in attr_list:
                bc = counters.get(attr) or Counter()
                sc = set_counters.get(attr) or Counter()
                for value, count in bc.items():
                    brand_share = count / t.n_total
                    set_share = sc.get(value, 0) / set_n
                    delta = brand_share - set_share
                    if brand_share < min_brand_share or delta < min_delta:
                        continue
                    if best is None or delta > best["delta"]:
                        best = {
                            "brand": cid, "attribute": attr, "value": value,
                            "brand_share": brand_share, "set_share": set_share, "delta": delta,
                        }
        if best:
            out.append(best)
    out.sort(key=lambda r: -r["delta"])
    return out


def _whitespace_for_brand(
    cid: str,
    comp_tallies: dict[str, AttributeTallies],
    set_tally: AttributeTallies,
    *,
    min_set_share: float = 0.20,
) -> list[dict[str, Any]]:
    """Values that ≥20% of the set uses but this brand uses 0%."""
    gaps: list[dict[str, Any]] = []
    t = comp_tallies[cid]
    set_n = set_tally.n_total
    if set_n == 0:
        return gaps
    for attr_kind, attr_list in (("scalar", _SCALAR_ATTRS), ("listed", _LIST_ATTRS)):
        brand_counters = t.scalar if attr_kind == "scalar" else t.listed
        set_counters = set_tally.scalar if attr_kind == "scalar" else set_tally.listed
        for attr in attr_list:
            bc = brand_counters.get(attr) or Counter()
            sc = set_counters.get(attr) or Counter()
            for value, scount in sc.items():
                set_share = scount / set_n
                if set_share < min_set_share:
                    continue
                if bc.get(value, 0) == 0:
                    gaps.append({
                        "attribute": attr, "value": value, "set_share": set_share,
                    })
    gaps.sort(key=lambda g: -g["set_share"])
    return gaps
