from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import DATA_DIR, DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS competitors (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  vertical TEXT,
  priority TEXT,
  meta_json TEXT
);

CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  competitor_id TEXT NOT NULL REFERENCES competitors(id),
  source_key TEXT NOT NULL,
  type TEXT NOT NULL,
  config_json TEXT NOT NULL,
  UNIQUE(competitor_id, source_key)
);

-- Immutable observation: every crawl writes a row.
-- raw_path / content_hash give us provenance + dedup.
CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id INTEGER NOT NULL REFERENCES sources(id),
  observed_at TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  raw_path TEXT,                       -- on-disk pointer (html/screenshot/json)
  parsed_json TEXT,                    -- structured fields extracted from raw
  changed INTEGER NOT NULL DEFAULT 0,  -- 1 if differs from prior observation
  change_summary TEXT,                 -- LLM-written diff explanation
  severity REAL DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS idx_obs_source_time ON observations(source_id, observed_at DESC);

-- One row per *distinct* ad creative seen across all crawls.
-- ad_archive_id is Meta's stable id; we dedupe on it.
CREATE TABLE IF NOT EXISTS ads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  competitor_id TEXT NOT NULL REFERENCES competitors(id),
  ad_archive_id TEXT NOT NULL,
  page_id TEXT,
  page_name TEXT,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  start_date TEXT,
  end_date TEXT,
  body_text TEXT,
  cta_type TEXT,
  link_url TEXT,
  publisher_platforms TEXT,
  raw_json TEXT,
  serp_position_rank INTEGER,          -- Meta Ad Library SERP rank (0=top), scrape-only
  UNIQUE(ad_archive_id)
);
CREATE INDEX IF NOT EXISTS idx_ads_competitor ON ads(competitor_id);
-- idx_ads_comp_rank is created in _migrate_ads_table() so it can land on existing
-- DBs after the ALTER TABLE for serp_position_rank.

-- Creative assets. Optionally linked to a paid ad; can also be standalone
-- (e.g. images extracted from an Amazon brand store).
CREATE TABLE IF NOT EXISTS creatives (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ad_id INTEGER REFERENCES ads(id),                       -- NULL for non-ad assets (brand store, website)
  competitor_id TEXT REFERENCES competitors(id),          -- direct competitor link; redundant when ad_id is set
  asset_type TEXT NOT NULL,           -- image | video_thumb | amazon_store_image | website_screenshot | ...
  asset_path TEXT NOT NULL,           -- local path under data/creative/
  phash TEXT,                         -- perceptual hash for visual dedup
  analysis_json TEXT,                 -- vision-model classification output
  analyzed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_creatives_ad ON creatives(ad_id);
CREATE INDEX IF NOT EXISTS idx_creatives_comp ON creatives(competitor_id);

-- Extracted offers / promos (Section 2)
CREATE TABLE IF NOT EXISTS offers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  competitor_id TEXT NOT NULL REFERENCES competitors(id),
  source TEXT NOT NULL,              -- 'website' | 'meta_ad' | etc
  source_ref TEXT,                   -- observation_id or ad_archive_id
  observed_at TEXT NOT NULL,
  kind TEXT,                         -- 'percent_off' | 'bogo' | 'free_shipping' | 'free_gift' | 'code'
  value TEXT,                        -- e.g. "20%", "$50", code string
  threshold TEXT,                    -- e.g. "orders over $50"
  description TEXT,
  starts_on TEXT,
  ends_on TEXT
);
CREATE INDEX IF NOT EXISTS idx_offers_competitor_time ON offers(competitor_id, observed_at DESC);

