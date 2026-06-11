"""Cross-competitor briefing synthesis.

Pulls the last N days of observations + ads + offers, hands a structured corpus
to Claude, and asks for a 'what changed and what should we do' briefing.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import anthropic

from ..config import load_settings
from ..storage import connect, popularity_score, record_briefing


class MissingApiKey(RuntimeError):
    pass


def _require_key() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise MissingApiKey(
            "ANTHROPIC_API_KEY not set — add it to .env. "
            "All LLM-backed commands (brief, agent, analyze-creative, extract-offers, cluster-hooks, whitespace) need it."
        )


BRIEFING_SYSTEM = """You are a senior marketing strategist writing a competitive intelligence briefing.

Audience: brand marketing leadership for a consumer/DTC brand.

You will receive a JSON corpus of:
- new ads launched per competitor (last N days) — the "what's just changed" view
- top served ads per competitor (popularity-ranked, lifetime) — the "what's actually working" view; ranked by Meta Ad Library's own sort × run duration. Use this to identify the ads carrying each competitor's spend, regardless of when they launched. Note: this is a proxy (Meta does not expose raw impressions for commercial US ads), intra-brand only.
- offer/promo changes detected on competitor sites
- on-site messaging changes (hero/banner/popup deltas)
- Amazon brand store activity (page counts, image counts, hero text) for brands that sell on Amazon
- homepage promotional posture per brand (hero headline, subhead, primary CTA, offer claim, expiration, urgency cues) — captured from the brand's homepage hero/banner area

Write a markdown briefing with these sections — and ONLY these sections:

# Competitive Briefing — <date range>

## TL;DR
3-5 bullets of the most important moves. Lead with what's *unusual* or actionable, not what's routine.

## What changed by competitor
For each competitor with material activity, a short paragraph (3-4 sentences max). Cite specific ad ids or offer values.

## Patterns across the set
1-2 paragraphs of cross-competitor synthesis: themes 2+ brands moved on, format shifts, price-bracket trends.

## Whitespace / opportunities
A bulleted list of angles, formats, or offers NO ONE in the set is using — phrased as a testable hypothesis we could run.

## Recommended actions (next 7 days)
A prioritized list (max 5) of concrete things to do, each tagged [low effort] / [med] / [high]. Be specific — name the channel and the move.

