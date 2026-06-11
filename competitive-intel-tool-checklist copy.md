# Agentic Competitive Intelligence Tool — Feature Checklist

A build-base checklist for an agentic tool that monitors competitors and surfaces **marketing opportunities and strategy signals**. Organized into two halves:

- **Part 1 — Intelligence domains:** *what* to track (the signals).
- **Part 2 — The agentic system layer:** *how* the tool ingests, detects change, reasons, scores, and delivers.

Each item is a discrete capability. Treat checkboxes as a backlog seed — most are independent and can be shipped incrementally.

> Scope assumption: consumer/DTC + retail brands (skincare, apparel, etc.), given Meta Ads Library + retailer presence are central. Flag/trim anything that doesn't fit your verticals.

---

## Part 1 — Intelligence Domains

### 1. Website & On-Site Messaging
- [ ] Homepage hero / above-the-fold messaging changes (headline, subhead, CTA, hero creative)
- [ ] Sitewide promo banners / announcement bars (offer text, dates, threshold)
- [ ] Popup / email-capture offer (the incentive %, the gate, the timing)
- [ ] Full-page screenshot archive per monitored URL (visual diff over time)

### 2. Pricing & Promotions
- [ ] Discount depth (% off) and frequency sitewide
- [ ] Promotional calendar reconstruction (infer cadence: monthly, holiday, flash)
- [ ] Free-shipping threshold changes
- [ ] Promo / coupon code detection (sitewide codes, affiliate codes, gated codes)