-- Structured promotional posture extracted from a brand homepage hero/banner.
-- One row per observation. hero_hash lets the runner cheaply skip the LLM
-- call when the hero crop hasn't changed since the prior observation.
CREATE TABLE IF NOT EXISTS homepage_promos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  competitor_id TEXT NOT NULL REFERENCES competitors(id),
  source_id INTEGER NOT NULL REFERENCES sources(id),
  observation_id INTEGER NOT NULL REFERENCES observations(id),
  observed_at TEXT NOT NULL,
  hero_hash TEXT,
  headline TEXT,
  subhead TEXT,
  primary_cta_text TEXT,
  primary_cta_url TEXT,
  offer_claim TEXT,
  offer_value REAL,
  offer_kind TEXT,
  expiration TEXT,
  channel_callouts TEXT,           -- JSON list
  urgency_cues TEXT,               -- JSON list
  confidence REAL,
  cache_hit INTEGER NOT NULL DEFAULT 0,
  raw_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_homepage_promos_comp_obs
  ON homepage_promos(competitor_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_homepage_promos_hero_hash
  ON homepage_promos(competitor_id, hero_hash);

-- LLM-written briefings (Section 21).
CREATE TABLE IF NOT EXISTS briefings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  scope TEXT NOT NULL,              -- 'weekly' | 'adhoc' | 'competitor:glossier'
  title TEXT NOT NULL,
  body_md TEXT NOT NULL,
  sources_json TEXT
);

-- Audit log (Section 25).
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  actor TEXT NOT NULL,              -- 'cli' | 'agent' | 'adapter:website' | ...
  action TEXT NOT NULL,
  details_json TEXT
);

-- Per-LLM-call telemetry: filled by intel.llm.client wrapper.
-- Phase 0 stubs this; Phase 1 wires the wrapper at every call site.
CREATE TABLE IF NOT EXISTS llm_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  call_site TEXT NOT NULL,           -- e.g. 'creative.analyze', 'briefing.generate'
  model TEXT NOT NULL,
  input_tokens INTEGER DEFAULT 0,
  output_tokens INTEGER DEFAULT 0,
  cache_read_tokens INTEGER DEFAULT 0,
  cache_creation_tokens INTEGER DEFAULT 0,
  latency_ms INTEGER DEFAULT 0,
  success INTEGER NOT NULL,
  error TEXT,
  attempt INTEGER DEFAULT 1,
  prompt_version TEXT,
  eval_run_id INTEGER,               -- non-null if this call was made inside an eval run
  task_id TEXT                       -- non-null if part of a task
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_eval ON llm_calls(eval_run_id, task_id);

-- Eval run aggregation. One row per `intel evals run` invocation.
CREATE TABLE IF NOT EXISTS eval_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  git_sha TEXT,
  task_count INTEGER DEFAULT 0,
  pass_count INTEGER DEFAULT 0,
  pass_slow_count INTEGER DEFAULT 0,
  fail_count INTEGER DEFAULT 0,
  skip_count INTEGER DEFAULT 0,
  headline_score REAL DEFAULT 0.0,
  notes TEXT
);

