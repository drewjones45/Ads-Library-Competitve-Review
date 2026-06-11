# intel — Agentic Competitive Intelligence

A working MVP of the agentic competitive intelligence tool described in [competitive-intel-tool-checklist copy.md](./competitive-intel-tool-checklist%20copy.md). Tracks competitor ads, on-site offers, and creative themes, then synthesizes weekly briefings.

This implementation covers the **highest-leverage early wins** called out in the checklist:

- Meta Ads Library new-ad detection (Section 4)
- On-site offer/promo extraction (Sections 1, 2)
- Vision-based creative classification (Section 6)
- Cross-competitor briefings + whitespace detection (Section 21)
- A tool-using agent that orchestrates the above (Section 19)

Built around Claude's tool-use API — each capability is exposed as a discrete tool the agent (or the CLI) can call independently.

---

## Architecture at a glance

```
config/competitors.yaml          ← source registry
        │
        ▼
src/intel/adapters/              ← website + meta_ads (extensible)
        │  raw HTML/JSON → data/raw/
        ▼
src/intel/runner.py              ← fetch → diff → enrich → persist
        │
        ▼
SQLite (data/intel.db)           ← observations, ads, offers, briefings, audit
        │
        ▼
src/intel/analysis/              ← offer extraction, vision creative analysis, theme clustering
src/intel/synthesis/             ← briefings + whitespace
        │
        ▼
src/intel/agent/                 ← tool-use loop — Claude decides what to call
src/intel/cli.py                 ← `intel <command>` exposes everything
```

**Why this shape:** the checklist's Section 24 ("Data Model & Infra") calls for `raw → parsed → enriched → scored` layers. Every adapter writes raw payloads to disk, parses into structured fields, then enrichment runs only on *changed* observations (Section 24 cost controls).

---

## Setup

```bash
# Python 3.11+
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e .

cp .env.example .env
# Fill in:
#   ANTHROPIC_API_KEY            (required for any LLM call)
#   META_AD_LIBRARY_ACCESS_TOKEN (optional; required for Meta ads ingestion)

intel init                       # create db, register competitors from yaml
```

Edit [config/competitors.yaml](./config/competitors.yaml) to point at your competitive set. Each competitor has a list of `sources`, each with a `type` and a `cadence`.

---

## CLI

```bash
intel init                                  # bootstrap db
intel ingest                                # run every adapter for every competitor
intel ingest --competitor glossier          # one competitor
intel ads --days 7                          # new ads detected this week
intel offers --days 14                      # promos/codes seen
intel brief --days 7                        # write a markdown briefing
intel brief --days 7 --no-llm               # deterministic briefing (no API key needed)
intel whitespace --vertical skincare        # gap analysis
intel cluster-hooks --competitor glossier   # theme buckets across recent ads
intel extract-offers "20% off site-wide"    # ad-hoc offer parse
intel briefings                             # list saved briefings
intel show-briefing 3                       # render one
```

### Creative analysis pipeline

After ingesting Meta ads via the scrape path (which downloads images to `data/creative/`):

```bash
intel analyze-creative path/to/ad.jpg                      # single image, ad-hoc
intel analyze-creatives                                    # batch — all unanalyzed creatives
intel analyze-creatives --competitor wayfair --limit 100   # one brand, cap calls
intel creative-readout --competitor bobs --days 7          # per-brand: net new + attribute distribution
intel creative-readout --competitor bobs --save reports/bobs.md
intel creative-comparison --days 30                        # cross-set: popularity, distinctiveness, whitespace
intel creative-comparison --save reports/comparison.md
```

What gets extracted per image (vision schema in [src/intel/analysis/creative.py](src/intel/analysis/creative.py)):

- `photography_style` — model_on_figure / flat_lay / lifestyle / studio_product_only / screenshot_ui / text_only / mixed
- `production_style` — polished_brand / ugc_creator_style / meme_graphic / mixed
- `product_emphasis` — product_forward / lifestyle_forward / balanced
- `products_visible` — array of product nouns (e.g. `sectional sofa`, `dining table`, `mattress`)
- `key_features` — array of visual flags (`price_visible`, `discount_badge`, `free_shipping_badge`, `brand_logo`, `cta_button_in_image`, `model_present`, `multi_product_collage`, etc.)
- `value_props`, `hook_style`, `urgency_cues`, `casting`, `dominant_colors_hex`, `seasonal_tags`, `notable_text`, `summary_one_line`