### 4. Paid Social — Meta Ads Library
- [ ] New ad detection (delta vs. last crawl) with launch-date capture
- [ ] Active ad count over time (overall + by Page)
- [ ] Ad longevity tracking (long-running ads = proven winners — flag the survivors)
- [ ] Creative format breakdown (single image / video / carousel / collection)
- [ ] Placement spread (FB, IG, Messenger, Audience Network, Reels)
- [ ] Landing-page destination per ad (which URLs/offers they're driving to)
- [ ] CTA button type distribution (Shop Now, Learn More, Sign Up, etc.)
- [ ] Offer extraction from ad copy (% off, BOGO, free gift, free shipping)
- [ ] Messaging / hook theme clustering (problem-solution, social proof, urgency, founder story)
- [ ] Launch velocity (new ads per week — testing cadence as a budget signal)
- [ ] Spend & impression *ranges* where disclosed (EU/political; otherwise note unavailability)
- [ ] Seasonal / campaign burst detection (volume spikes around events)
- [ ] Multiple-Page detection (sub-brands, regional Pages, influencer whitelisting Pages)
- [ ] Iteration tracking (same creative concept, copy/format A/B variants in market)

### 6. Creative Analysis (cross-channel, the AI-heavy layer)
*Apply vision/LLM analysis to all captured creative — Meta, TikTok, organic, email, on-site.*
- [ ] Photography style classification (model/on-figure vs. flat-lay vs. lifestyle vs. studio/product-only)
- [ ] Video vs. still ratio (and trend over time)
- [ ] Video sub-analysis: hook style (first 3s), length, pacing, captions-on-by-default
- [ ] UGC / creator-style vs. polished brand production
- [ ] Text-overlay presence & density (and whether copy is offer-led vs. benefit-led)
- [ ] Dominant color palette extraction per creative & aggregate brand palette drift
- [ ] Aspect-ratio / placement-format mix (1:1, 4:5, 9:16)
- [ ] Product-forward vs. lifestyle-forward emphasis
- [ ] Offer/urgency presence in creative (countdown, "ends soon," scarcity)
- [ ] Value-prop themes (efficacy, price, sustainability, inclusivity, convenience)
- [ ] Emotional vs. rational appeal classification
- [ ] Casting / representation patterns (demographic diversity, age range, etc.)
- [ ] Influencer / creator identification (recurring faces, handle detection)
- [ ] On-brand consistency scoring (logo, font, layout system adherence)
- [ ] Seasonal / thematic tagging (holiday, back-to-school, "summer skin," etc.)
- [ ] Trend detection: emerging creative patterns *across* the competitive set, not just one brand
- [ ] "Whitespace" detection — angles/formats *none* of the set is using (the opportunity output)

### 7. Organic Social
- [ ] Posting cadence by platform (IG, TikTok, FB, YouTube, X, Pinterest)
- [ ] Content theme / pillar mix (educational, product, UGC, BTS, entertainment)
- [ ] Engagement rate trends (likes/comments/shares relative to follower base)
- [ ] Follower / subscriber growth trajectory
- [ ] Format adoption (Reels, Shorts, carousels, Lives) and shift over time
- [ ] Trend / audio / hashtag adoption speed (are they early or late to formats?)
- [ ] Top-performing organic posts (what resonates — informs paid)
- [ ] Community management style (response rate, tone, comment handling)
- [ ] Organic influencer mentions & partnerships (tagged collabs)
- [ ] Cross-post vs. platform-native strategy


### 13. Martech / Tech Stack
*This is reverse-engineering their measurement & CRO maturity — directly actionable for strategy.*
- [ ] Tech-stack fingerprinting (BuiltWith / Wappalyzer-style: platform, CMS, frameworks)
- [ ] Pixel & tag detection (Meta, Google, TikTok, Pinterest — what they measure)
- [ ] Server-side / CAPI signals (Elevar, Stape, enhanced-matching indicators)
- [ ] A/B testing & personalization tools detected (Optimizely, VWO, Dynamic Yield, etc.)
- [ ] Live experiment detection (variant cookies, flicker, split URLs)
- [ ] Reviews / loyalty / subscription app fingerprints (Yotpo, Okendo, Recharge, etc.)
- [ ] Consent-management / privacy stack (CMP, geo-gating behavior)
- [ ] Analytics maturity inference (GA4, Amplitude, warehouse-native signals)
- [ ] CDN / hosting / performance stack changes

### 14. Corporate & Strategic Signals
- [ ] Press release / newsroom monitoring
- [ ] News & media mention tracking (PR sentiment, share of coverage)
- [ ] Funding / M&A / investment events
- [ ] Job-posting monitoring (hiring = strategic direction; e.g., "Retail Media Lead," "International GM")
- [ ] Leadership / org changes (LinkedIn, exec departures/hires)
- [ ] Trademark filings (new product/sub-brand names before launch — early-warning signal)
- [ ] Patent filings (innovation pipeline)
- [ ] Investor / earnings materials (if public — guidance, segment performance, marketing-spend commentary)
- [ ] Events / sponsorships / experiential activations
- [ ] CSR / sustainability / regulatory positioning shifts

---

## Part 2 — The Agentic System Layer

How the tool actually *operates*. This is where "monitoring" becomes "agentic intelligence."

### 16. Ingestion & Source Management
- [ ] Per-competitor source registry (URLs, Page IDs, handles, retailer SKUs, seed inboxes)
- [ ] Per-source crawl cadence config (price = daily, corporate = weekly, etc.)
- [ ] Adapter pattern per source type (web scrape, Meta Ads Library API/CLI, retailer API, RSS)
- [ ] Headless-browser rendering for JS-heavy / SPA sites
- [ ] Anti-bot resilience (rotating context, rate-limiting, polite crawling, backoff)
- [ ] Raw-payload storage (HTML, JSON, image/video assets) separate from parsed data
- [ ] Idempotent ingestion (re-runs don't create dupes)

### 17. Change Detection & Diffing
- [ ] Structured diff for prices/text/catalog (field-level deltas)
- [ ] Visual diff for pages & creative (perceptual hashing / screenshot comparison)
- [ ] Semantic diff (LLM judges "is this a *meaningful* change?" to suppress noise)
- [ ] Threshold/noise filtering (ignore timestamps, session IDs, cart counts)
- [ ] New-entity detection (new ad, new SKU, new retailer) vs. modification detection

### 18. Archival & Historical State
- [ ] Time-series store for every tracked numeric/categorical field (price, ad count, rating)
- [ ] Immutable snapshot archive (screenshots, creative assets, raw HTML) — your own Wayback
- [ ] Versioning so any signal can be replayed "as of" a date
- [ ] Asset library for all captured creative (dedup by hash, tagged with analysis output)

### 19. LLM Analysis & Enrichment
- [ ] Vision model for creative classification (the Section 6 taxonomy)
- [ ] LLM summarization of textual changes ("what changed and why it matters")
- [ ] Entity extraction (offers, claims, prices, dates) into structured fields
- [ ] Sentiment & theme clustering for reviews / social / news
- [ ] Confidence + provenance on every enriched field (which source, which crawl, model used)
- [ ] Human-in-the-loop correction loop (label fixes feed back into prompts/taxonomy)

### 20. Scoring & Prioritization
- [ ] Relevance/severity scoring per change (is this a price-tweak or a category launch?)
- [ ] Configurable weighting by *your* strategic priorities (e.g., weight creative > corporate)
- [ ] Threat vs. opportunity classification
- [ ] Anomaly detection (statistically unusual spend velocity, sellouts, review spikes)
- [ ] Trend vs. one-off distinction (sustained shift vs. blip)

### 21. Synthesis & Opportunity Detection
*The strategic output — the reason the tool exists.*
- [ ] Cross-competitor benchmarking
- [ ] Whitespace / gap analysis (angles, formats, price points, segments nobody owns)
- [ ] Pattern synthesis across the set ("3 of 5 competitors moved to UGC video in Q2")
- [ ] Auto-generated "so what + now what" recommendations tied to each finding
- [ ] Competitive matrix / positioning map auto-build
- [ ] Periodic auto-narrative (weekly/monthly LLM-written competitive briefing)
- [ ] Hypothesis generation for your own A/B test backlog (seed from competitor moves)

### 22. Alerting & Delivery
- [ ] Real-time alerts for high-severity events (new ad burst, big price drop, etc)
- [ ] Digest mode (daily/weekly roll-up to avoid alert fatigue)
- [ ] Channel delivery: Slack, email, in-app inbox (configurable per severity)
- [ ] Alert dedup & grouping (one "they launched a sale" event, not 40 SKU pings)
- [ ] Per-user / per-competitor subscription preferences
- [ ] Export to slides / report for stakeholder distribution

### 23. Querying, Dashboards & UI
- [ ] Natural-language query over the corpus ("show me every BOGO offer last quarter")
- [ ] Competitor dashboards (per-brand and comparative views)
- [ ] Time-series charts for price/ad-count/rating/SOV
- [ ] Creative gallery with filterable analysis tags
- [ ] Saved views / watchlists
- [ ] Drill-down from any metric to the source snapshot (provenance everywhere)

### 24. Data Model & Infra
- [ ] Canonical schema: competitor → channel → asset/signal → observation (timestamped)
- [ ] Stable entity IDs across crawls (so a SKU/ad/page persists through changes)
- [ ] Warehouse/store choice (you'll likely want BigQuery here given your stack)
- [ ] Separation of raw → parsed → enriched → scored layers (medallion-style)
- [ ] Backfill & reprocessing capability (rerun enrichment when taxonomy improves)
- [ ] Cost controls on LLM/vision calls (cache, only enrich on real change)

### 25. Governance, Compliance & Ethics
- [ ] Respect robots.txt / ToS posture per source; document what's API vs. scrape
- [ ] Rate-limiting & polite crawling (don't hammer competitor infra)
- [ ] PII handling policy (review authors, creator handles, seed-inbox data)
- [ ] Data-source legal review (Meta Ads Library API terms, retailer terms, etc.)
- [ ] Audit log of what was collected, when, from where
- [ ] Clear "intelligence vs. inference" labeling (don't present estimates as facts)

---

## Suggested Build Sequence (MVP → mature)

1. **Foundation:** source registry, ingestion adapters, raw + time-series storage, basic web diff.
2. **First high-value signal:** Meta Ads Library new-ad detection + price tracking (your two stated priorities) with Slack/email alerts.
3. **Creative intelligence:** vision-based creative tagging (Section 6) on captured ad assets.
4. **Synthesis:** cross-competitor benchmarking + weekly auto-briefing.
5. **Expand horizontally:** layer in additional channels (email capture, retail, SEO, reviews).
6. **Maturity:** NL query, dashboards, opportunity/whitespace engine, hypothesis-to-test pipeline.

> **Highest-leverage early wins** for a marketing-strategy use case: ads-library new-creative alerts, creative-format trend tagging, price/promo calendar reconstruction, and the weekly synthesized briefing. Those four alone deliver most of the "what should we do this week" value.