-- Per-task result inside an eval run. Drill-down lives here.
CREATE TABLE IF NOT EXISTS eval_task_results (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  eval_run_id INTEGER NOT NULL REFERENCES eval_runs(id),
  task_id TEXT NOT NULL,
  title TEXT NOT NULL,
  category TEXT NOT NULL,            -- 'regression' | 'failure_mode'
  status TEXT NOT NULL,              -- 'PASS' | 'PASS_SLOW' | 'FAIL' | 'SKIP' | 'ERROR'
  elapsed_ms INTEGER DEFAULT 0,
  graders_json TEXT,                 -- list[{name, passed, is_speed, detail}]
  output_json TEXT,                  -- task output (truncated if huge)
  error TEXT,
  skipped_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_eval_task_run ON eval_task_results(eval_run_id);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Blended popularity weights — v1 defaults, locked in via plan review.
# 0.55 SERP rank (Meta's own sort) + 0.35 run duration + 0.10 active bonus.
_POP_RANK_WEIGHT = 0.55
_POP_DURATION_WEIGHT = 0.35
_POP_ACTIVE_BONUS = 0.10
_POP_DURATION_CAP_DAYS = 60.0  # past 60d, more days don't keep accruing


def popularity_score(
    rank: int | None,
    start_date: str | None,
    last_seen: str | None,
    active: int | bool,
    brand_max_rank: int,
) -> float:
    """Blended ad-popularity proxy in [0, 1].

    Inputs (all available on the `ads` row):
      rank             — `serp_position_rank` (0 = top of Meta's "served-most-first"
                         sort; NULL for graph-API rows that have no rank concept).
      start_date       — ISO 'YYYY-MM-DD' or NULL.
      last_seen        — ISO timestamp from the most recent ingest.
      active           — 0/1, derived from `is_active_inferred` at ingest time.
      brand_max_rank   — the highest rank seen for this brand in the current
                         scrape (passed by the caller so we don't subquery here).

    The formula:
      rank_component     = 1 - rank / max(brand_max_rank, 1)   (NULL rank → 0)
      duration_component = min(duration_days / 60, 1)          (NULL start → 0)
      active_bonus       = 0.10 if active else 0
      score              = 0.55 * rank_component + 0.35 * duration_component + bonus

    Honest limits (do not delete; surface in the dashboard tooltip):
      1. Intra-brand only. A 200-ad brand's rank-1 and a 5-ad brand's rank-1 are
         not comparable. We normalize by brand_max_rank so the formula doesn't
         pretend otherwise, but cross-brand sorting is still misleading.
      2. Evergreen confound. Run duration treats "always-on hygiene ad" the same
         as "scaled winner."
      3. No raw impressions. This is a proxy. Meta does not expose impression
         numbers for commercial US advertisers; numeric ranges only land for
         EU political / social-issue ads (currently 0% of our corpus).
      4. Scrape cap. ads beyond `meta_ads_scrape.max_cards` (200 by default)
         have NULL rank and collapse to duration-only scoring.
    """
    rank_component = 0.0
    if rank is not None and brand_max_rank > 0:
        # rank=0 → top; rank=brand_max_rank → bottom.
        rank_component = max(0.0, 1.0 - float(rank) / max(brand_max_rank, 1))
    duration_component = 0.0
    if start_date and last_seen:
        try:
            # Accept both 'YYYY-MM-DD' (start_date) and ISO timestamps (last_seen).
            start = datetime.fromisoformat(start_date[:10])
            end = datetime.fromisoformat(last_seen[:10])
            days = max((end - start).days, 0)
            duration_component = min(days / _POP_DURATION_CAP_DAYS, 1.0)
        except (ValueError, TypeError):
            duration_component = 0.0
    active_bonus = _POP_ACTIVE_BONUS if active else 0.0
    score = (
        _POP_RANK_WEIGHT * rank_component
        + _POP_DURATION_WEIGHT * duration_component
        + active_bonus
    )
    return max(0.0, min(1.0, score))


def content_hash(payload: bytes | str) -> str:
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def init_db(db_path: Path | None = None) -> Path:
    p = Path(db_path or DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "snapshots").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "creative").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "raw").mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(p) as conn:
        conn.executescript(SCHEMA)
        _migrate_creatives_table(conn)
        _migrate_ads_table(conn)
    return p