The batch analyzer is **idempotent** — re-runs only process net-new creatives, and reuses the analysis from any visually identical image (perceptual-hash match) without spending another model call.

Cost guide: ~$0.005–0.015 per image with `claude-sonnet-4-6` vision (a 250-image set runs $1–4).

### Agent mode

The orchestrating agent (`intel agent`) gives Claude the full toolset and a free-form goal. The model decides which tools to call.

```bash
intel agent "what's the biggest competitive move this week?"
intel agent "ingest everything, then write the weekly briefing" --verbose
intel agent "did any competitor launch a sale in the last 3 days?"
```

`--verbose` streams each tool call to stderr so you can see the agent's plan.

---

## Adapters

| Type        | Status | Notes                                                                  |
|-------------|--------|------------------------------------------------------------------------|
| `website`   | ✅     | httpx + BeautifulSoup; extracts hero / banner / popup regions for diff |
| `meta_ads`  | ✅     | Graph API path wired; browser-scrape path is an extension hook         |
| `retailer`  | ⏳     | Adapter registry ready — drop a new module in `src/intel/adapters/`    |
| `rss`       | ⏳     | Same                                                                   |

Adding a new adapter: subclass `Adapter`, implement `fetch() -> IngestResult`, register in `adapters/__init__.py`. The runner picks it up automatically.

### Meta Ads Library notes

Two paths, selected per source via the `method:` field in `competitors.yaml`:

| `method:` | When to use | Notes |
|---|---|---|
| `auto` (default) | Most cases | Try Graph API; fall back to scrape only if no token at all |
| `graph_api` | Force API only | Token + identity confirmation required; `ad_active_status=ACTIVE` is unreliable for US commercial ads, so we pull `ALL` and infer active state from delivery stop time |
| `scrape` | When the API returns no current ads for a page (commercial-coverage gap) | Requires `page_id` (numeric Facebook Page ID); navigates the public Ad Library web UI with Playwright, extracts each card, downloads creative images |

**Browser-scrape setup** (one-time):

```bash
pip install -e '.[browser]'
playwright install chromium
```

When `method: scrape`, the adapter pulls:
- ad archive id, body copy, CTA label
- start date, active status (visible-on-card)
- platforms (Facebook / Instagram / Threads / etc.)
- landing-page link URL (unwraps `l.facebook.com/?u=...`)
- creative image URLs → downloaded to `data/creative/<competitor>/<archive_id>/`
- per-image perceptual hash → `creatives` table (dedup + vision-analysis input)

Find a Page ID by visiting the brand's Facebook page and copying the digits from `view_all_page_id=` in any Ad Library URL, or from the page's About → Page Transparency section.

---

## What's deliberately deferred

The checklist is a 6+ month roadmap. This MVP intentionally skips:

- Section 7 organic-social ingestion (needs platform-specific adapters + auth)
- Sections 13–14 tech stack / corporate signals (BuiltWith-style fingerprinting)
- Section 22 real-time alerting (the audit log + severity field are ready; wiring Slack/email is a 1-day add)
- Section 23 dashboard UI (CLI + markdown briefings cover the strategic loop; UI is read-only on top of this same db)
- Headless-browser rendering for SPA sites (extension point in `WebsiteAdapter._fetch_html`)

Each of these slots cleanly into the existing structure without refactoring the core.

---

## Data location

- `data/intel.db` — SQLite store (observations, ads, offers, briefings, audit log)
- `data/raw/<competitor>/<source_type>/...` — every raw HTML/JSON payload, timestamped (the "own Wayback" from Section 18)
- `data/snapshots/`, `data/creative/` — screenshot + creative-asset archives

---

## Governance (Section 25)

- Every adapter sets a custom UA identifying as a bot
- `audit_log` table records every ingestion + enrichment action
- LLM enrichment is gated to *changed* observations to cap cost
- Raw payloads are stored alongside parsed fields so any signal can be replayed "as of" a date

You're responsible for respecting per-source ToS (Meta Ad Library terms, target-site robots.txt). The defaults are polite (single request per source, custom UA, no concurrency in the adapter layer); production should add per-domain rate-limits at the scheduler level.