Rules:
- No filler. No "in conclusion." No restating the prompt.
- If there's no material activity, say so plainly in one line and stop.
- Distinguish facts (in corpus) from inference. Mark inferences as "(inferred)".
- Never invent ad ids, prices, or competitor names not in the corpus.
"""


def build_corpus(days: int = 7) -> dict[str, Any]:
    """Assemble the last `days` of activity from the local store."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    corpus: dict[str, Any] = {"window_days": days, "competitors": {}}
    with connect() as conn:
        comps = conn.execute("SELECT id, name, vertical FROM competitors").fetchall()
        for c in comps:
            entry: dict[str, Any] = {
                "name": c["name"],
                "vertical": c["vertical"],
                "new_ads": [],
                "top_ads": [],
                "offers": [],
                "site_changes": [],
                "brand_store_changes": [],
                "homepage_creatives": [],
                "homepage_promos": [],
            }
            for ad in conn.execute(
                "SELECT ad_archive_id, page_name, first_seen, body_text, cta_type, link_url, "
                "publisher_platforms FROM ads "
                "WHERE competitor_id=? AND first_seen >= ? ORDER BY first_seen DESC LIMIT 50",
                (c["id"], since),
            ).fetchall():
                entry["new_ads"].append(dict(ad))

            # top_ads: lifetime view, ranked by popularity_score proxy. NOT
            # filtered by the window — these are the ads we believe are carrying
            # the brand's spend right now, whenever they launched.
            max_rank = conn.execute(
                "SELECT MAX(serp_position_rank) FROM ads "
                "WHERE competitor_id=? AND serp_position_rank IS NOT NULL",
                (c["id"],),
            ).fetchone()[0] or 0
            ad_rows = conn.execute(
                "SELECT ad_archive_id, first_seen, last_seen, start_date, active, "
                "body_text, cta_type, serp_position_rank "
                "FROM ads WHERE competitor_id=?",
                (c["id"],),
            ).fetchall()
            scored = []
            for r in ad_rows:
                d = dict(r)
                d["popularity_score"] = round(popularity_score(
                    d.get("serp_position_rank"),
                    d.get("start_date"),
                    d.get("last_seen"),
                    d.get("active") or 0,
                    max_rank,
                ), 3)
                if d.get("start_date") and d.get("last_seen"):
                    try:
                        s = datetime.fromisoformat(d["start_date"][:10])
                        e = datetime.fromisoformat(d["last_seen"][:10])
                        d["run_days"] = max((e - s).days, 0)
                    except (ValueError, TypeError):
                        d["run_days"] = 0
                else:
                    d["run_days"] = 0
                scored.append(d)
            scored.sort(key=lambda d: d["popularity_score"], reverse=True)
            # Trim body_text to keep the corpus compact; the LLM needs enough
            # to recognize the ad, not the full body.
            for d in scored[:5]:
                body = (d.get("body_text") or "").replace("\n", " ")[:240]
                entry["top_ads"].append({
                    "ad_archive_id": d["ad_archive_id"],
                    "serp_rank": (d.get("serp_position_rank") + 1) if d.get("serp_position_rank") is not None else None,
                    "run_days": d["run_days"],
                    "active": bool(d.get("active")),
                    "popularity_score": d["popularity_score"],
                    "body_snippet": body,
                    "cta_type": d.get("cta_type"),
                })
            for o in conn.execute(
                "SELECT observed_at, kind, value, threshold, description, source "
                "FROM offers WHERE competitor_id=? AND observed_at >= ? ORDER BY observed_at DESC LIMIT 50",
                (c["id"], since),
            ).fetchall():
                entry["offers"].append(dict(o))
            # site_changes: every changed observation EXCEPT brand-store (broken out separately).
            for obs in conn.execute(
                "SELECT observations.id, observations.observed_at, observations.change_summary, "
                "observations.severity, sources.type, sources.source_key "
                "FROM observations JOIN sources ON sources.id=observations.source_id "
                "WHERE sources.competitor_id=? AND sources.type != 'amazon_brand_store' "
                "AND observations.changed=1 AND observations.observed_at >= ? "
                "ORDER BY observations.severity DESC, observations.observed_at DESC LIMIT 25",
                (c["id"], since),
            ).fetchall():
                entry["site_changes"].append(dict(obs))
            # brand_store_changes: pull parsed_json so we can surface page counts + hero text.
            for bs in conn.execute(
                "SELECT observations.observed_at, observations.changed, observations.severity, "
                "observations.parsed_json, sources.source_key "
                "FROM observations JOIN sources ON sources.id=observations.source_id "
                "WHERE sources.competitor_id=? AND sources.type='amazon_brand_store' "
                "AND observations.observed_at >= ? "
                "ORDER BY observations.observed_at DESC LIMIT 10",
                (c["id"], since),
            ).fetchall():
                bs_row = dict(bs)
                try:
                    p = json.loads(bs_row.pop("parsed_json") or "{}")
                except Exception:
                    p = {}
                bs_row["store_url"] = p.get("store_url")
                bs_row["pages_captured"] = (p.get("totals") or {}).get("pages_captured", 0)
                bs_row["image_count"] = (p.get("totals") or {}).get("images", 0)
                # Pluck top-3 page titles for at-a-glance content
                bs_row["sample_titles"] = [
                    (pg.get("title") or "")[:80] for pg in (p.get("pages") or [])[:3]
                ]
                entry["brand_store_changes"].append(bs_row)
            # homepage_creatives: image_count + analyzed_count per website observation in window
            for obs in conn.execute(
                "SELECT observations.id, observations.observed_at, observations.parsed_json "
                "FROM observations JOIN sources ON sources.id=observations.source_id "
                "WHERE sources.competitor_id=? AND sources.type='website' "
                "AND observations.observed_at >= ? "
                "ORDER BY observations.observed_at DESC LIMIT 10",
                (c["id"], since),
            ).fetchall():
                try:
                    pj = json.loads(obs["parsed_json"] or "{}")
                except Exception:
                    pj = {}
                analyzed = conn.execute(
                    "SELECT COUNT(*) AS n FROM creatives "
                    "WHERE asset_type='homepage_image' AND competitor_id=? AND analyzed_at IS NOT NULL",
                    (c["id"],),
                ).fetchone()["n"]
                entry["homepage_creatives"].append({
                    "observed_at": obs["observed_at"],
                    "image_count": pj.get("image_count", 0),
                    "analyzed_count": analyzed,
                    "hero_text": (pj.get("meta") or {}).get("title"),
                })
            # homepage_promos: structured promotional posture (hero/banner) per observation
            for hp in conn.execute(
                "SELECT observed_at, headline, subhead, primary_cta_text, "
                "offer_claim, offer_value, offer_kind, expiration, "
                "channel_callouts, urgency_cues, confidence, cache_hit "
                "FROM homepage_promos WHERE competitor_id=? AND observed_at >= ? "
                "ORDER BY observed_at DESC LIMIT 10",
                (c["id"], since),
            ).fetchall():
                hp_row = dict(hp)
                for k in ("channel_callouts", "urgency_cues"):
                    try:
                        hp_row[k] = json.loads(hp_row[k] or "[]")
                    except Exception:
                        hp_row[k] = []
                entry["homepage_promos"].append(hp_row)
            corpus["competitors"][c["id"]] = entry
    return corpus