def _migrate_ads_table(conn: sqlite3.Connection) -> None:
    """Add `serp_position_rank` column + index to pre-existing `ads` tables.
    Idempotent: a no-op once applied."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(ads)").fetchall()}
    if not cols:
        return  # fresh DB; SCHEMA already created it
    if "serp_position_rank" not in cols:
        conn.execute("ALTER TABLE ads ADD COLUMN serp_position_rank INTEGER")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ads_comp_rank "
        "ON ads(competitor_id, serp_position_rank)"
    )


def _migrate_creatives_table(conn: sqlite3.Connection) -> None:
    """Bring a pre-Phase-B1 `creatives` table forward:
       - add `competitor_id` column if missing (backfilled from ads.competitor_id)
       - drop NOT NULL on `ad_id` if present (rebuild table; SQLite limitation)
    Idempotent: a no-op if the table is already current.
    """
    cols = {row[1]: row for row in conn.execute("PRAGMA table_info(creatives)").fetchall()}
    if not cols:
        # Table doesn't exist yet (fresh db) — SCHEMA already created it.
        return

    has_competitor = "competitor_id" in cols
    # cols row tuple: (cid, name, type, notnull, dflt, pk)
    ad_id_notnull = cols.get("ad_id", (None, None, None, 0))[3] == 1

    if not has_competitor:
        conn.execute("ALTER TABLE creatives ADD COLUMN competitor_id TEXT REFERENCES competitors(id)")
        conn.execute(
            "UPDATE creatives SET competitor_id = ("
            " SELECT a.competitor_id FROM ads a WHERE a.id = creatives.ad_id"
            ") WHERE competitor_id IS NULL"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_creatives_comp ON creatives(competitor_id)")

    if ad_id_notnull:
        # SQLite can't drop a NOT NULL constraint in place — rebuild the table.
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("ALTER TABLE creatives RENAME TO creatives_legacy")
        conn.execute(
            "CREATE TABLE creatives ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " ad_id INTEGER REFERENCES ads(id),"
            " competitor_id TEXT REFERENCES competitors(id),"
            " asset_type TEXT NOT NULL,"
            " asset_path TEXT NOT NULL,"
            " phash TEXT,"
            " analysis_json TEXT,"
            " analyzed_at TEXT"
            ")"
        )
        conn.execute(
            "INSERT INTO creatives (id, ad_id, competitor_id, asset_type, asset_path, "
            "phash, analysis_json, analyzed_at) "
            "SELECT id, ad_id, competitor_id, asset_type, asset_path, "
            "phash, analysis_json, analyzed_at FROM creatives_legacy"
        )
        conn.execute("DROP TABLE creatives_legacy")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_creatives_ad ON creatives(ad_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_creatives_comp ON creatives(competitor_id)")
        conn.execute("PRAGMA foreign_keys=ON")


def _migrate_homepage_promos_table(conn: sqlite3.Connection) -> None:
    """Create the homepage_promos table on existing DBs that predate Phase H2.
    Fresh DBs already get this from SCHEMA; this is a no-op there.
    """
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='homepage_promos'"
    ).fetchone()
    if exists:
        return
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS homepage_promos (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      competitor_id TEXT NOT NULL REFERENCES competitors(id),
      source_id INTEGER NOT NULL REFERENCES sources(id),
      observation_id INTEGER NOT NULL REFERENCES observations(id),
      observed_at TEXT NOT NULL,
      hero_hash TEXT,
      headline TEXT,
      subhead TEXT,
      primary_cta_text TEXT,
      primary_cta_url TEXT,
      offer_claim TEXT,
      offer_value REAL,
      offer_kind TEXT,
      expiration TEXT,
      channel_callouts TEXT,
      urgency_cues TEXT,
      confidence REAL,
      cache_hit INTEGER NOT NULL DEFAULT 0,
      raw_json TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_homepage_promos_comp_obs
      ON homepage_promos(competitor_id, observed_at DESC);
    CREATE INDEX IF NOT EXISTS idx_homepage_promos_hero_hash
      ON homepage_promos(competitor_id, hero_hash);
    """)


@contextmanager
def connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    p = Path(db_path or DB_PATH)
    if not p.exists():
        init_db(p)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    # Idempotent forward-migrations — cheap (PRAGMA call); short-circuits if no work to do.
    _migrate_creatives_table(conn)
    _migrate_homepage_promos_table(conn)
    _migrate_ads_table(conn)
    conn.commit()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_competitor(conn: sqlite3.Connection, comp: Any) -> None:
    conn.execute(
        "INSERT INTO competitors(id, name, vertical, priority) VALUES (?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, vertical=excluded.vertical, priority=excluded.priority",
        (comp.id, comp.name, comp.vertical, comp.priority),
    )
    for src in comp.sources:
        conn.execute(
            "INSERT INTO sources(competitor_id, source_key, type, config_json) VALUES (?,?,?,?) "
            "ON CONFLICT(competitor_id, source_key) DO UPDATE SET config_json=excluded.config_json",
            (comp.id, src.stable_key(), src.type, json.dumps(src.__dict__, default=str)),
        )


def get_source_id(conn: sqlite3.Connection, competitor_id: str, source_key: str) -> int:
    row = conn.execute(
        "SELECT id FROM sources WHERE competitor_id=? AND source_key=?",
        (competitor_id, source_key),
    ).fetchone()
    if row:
        return row["id"]
    raise KeyError(f"source not registered: {competitor_id}/{source_key}")


def latest_observation(conn: sqlite3.Connection, source_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM observations WHERE source_id=? ORDER BY observed_at DESC LIMIT 1",
        (source_id,),
    ).fetchone()


