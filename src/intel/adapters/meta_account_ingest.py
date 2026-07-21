"""Orchestration for owned Meta ad-account ingest.

Ties `meta_account.py` (the Graph client) to storage: pulls insights, resolves
creative metadata, classifies each ad, acquires whatever visual asset exists, and
writes ads/creatives/performance rows.

Owned ads are written into the shared `ads` + `creatives` tables under
`source='meta_owned'` so the existing vision-analysis pipeline
(scripts/cc_vision_prep.py → cc_vision_write.py) and the phash dedup work on them
with no changes. Because every existing report path filters on an explicit source
set that excludes 'meta_owned', none of the competitor reports are affected.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

from . import meta_account as ma
from .. import storage


log = logging.getLogger("intel.meta_account_ingest")


def _asset_dir(base: Path, competitor_id: str, platform_ad_id: str) -> Path:
    return base / "creative_owned" / competitor_id / platform_ad_id


def ingest_account(
    conn,
    *,
    account_id: str,
    account_name: str | None,
    competitor_id: str,
    data_dir: Path,
    since: str | None = None,
    until: str | None = None,
    date_preset: str = "last_90d",
    render_previews: bool = True,
    max_previews: int = 0,
) -> dict[str, Any]:
    """Ingest one owned account. Returns a summary dict.

    `max_previews` caps how many preview screenshots are rendered (0 = no cap).
    Rendering drives a real browser per ad, so it is the slowest step by far;
    capping it lets a first run stay quick while still covering the top spenders,
    which is why ads are rendered in descending spend order.
    """
    client = httpx.Client(timeout=120)
    try:
        raw_rows = ma.fetch_insights(
            account_id, since=since, until=until,
            date_preset=date_preset, attribution_windows=ma.ATTR_WINDOWS,
            client=client,
        )
        if not raw_rows:
            log.warning("account %s returned no delivering ads for the window", account_id)
            return {"account_id": account_id, "ads": 0, "assets": 0, "previews": 0,
                    "coverage": {"by_class": {}, "total_spend": 0.0,
                                 "analyzable_spend": 0.0, "analyzable_pct": 0.0}}

        perf = [ma.normalize_insight(r) for r in raw_rows]

        # NOTE on data-driven attribution: measured on these accounts, a `dda`
        # request returns a value byte-identical to the account default for every
        # ad — the accounts are already configured on data-driven attribution, so
        # `dda` is not a distinct number. It was therefore dropped: it added no
        # information and the extra insights request per account doubled the load
        # that trips Meta's app-level rate limit. True incrementality needs a
        # conversion-lift holdout study, not an attribution window.
        for p in perf:
            p["attribution_json"] = json.dumps(p["attribution"])
            # Pin the stored window to the REQUESTED range. Accounts sit in
            # different timezones, so Meta echoes date_start/date_stop shifted by
            # a day or two per account; left as-is, each account's data for "the
            # same window" would land under a slightly different key and fail to
            # overwrite the existing rows. The requested range is the one logical
            # window the dashboard selects on.
            if since and until:
                p["date_start"], p["date_stop"] = since, until
        ad_ids = [p["platform_ad_id"] for p in perf if p.get("platform_ad_id")]
        meta_by_id = ma.fetch_ad_meta(account_id, ad_ids, client=client)

        # Audience facets come from the adset, not the ad. Fetch only the adsets
        # these ads actually reference — the accounts hold thousands, and asking
        # for the whole edge with targeting expanded trips a Graph 500.
        adset_ids = [p.get("adset_id") for p in perf if p.get("adset_id")]
        adsets = ma.fetch_adsets(account_id, adset_ids, client=client)
        audience_by_adset = {
            aid: ma.classify_audience(node) for aid, node in adsets.items()
        }

        summary_rows: list[dict[str, Any]] = []
        preview_jobs: list[tuple[str, Path]] = []
        preview_targets: list[tuple[str, str, Path]] = []  # (ad_id, creative_id, dest)
        assets_written = 0

        # Highest spend first so a capped preview run covers what matters most.
        perf.sort(key=lambda r: r.get("spend") or 0, reverse=True)

        for row in perf:
            pid = row["platform_ad_id"]
            node = meta_by_id.get(pid) or {}
            creative = node.get("creative") or {}
            cls = ma.classify_creative(creative)
            copy = ma.creative_copy(creative)

            # Mirror into the shared `ads` table so vision + dashboards see it.
            ad_dict = {
                "ad_archive_id": pid,          # the account's native ad.id
                "page_id": (creative.get("effective_object_story_id") or "").split("_")[0] or None,
                "page_name": account_name,
                "is_active_inferred": True,
                "start_date": row.get("date_start"),
                "end_date": None,
                "body_text": copy.get("body"),
                "cta_type": copy.get("cta_type"),
                "link_url": copy.get("link_url"),
                "source": "meta_owned",
            }
            ad_db_id, _ = storage.upsert_ad(conn, competitor_id, ad_dict, source="meta_owned")

            storage.upsert_owned_ad(
                conn,
                platform_ad_id=pid,
                competitor_id=competitor_id,
                account_id=account_id,
                account_name=account_name,
                ad_db_id=ad_db_id,
                meta={
                    "ad_name": row.get("ad_name"),
                    "campaign_id": row.get("campaign_id"),
                    "campaign_name": row.get("campaign_name"),
                    "adset_id": row.get("adset_id"),
                    "adset_name": row.get("adset_name"),
                    "creative_id": creative.get("id"),
                    "object_type": creative.get("object_type"),
                    "creative_class": cls,
                    "title": copy.get("title"),
                    "body": copy.get("body"),
                    "cta_type": copy.get("cta_type"),
                    "link_url": copy.get("link_url"),
                    "product_set_id": creative.get("product_set_id"),
                    "effective_object_story_id": creative.get("effective_object_story_id"),
                    "created_time": node.get("created_time"),
                    **audience_by_adset.get(row.get("adset_id") or "", {}),
                    "raw": {"creative": creative, "ad_name": row.get("ad_name")},
                },
            )
            storage.upsert_ad_performance(
                conn, competitor_id=competitor_id, account_id=account_id, row=row,
            )

            # --- visual assets -------------------------------------------------
            dest_dir = _asset_dir(data_dir, competitor_id, pid)
            for idx, asset in enumerate(ma.extract_assets(creative)):
                url = asset.get("url")
                if not url:
                    continue
                suffix = ".jpg"
                name = f"{asset['kind']}_{idx}{suffix}"
                dest = dest_dir / name
                if dest.exists() or ma.download_asset(url, dest, client=client):
                    storage.upsert_creative(
                        conn, ad_db_id, asset["kind"], str(dest),
                        competitor_id=competitor_id, source="meta_owned",
                    )
                    assets_written += 1

            # A rendered preview is the best available asset — the ad as served,
            # with copy and social proof composited in. DPA ads have no fixed
            # creative, so Meta refuses to render them; don't waste a browser
            # navigation on a known-unavailable render.
            if render_previews and cls == "analyzable" and creative.get("id"):
                if not max_previews or len(preview_targets) < max_previews:
                    dest = dest_dir / "preview.png"
                    if not dest.exists():
                        preview_targets.append((pid, creative["id"], dest))

            summary_rows.append({
                "platform_ad_id": pid, "creative_class": cls,
                "spend": row.get("spend"), "impressions": row.get("impressions"),
            })

        # Resolve preview iframe URLs, then render them in one browser session.
        previews_ok = 0
        if preview_targets:
            for pid, cid, dest in preview_targets:
                url = ma.preview_iframe_url(cid, client=client)
                if url:
                    preview_jobs.append((url, dest))
            results = ma.render_previews(preview_jobs)
            for (pid, cid, dest) in preview_targets:
                if results.get(str(dest)):
                    ad_db = conn.execute(
                        "SELECT ad_db_id FROM owned_ads WHERE platform_ad_id=?", (pid,)
                    ).fetchone()
                    storage.upsert_creative(
                        conn, ad_db["ad_db_id"] if ad_db else None,
                        "ad_preview", str(dest),
                        competitor_id=competitor_id, source="meta_owned",
                    )
                    previews_ok += 1
                    assets_written += 1

        coverage = ma.spend_coverage(summary_rows)
        stages: dict[str, int] = {}
        for a in audience_by_adset.values():
            s = a.get("audience_stage") or "unknown"
            stages[s] = stages.get(s, 0) + 1
        return {
            "audience_stages": stages,
            "account_id": account_id, "account_name": account_name,
            "competitor_id": competitor_id,
            "ads": len(perf), "assets": assets_written,
            "previews": previews_ok, "preview_attempted": len(preview_targets),
            "coverage": coverage,
        }
    finally:
        client.close()
