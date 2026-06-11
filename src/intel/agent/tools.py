"""Claude tool definitions — the agent's hand.

Each tool maps a Claude `input_schema` to a Python implementation in `dispatch`.
Tools are deliberately small and composable: the LLM decides which to call.
"""
from __future__ import annotations

import json
from typing import Any

from ..analysis.creative import analyze_creative_image
from ..analysis.creative_batch import analyze_pending
from ..analysis.offers import extract_offers_from_text
from ..analysis.themes import cluster_hooks
from ..config import load_competitors
from ..runner import ingest_all, ingest_competitor, ingest_one
from ..storage import connect
from ..synthesis.briefing import build_corpus, generate_briefing
from ..synthesis.creative_readout import cross_set_comparison, per_brand_readout
from ..synthesis.whitespace import detect_whitespace


TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_competitors",
        "description": "List every competitor in the registry with their tracked source counts.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "ingest_competitor",
        "description": (
            "Run every source adapter for a single competitor — fetches latest state, "
            "diffs against prior, persists observations and new ads. Use BEFORE analysis "
            "when fresh data is needed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "competitor_id": {"type": "string", "description": "id from competitors.yaml"},
            },
            "required": ["competitor_id"],
        },
    },
    {
        "name": "ingest_all",
        "description": "Run every source for every competitor. Heavy — only when starting a daily cycle.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "recent_ads",
        "description": "List new ads detected in the last N days for one or all competitors.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "default": 7, "minimum": 1, "maximum": 90},
                "competitor_id": {"type": "string", "description": "Optional — omit for all"},
            },
            "required": [],
        },
    },
    {
        "name": "recent_offers",
        "description": "List offers detected on competitor sites in the last N days.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "default": 7, "minimum": 1, "maximum": 90},
                "competitor_id": {"type": "string"},
            },
            "required": [],
        },
    },
    {
        "name": "extract_offers_from_text",
        "description": (
            "Run the offer-extraction taxonomy on an arbitrary string. Useful when reasoning "
            "about a single ad's copy or pasted landing-page text."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "analyze_creative",
        "description": (
            "Vision-classify a saved creative image against the Section-6 taxonomy "
            "(photography style, hook style, value props, casting, dominant colors, etc)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "image_path": {"type": "string", "description": "Local path under data/creative/ or data/raw/"},
            },
            "required": ["image_path"],
        },
    },
    {
        "name": "cluster_ad_hooks",
        "description": "Cluster the recent active ads of one competitor (or the whole set) by hook/theme.",
        "input_schema": {
            "type": "object",
            "properties": {
                "competitor_id": {"type": "string"},
                "limit": {"type": "integer", "default": 50, "maximum": 200},
            },
            "required": [],
        },
    },
    {
        "name": "detect_whitespace",
        "description": "Find angles/formats/offers no competitor in the set is using.",
        "input_schema": {
            "type": "object",
            "properties": {"vertical": {"type": "string"}},
            "required": [],
        },
    },
    {
        "name": "build_briefing_corpus",
        "description": "Assemble the structured corpus (new ads, offers, site changes) for a window. Lightweight — no LLM call.",
        "input_schema": {
            "type": "object",
            "properties": {"days": {"type": "integer", "default": 7}},
            "required": [],
        },
    },
    {
        "name": "generate_briefing",
        "description": "Write a full markdown competitive briefing for the last N days and persist it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "default": 7, "minimum": 1, "maximum": 90},
                "scope": {"type": "string", "description": "Free-form label, e.g. 'weekly' or 'competitor:glossier'"},
            },
            "required": [],
        },
    },
    {
        "name": "batch_analyze_creatives",
        "description": (
            "Vision-classify every unanalyzed creative image — populates `key_features`, "
            "`products_visible`, `photography_style`, etc. Idempotent and deduped by perceptual hash."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "competitor_id": {"type": "string", "description": "optional brand id; else all"},
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 500},
            },
            "required": [],
        },
    },
    {
        "name": "creative_readout",
        "description": "Per-brand creative attribute breakdown + net-new in window.",
        "input_schema": {
            "type": "object",
            "properties": {
                "competitor_id": {"type": "string"},
                "days": {"type": "integer", "default": 7, "minimum": 1, "maximum": 90},
            },
            "required": ["competitor_id"],
        },
    },
    {
        "name": "creative_comparison",
        "description": (
            "Cross-competitor creative comparison: set-wide popularity matrices, "
            "per-brand distinctiveness, and per-brand whitespace gaps."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "default": 30, "minimum": 1, "maximum": 180},
            },
            "required": [],
        },
    },
]


# ---- dispatch ----