def render_deterministic_briefing(corpus: dict[str, Any]) -> str:
    """Template-based briefing — no LLM. Useful when ANTHROPIC_API_KEY is unset.
    Output is structured and factual; no recommendations or synthesis."""
    days = corpus.get("window_days", 7)
    comps = corpus.get("competitors", {})
    total_ads = sum(len(v["new_ads"]) for v in comps.values())
    total_offers = sum(len(v["offers"]) for v in comps.values())
    total_site = sum(len(v["site_changes"]) for v in comps.values())
    total_brand_store = sum(len(v.get("brand_store_changes") or []) for v in comps.values())
    total_hp_promos = sum(len(v.get("homepage_promos") or []) for v in comps.values())
    active_comps = [
        cid for cid, v in comps.items()
        if v["new_ads"] or v["offers"] or v["site_changes"]
        or v.get("brand_store_changes") or v.get("homepage_promos")
    ]

    lines: list[str] = []
    lines.append(f"# Competitive Briefing — last {days}d (deterministic, no LLM)\n")
    lines.append("## TL;DR\n")
    lines.append(f"- {total_ads} new ads detected across {len(active_comps)} active competitor(s)")
    lines.append(f"- {total_offers} offer/promo signals captured")
    lines.append(f"- {total_site} on-site changes flagged")
    if total_brand_store:
        lines.append(f"- {total_brand_store} Amazon brand-store snapshot(s) captured")
    if total_hp_promos:
        lines.append(f"- {total_hp_promos} homepage promotional-posture record(s) captured")
    lines.append("")

    if not active_comps:
        lines.append("_No material competitive activity in the window. Either the set is quiet or sources haven't been ingested recently._\n")
        return "\n".join(lines)

    lines.append("## Per-competitor activity\n")
    for cid in sorted(comps.keys()):
        v = comps[cid]
        if not (v["new_ads"] or v["offers"] or v["site_changes"]):
            continue
        lines.append(f"### {v['name']} (`{cid}`)")
        if v["new_ads"]:
            lines.append(f"\n**{len(v['new_ads'])} new ad(s)**")
            for ad in v["new_ads"][:8]:
                body = (ad.get("body_text") or "").replace("\n", " ").strip()[:140]
                ctas = f" · CTA: `{ad['cta_type']}`" if ad.get("cta_type") else ""
                lines.append(f"- `{ad['ad_archive_id']}` — {body}{ctas}")
            if len(v["new_ads"]) > 8:
                lines.append(f"- _… and {len(v['new_ads']) - 8} more_")
        if v["offers"]:
            lines.append(f"\n**{len(v['offers'])} offer(s) detected**")
            for o in v["offers"][:8]:
                thr = f" (threshold: {o['threshold']})" if o.get("threshold") else ""
                lines.append(f"- {o.get('kind','?')}: {o.get('value','')}{thr} — {(o.get('description') or '')[:120]}")
        if v["site_changes"]:
            lines.append(f"\n**{len(v['site_changes'])} site change(s)** (top 5 by severity)")
            for s in v["site_changes"][:5]:
                lines.append(
                    f"- [{s.get('type','?')}] severity={s.get('severity',0):.2f} — "
                    f"{(s.get('change_summary') or '')[:100]} ({s.get('observed_at','')[:10]})"
                )
        if v.get("brand_store_changes"):
            bs_list = v["brand_store_changes"]
            lines.append(f"\n**{len(bs_list)} Amazon brand-store snapshot(s)**")
            for bs in bs_list[:3]:
                titles = ", ".join(t for t in bs.get("sample_titles") or [] if t)
                lines.append(
                    f"- {bs.get('observed_at','')[:10]} — "
                    f"{bs.get('pages_captured',0)} page(s), {bs.get('image_count',0)} image(s)"
                    f"{' · ' + titles if titles else ''}"
                )
        if v.get("homepage_promos"):
            hp_list = v["homepage_promos"]
            # Surface the most recent unique promotional posture
            latest = hp_list[0]
            lines.append("\n**Homepage promotional posture (latest)**")
            head = latest.get("headline") or "_(no headline detected)_"
            cta = latest.get("primary_cta_text")
            claim = latest.get("offer_claim")
            exp = latest.get("expiration")
            extras: list[str] = []
            if cta:
                extras.append(f"CTA `{cta}`")
            if claim:
                extras.append(f"offer: {claim}")
            if exp:
                extras.append(f"expires: {exp}")
            tail = " · ".join(extras)
            lines.append(f"- {head}{(' — ' + tail) if tail else ''}")
            cached = sum(1 for hp in hp_list if hp.get("cache_hit"))
            if len(hp_list) > 1:
                lines.append(f"- _{len(hp_list)} observations in window, {cached} from hero-hash cache_")
        lines.append("")

    # Light cross-set patterns (counts only — no inference).
    cta_counts: dict[str, int] = {}
    for v in comps.values():
        for ad in v["new_ads"]:
            c = ad.get("cta_type") or "(none)"
            cta_counts[c] = cta_counts.get(c, 0) + 1
    if cta_counts:
        lines.append("## CTA mix across new ads\n")
        for cta, n in sorted(cta_counts.items(), key=lambda x: -x[1]):
            lines.append(f"- `{cta}`: {n}")
        lines.append("")

    offer_kinds: dict[str, int] = {}
    for v in comps.values():
        for o in v["offers"]:
            k = o.get("kind") or "(unknown)"
            offer_kinds[k] = offer_kinds.get(k, 0) + 1
    if offer_kinds:
        lines.append("## Offer-kind mix across set\n")
        for k, n in sorted(offer_kinds.items(), key=lambda x: -x[1]):
            lines.append(f"- `{k}`: {n}")
        lines.append("")

    lines.append("---")
    lines.append("_Deterministic briefing — no LLM synthesis. For narrative + recommendations, run `intel brief` with ANTHROPIC_API_KEY set._")
    return "\n".join(lines)