def record_observation(
    conn: sqlite3.Connection,
    source_id: int,
    content_hash_val: str,
    raw_path: str | None,
    parsed: dict | None,
    changed: bool,
    change_summary: str | None = None,
    severity: float = 0.0,
) -> int:
    cur = conn.execute(
        "INSERT INTO observations(source_id, observed_at, content_hash, raw_path, parsed_json, changed, change_summary, severity) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            source_id,
            utcnow(),
            content_hash_val,
            raw_path,
            json.dumps(parsed) if parsed is not None else None,
            1 if changed else 0,
            change_summary,
            severity,
        ),
    )
    return cur.lastrowid or 0


def upsert_ad(conn: sqlite3.Connection, competitor_id: str, ad: dict) -> tuple[int, bool]:
    """Return (ad_row_id, is_new). `active` is derived from `is_active_inferred`
    (the adapter computes it from delivery stop time, since the API's own active
    flag is unreliable for US commercial ads)."""
    archive_id = str(ad["ad_archive_id"])
    active = 1 if ad.get("is_active_inferred", True) else 0
    existing = conn.execute(
        "SELECT id, first_seen FROM ads WHERE ad_archive_id=?", (archive_id,)
    ).fetchone()
    now = utcnow()
    rank = ad.get("serp_position_rank")
    if existing:
        # serp_position_rank: always overwrite with the freshest scrape, even with NULL
        # (graph-path ads have no rank; leaving NULL is correct).
        conn.execute(
            "UPDATE ads SET last_seen=?, active=?, end_date=COALESCE(?, end_date), "
            "body_text=COALESCE(?, body_text), cta_type=COALESCE(?, cta_type), "
            "link_url=COALESCE(?, link_url), publisher_platforms=COALESCE(?, publisher_platforms), "
            "raw_json=?, serp_position_rank=? WHERE id=?",
            (
                now,
                active,
                ad.get("end_date"),
                ad.get("body_text"),
                ad.get("cta_type"),
                ad.get("link_url"),
                json.dumps(ad.get("publisher_platforms")) if ad.get("publisher_platforms") else None,
                json.dumps(ad),
                rank,
                existing["id"],
            ),
        )
        return existing["id"], False
    cur = conn.execute(
        "INSERT INTO ads(competitor_id, ad_archive_id, page_id, page_name, first_seen, last_seen, "
        "active, start_date, end_date, body_text, cta_type, link_url, publisher_platforms, raw_json, "
        "serp_position_rank) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            competitor_id,
            archive_id,
            ad.get("page_id"),
            ad.get("page_name"),
            now,
            now,
            active,
            ad.get("start_date"),
            ad.get("end_date"),
            ad.get("body_text"),
            ad.get("cta_type"),
            ad.get("link_url"),
            json.dumps(ad.get("publisher_platforms")) if ad.get("publisher_platforms") else None,
            json.dumps(ad),
            rank,
        ),
    )
    return cur.lastrowid or 0, True


def upsert_creative(
    conn: sqlite3.Connection,
    ad_id: int | None,
    asset_type: str,
    asset_path: str,
    phash: str | None = None,
    *,
    competitor_id: str | None = None,
) -> tuple[int, bool]:
    """Insert a creative asset row. Idempotent on `asset_path` (unique per file
    on disk regardless of whether it came from an ad or a brand store).
    `ad_id` may be None for non-ad assets (brand store, website screenshot);
    in that case `competitor_id` must be provided.
    Returns (row_id, is_new)."""
    if ad_id is None and not competitor_id:
        raise ValueError("upsert_creative needs either ad_id or competitor_id")
    existing = conn.execute(
        "SELECT id FROM creatives WHERE asset_path=?",
        (asset_path,),
    ).fetchone()
    if existing:
        return existing["id"], False
    cur = conn.execute(
        "INSERT INTO creatives(ad_id, competitor_id, asset_type, asset_path, phash) "
        "VALUES (?,?,?,?,?)",
        (ad_id, competitor_id, asset_type, asset_path, phash),
    )
    return cur.lastrowid or 0, True