def _list_competitors() -> dict[str, Any]:
    out = []
    for c in load_competitors():
        out.append({
            "id": c.id,
            "name": c.name,
            "vertical": c.vertical,
            "priority": c.priority,
            "source_count": len(c.sources),
            "source_types": sorted({s.type for s in c.sources}),
        })
    return {"competitors": out}


def _recent_ads(days: int = 7, competitor_id: str | None = None) -> dict[str, Any]:
    q = (
        "SELECT competitor_id, ad_archive_id, page_name, first_seen, body_text, cta_type, link_url "
        "FROM ads WHERE first_seen >= datetime('now', ?) "
    )
    params: list[Any] = [f"-{int(days)} days"]
    if competitor_id:
        q += " AND competitor_id=?"
        params.append(competitor_id)
    q += " ORDER BY first_seen DESC LIMIT 100"
    with connect() as conn:
        rows = conn.execute(q, params).fetchall()
    return {"count": len(rows), "ads": [dict(r) for r in rows]}


def _recent_offers(days: int = 7, competitor_id: str | None = None) -> dict[str, Any]:
    q = (
        "SELECT competitor_id, observed_at, kind, value, threshold, description, source "
        "FROM offers WHERE observed_at >= datetime('now', ?) "
    )
    params: list[Any] = [f"-{int(days)} days"]
    if competitor_id:
        q += " AND competitor_id=?"
        params.append(competitor_id)
    q += " ORDER BY observed_at DESC LIMIT 100"
    with connect() as conn:
        rows = conn.execute(q, params).fetchall()
    return {"count": len(rows), "offers": [dict(r) for r in rows]}


def _cluster_ad_hooks(competitor_id: str | None = None, limit: int = 50) -> dict[str, Any]:
    q = "SELECT ad_archive_id, body_text FROM ads WHERE active=1"
    params: list[Any] = []
    vertical = ""
    if competitor_id:
        q += " AND competitor_id=?"
        params.append(competitor_id)
        with connect() as conn:
            row = conn.execute("SELECT vertical FROM competitors WHERE id=?", (competitor_id,)).fetchone()
            if row:
                vertical = row["vertical"] or ""
    q += " ORDER BY first_seen DESC LIMIT ?"
    params.append(int(limit))
    with connect() as conn:
        rows = conn.execute(q, params).fetchall()
    ads = [{"ad_archive_id": r["ad_archive_id"], "body_text": r["body_text"]} for r in rows]
    return cluster_hooks(ads, vertical=vertical)


def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Execute a tool by name. Returns a JSON-serializable dict."""
    try:
        if name == "list_competitors":
            return _list_competitors()
        if name == "ingest_competitor":
            reports = ingest_competitor(args["competitor_id"])
            return {"reports": [r.__dict__ for r in reports]}
        if name == "ingest_all":
            reports = ingest_all()
            return {"reports": [r.__dict__ for r in reports]}
        if name == "recent_ads":
            return _recent_ads(args.get("days", 7), args.get("competitor_id"))
        if name == "recent_offers":
            return _recent_offers(args.get("days", 7), args.get("competitor_id"))
        if name == "extract_offers_from_text":
            return {"offers": extract_offers_from_text(args["text"])}
        if name == "analyze_creative":
            return analyze_creative_image(args["image_path"])
        if name == "cluster_ad_hooks":
            return _cluster_ad_hooks(args.get("competitor_id"), args.get("limit", 50))
        if name == "detect_whitespace":
            return detect_whitespace(vertical=args.get("vertical"))
        if name == "build_briefing_corpus":
            return build_corpus(days=args.get("days", 7))
        if name == "generate_briefing":
            res = generate_briefing(days=args.get("days", 7), scope=args.get("scope"))
            # Drop the (potentially large) corpus from the agent-visible result;
            # the briefing itself is what the agent will reason over.
            return {"briefing_id": res["briefing_id"], "title": res["title"], "body_md": res["body_md"]}
        if name == "batch_analyze_creatives":
            r = analyze_pending(competitor_id=args.get("competitor_id"), limit=args.get("limit", 50))
            return {
                "examined": r.examined, "analyzed": r.analyzed,
                "reused_phash": r.reused_phash, "failed": r.failed,
                "skipped_missing_file": r.skipped_missing_file,
                "errors": r.errors[:5],
            }
        if name == "creative_readout":
            r = per_brand_readout(args["competitor_id"], window_days=args.get("days", 7))
            # Strip the heavy tallies object before returning to the model.
            return {k: v for k, v in r.items() if k not in ("tallies", "tallies_new")}
        if name == "creative_comparison":
            r = cross_set_comparison(window_days=args.get("days", 30))
            return {k: v for k, v in r.items() if k not in ("set_tally", "comp_tallies")}
        return {"error": f"unknown tool: {name}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