def generate_briefing(days: int = 7, *, model: str | None = None, scope: str | None = None, use_llm: bool = True) -> dict[str, Any]:
    """Build corpus + (optionally) ask Claude for the briefing + persist it.

    When `use_llm=False` (or ANTHROPIC_API_KEY is missing), a deterministic
    templated briefing is rendered instead — covers what changed factually,
    no recommendations or inferential synthesis.
    """
    settings = load_settings()
    model = model or settings.reasoning_model
    corpus = build_corpus(days)

    if not use_llm or not os.environ.get("ANTHROPIC_API_KEY"):
        end = datetime.now(timezone.utc).date().isoformat()
        start = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
        title = f"Competitive Briefing (no-LLM) — {start} to {end}"
        body = render_deterministic_briefing(corpus)
        with connect() as conn:
            bid = record_briefing(conn, scope or f"window_{days}d_no_llm", title, body, sources=list(corpus["competitors"].keys()))
        return {"briefing_id": bid, "title": title, "body_md": body, "corpus": corpus}

    # Empty-set short-circuit.
    has_activity = any(
        v["new_ads"] or v["offers"] or v["site_changes"]
        or v.get("brand_store_changes") or v.get("homepage_promos")
        for v in corpus["competitors"].values()
    )
    if not has_activity:
        body = f"No material competitive activity in the last {days} days."
        title = f"Briefing — quiet window ({days}d)"
        with connect() as conn:
            bid = record_briefing(conn, scope or f"window_{days}d", title, body, sources=[])
        return {"briefing_id": bid, "title": title, "body_md": body, "corpus": corpus}

    end = datetime.now(timezone.utc).date().isoformat()
    start = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    title = f"Competitive Briefing — {start} to {end}"

    _require_key()
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=model,
        max_tokens=4000,
        system=BRIEFING_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Date range: {start} to {end}\n\n"
                    f"Corpus (JSON):\n{json.dumps(corpus, default=str)}\n\n"
                    "Write the briefing now."
                ),
            }
        ],
    )
    body = "".join(b.text for b in msg.content if b.type == "text").strip()
    with connect() as conn:
        bid = record_briefing(
            conn,
            scope=scope or f"window_{days}d",
            title=title,
            body_md=body,
            sources=list(corpus["competitors"].keys()),
        )
    return {"briefing_id": bid, "title": title, "body_md": body, "corpus": corpus}