def prune_videos(conn: sqlite3.Connection, competitor_id: str, *, keep_n: int = 12) -> int:
    """Keep mp4 files only for the top-N most-popular ads per brand.

    Frames + transcript + analysis_json are KEPT for all videos — they're the
    analyzer's inputs, irreversibly small. Only the bulky mp4 is evicted.

    For each evicted video creative:
      - delete the mp4 from disk
      - update asset_type from 'video' to 'video_evicted' so the dashboard
        knows to render first-frame + Meta Ad Library click-out instead of
        an inline <video> tag
      - update asset_path to point at the first frame so renderers have
        something showable

    Returns the number of mp4s evicted.
    """
    from pathlib import Path as _Path

    # Pull all video creatives for this brand with their ad's score inputs.
    rows = conn.execute(
        "SELECT c.id AS creative_id, c.asset_path, c.asset_type, "
        "       a.id AS ad_id, a.serp_position_rank, a.start_date, a.last_seen, a.active "
        "FROM creatives c JOIN ads a ON a.id = c.ad_id "
        "WHERE a.competitor_id = ? AND c.asset_type IN ('video', 'video_evicted')",
        (competitor_id,),
    ).fetchall()
    if not rows:
        return 0

    # brand_max_rank from all of the brand's ads (not just video ads).
    max_rank_row = conn.execute(
        "SELECT MAX(serp_position_rank) FROM ads "
        "WHERE competitor_id=? AND serp_position_rank IS NOT NULL",
        (competitor_id,),
    ).fetchone()
    brand_max_rank = (max_rank_row[0] or 0) if max_rank_row else 0

    scored = []
    for r in rows:
        score = popularity_score(
            r["serp_position_rank"], r["start_date"], r["last_seen"],
            r["active"] or 0, brand_max_rank,
        )
        scored.append((score, dict(r)))
    scored.sort(key=lambda t: t[0], reverse=True)
    keep_ids = {t[1]["creative_id"] for t in scored[:keep_n]}

    evicted = 0
    for score, r in scored:
        cid = r["creative_id"]
        path = r["asset_path"]
        is_evicted_already = (r["asset_type"] == "video_evicted")
        in_top = cid in keep_ids
        if in_top:
            # Restore from evicted state if the ad climbed back into the top-N
            # (rare but possible). We don't re-download — just flag.
            if is_evicted_already:
                conn.execute(
                    "UPDATE creatives SET asset_type='video' WHERE id=?",
                    (cid,),
                )
            continue
        # Evict: delete the mp4 from disk (if still present), update row.
        if path and path.endswith(".mp4"):
            try:
                _Path(path).unlink(missing_ok=True)
            except Exception:
                pass
            # Find the first frame in the same directory (always kept).
            first_frame = None
            try:
                ad_dir = _Path(path).parent
                frames = sorted(ad_dir.glob("frame_*_t*.jpg"))
                if frames:
                    first_frame = str(frames[0])
            except Exception:
                pass
            conn.execute(
                "UPDATE creatives SET asset_type='video_evicted', asset_path=? "
                "WHERE id=?",
                (first_frame or path, cid),
            )
            if not is_evicted_already:
                evicted += 1
        elif not is_evicted_already:
            # asset_path already a frame (was evicted earlier and stamped here).
            conn.execute("UPDATE creatives SET asset_type='video_evicted' WHERE id=?", (cid,))
    return evicted


def record_offer(conn: sqlite3.Connection, competitor_id: str, offer: dict) -> int:
    cur = conn.execute(
        "INSERT INTO offers(competitor_id, source, source_ref, observed_at, kind, value, threshold, description, starts_on, ends_on) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            competitor_id,
            offer.get("source", "unknown"),
            offer.get("source_ref"),
            offer.get("observed_at", utcnow()),
            offer.get("kind"),
            offer.get("value"),
            offer.get("threshold"),
            offer.get("description"),
            offer.get("starts_on"),
            offer.get("ends_on"),
        ),
    )
    return cur.lastrowid or 0


def record_homepage_promo(
    conn: sqlite3.Connection,
    competitor_id: str,
    source_id: int,
    observation_id: int,
    observed_at: str,
    promo: dict,
    hero_hash: str | None,
    cache_hit: bool = False,
) -> int:
    cur = conn.execute(
        "INSERT INTO homepage_promos(competitor_id, source_id, observation_id, observed_at, "
        "hero_hash, headline, subhead, primary_cta_text, primary_cta_url, "
        "offer_claim, offer_value, offer_kind, expiration, "
        "channel_callouts, urgency_cues, confidence, cache_hit, raw_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            competitor_id,
            source_id,
            observation_id,
            observed_at,
            hero_hash,
            promo.get("headline"),
            promo.get("subhead"),
            promo.get("primary_cta_text"),
            promo.get("primary_cta_url"),
            promo.get("offer_claim"),
            promo.get("offer_value"),
            promo.get("offer_kind"),
            promo.get("expiration"),
            json.dumps(promo.get("channel_callouts") or []),
            json.dumps(promo.get("urgency_cues") or []),
            promo.get("confidence"),
            1 if cache_hit else 0,
            json.dumps(promo),
        ),
    )
    return cur.lastrowid or 0


def fetch_prior_homepage_promo_by_hero_hash(
    conn: sqlite3.Connection, competitor_id: str, hero_hash: str
) -> dict | None:
    """Look up the most recent homepage_promos row for this brand + hero_hash.
    Used by the runner to skip the LLM call when the hero crop hasn't changed."""
    row = conn.execute(
        "SELECT raw_json FROM homepage_promos "
        "WHERE competitor_id=? AND hero_hash=? AND raw_json IS NOT NULL "
        "ORDER BY observed_at DESC LIMIT 1",
        (competitor_id, hero_hash),
    ).fetchone()
    if not row or not row["raw_json"]:
        return None
    try:
        return json.loads(row["raw_json"])
    except Exception:
        return None


def record_briefing(conn: sqlite3.Connection, scope: str, title: str, body_md: str, sources: list | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO briefings(created_at, scope, title, body_md, sources_json) VALUES (?,?,?,?,?)",
        (utcnow(), scope, title, body_md, json.dumps(sources) if sources else None),
    )
    return cur.lastrowid or 0


def audit(conn: sqlite3.Connection, actor: str, action: str, details: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO audit_log(ts, actor, action, details_json) VALUES (?,?,?,?)",
        (utcnow(), actor, action, json.dumps(details) if details else None),
    )


def asset_path(kind: str, *parts: str) -> Path:
    """Resolve a storage path under data/{kind}/... and ensure parent exists."""
    base = DATA_DIR / kind
    p = base.joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def record_llm_call(
    conn: sqlite3.Connection,
    call_site: str,
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    latency_ms: int = 0,
    success: bool = True,
    error: str | None = None,
    attempt: int = 1,
    prompt_version: str | None = None,
    eval_run_id: int | None = None,
    task_id: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO llm_calls(ts, call_site, model, input_tokens, output_tokens, "
        "cache_read_tokens, cache_creation_tokens, latency_ms, success, error, "
        "attempt, prompt_version, eval_run_id, task_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            utcnow(), call_site, model,
            input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
            latency_ms, 1 if success else 0, error,
            attempt, prompt_version, eval_run_id, task_id,
        ),
    )
    return cur.lastrowid or 0


def start_eval_run(conn: sqlite3.Connection, *, git_sha: str | None = None, notes: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO eval_runs(started_at, git_sha, notes) VALUES (?,?,?)",
        (utcnow(), git_sha, notes),
    )
    return cur.lastrowid or 0


def finish_eval_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    task_count: int,
    pass_count: int,
    pass_slow_count: int,
    fail_count: int,
    skip_count: int,
    headline_score: float,
) -> None:
    conn.execute(
        "UPDATE eval_runs SET finished_at=?, task_count=?, pass_count=?, "
        "pass_slow_count=?, fail_count=?, skip_count=?, headline_score=? WHERE id=?",
        (utcnow(), task_count, pass_count, pass_slow_count,
         fail_count, skip_count, headline_score, run_id),
    )


def record_task_result(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    task_id: str,
    title: str,
    category: str,
    status: str,
    elapsed_ms: int,
    graders: list[dict],
    output: Any = None,
    error: str | None = None,
    skipped_reason: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO eval_task_results(eval_run_id, task_id, title, category, status, "
        "elapsed_ms, graders_json, output_json, error, skipped_reason) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            run_id, task_id, title, category, status, elapsed_ms,
            json.dumps(graders, default=str),
            json.dumps(output, default=str)[:200_000] if output is not None else None,
            error, skipped_reason,
        ),
    )
    return cur.lastrowid or 0
