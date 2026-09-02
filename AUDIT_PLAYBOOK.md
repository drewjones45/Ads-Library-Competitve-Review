# Competitive Ads Library Audit — Onboarding Playbook

For anyone running one of these client audits (Bobs, TREX, Philo, Wegmans, JD
Sports, Spectrum, ...) for the first time. Written from the Edward Jones
deployment (2026-09-02) — the first client onboarded end-to-end by someone with
no prior access to this repo. Numbers and gotchas below are real, not
estimated, unless marked otherwise.

Read [README.md](README.md) first for the architecture. This document is the
*process* on top of it: what order to do things in, where people got stuck,
and how long each phase actually took.

---

## TL;DR timeline

| Phase | First time (new machine, new client) | Repeat (known machine, new client) |
|---|---|---|
| 1. Environment setup | 15–30 min | 0 (skip) |
| 2. Client intake + competitor research | 15–30 min | 15–30 min |
| 3. Meta page-ID resolution + verification | 30–90 min (per brand: 3 min clean, 15–20 min messy) | same |
| 4. Write `config/` + `quickstart_*.sh` | 30–45 min | 15 min (copy + edit a prior file) |
| 5. Ingest pipeline run | 5–15 min | 5–15 min |
| 6. Vision analysis | 20–30 min (no API key, subagent path) / ~5 min (keyed) | same |
| 7. Report + dashboard build | 2–5 min | 2–5 min |
| 8. Git staging + commit | 10–15 min | 5 min |
| 9. Push (handle divergence if any) | 5–20 min | 5 min, more if conflicts |
| 10. Netlify build + publish | 5 min (org-level build permission bug hit + fixed once, see Gotcha 8) | 5 min |
| **Total, active work** | **~2.5–4.5 hours** | **~1–1.5 hours** |

The Edward Jones run took the long end of most of these because it was the
first run on this machine (broken `.venv`, no working Python) and hit two
undocumented site-level bot-blocks. A repeat run on a working machine, for a
client whose page IDs resolve cleanly, is much faster — most of the time above
is one-time repo/environment debt, not audit work.

---

## Phase 1 — Environment setup

Check this before anything else. The committed `.venv` in this repo is **not
portable** — it was built on a specific machine and its shebang points at that
machine's absolute path.

```bash
.venv/bin/intel --help
```

If that fails with something like `cannot execute: No such file or directory`,
the venv is stale. Also check the Python version — `pyproject.toml` requires
`>=3.11`, and this machine only had system Python 3.9.6.

**Fix (what we did):**

```bash
brew install python@3.13
rm -rf .venv
/opt/homebrew/bin/python3.13 -m venv .venv
.venv/bin/pip install -e '.[browser]'
.venv/bin/playwright install chromium
```

Verify: `.venv/bin/intel --help` should print the command list, and
`.venv/bin/python -c "from playwright.sync_api import sync_playwright"` should
not error.

**Gotcha:** Playwright's browser processes may get killed by a sandboxed shell
tool. If a scrape fails with `Target page, context or browser has been closed`
or `kill EPERM`, retry with the sandbox disabled for that command rather than
debugging the site — it's an execution-environment issue, not a site issue.

---

## Phase 2 — Client intake + competitor research

Get from the client (or requester): the client's own brand, their Meta Ad
Library page ID if they have one, and the named competitor set.

**Verify names before doing anything else.** The Edward Jones request as typed
said "Edwards Jones" and "Ameripris" — neither is a real company. The actual
entities are **Edward Jones** (Edward D. Jones & Co., L.P.) and **Ameriprise
Financial**. Get this wrong and every artifact downstream — config comments,
`competitor_id`s, report titles — inherits the typo. State the correction
explicitly to whoever asked, don't just silently fix it.

Do a first pass on each competitor's actual business model before wiring
anything. Ask: is this brand a clean single-category competitor, or does its
Meta page also carry unrelated business lines? This mattered a lot here —
Wells Fargo, Fidelity and Merrill's pages mix wealth-advisory creative with
retail banking, self-directed brokerage and credit-card ads that Edward Jones
doesn't compete in at all. Write this down now; it becomes the "mixed-business
caveat" in the config header and changes how the final numbers should be read
(attribute mix is comparable, raw ad-volume is not).

---

## Phase 3 — Meta page-ID resolution + verification

**This is the highest-risk, most time-variable phase.** A wrong page ID
produces a dashboard that looks complete and is quietly about the wrong thing.
The JD Sports deployment already hit this once (client-supplied ID was the
UK/global page, not the US one) — treat every page ID, client-supplied or not,
as unverified until proven otherwise.

### Why free-text search doesn't work

Graph API `search_terms` is spam-ranked and effectively unusable — a search
for "Merrill" or "Merrill Lynch" surfaces "Base Camp Trading," diet-supplement
pages and unrelated small businesses ahead of anything real. Don't trust its
ranking, and don't conclude a brand has no Meta presence just because Graph's
`ads_archive` + `search_page_ids` returns 0 active US ads for a real page —
that's a known commercial-ads coverage gap, not evidence of absence.

### What actually works

```bash
.venv/bin/python scripts/resolve_meta_page_ids.py <slug1> <slug2> ...
```

This script (added during the Edward Jones run, codifying what the JD Sports
run did by hand) does two things for each `facebook.com/<slug>`:

1. Loads the page in Playwright and reads the numeric ID out of the embedded
   `delegate_page` JSON.
2. **Verifies** it by running the real scrape path
   (`meta_ads_scrape.scrape_page_ads`) against that ID with image downloads
   off, in a throwaway temp dir — so a wrong guess never pollutes
   `INTEL_DATA_DIR`.

An ID that parses but returns zero ad cards is reported as **NOT VERIFIED**,
not silently accepted.

```bash
.venv/bin/python scripts/resolve_meta_page_ids.py --verify-only <numeric_id>   # check a client-supplied ID
.venv/bin/python scripts/resolve_meta_page_ids.py --no-verify <slug>          # resolve without spending a scrape
```

### Traps this catches that slug-guessing alone won't

- `facebook.com/merrill` and `facebook.com/Merrill` both resolve to **"Merrill
  Oakes,"** an unrelated person. Merrill Lynch had no working slug at all —
  recovered by reading `page_id`/`page_name` pairs out of the public **Ad
  Library keyword-search result JSON** (`?q=Merrill&search_type=keyword_unordered`),
  then confirming the candidate via scrape.
- `facebook.com/ameripriseadvisors` resolves to **one individual advisor's
  branch page** ("Michael Harrison - Ameriprise Financial Services, LLC | Flint
  TX"), not the national brand. Brands that run thousands of local/advisor
  pages (Edward Jones, Ameriprise, and similarly any franchise-model brand)
  need extra care here — confirm you have the *national* page, e.g. by
  checking that ad copy and landing links carry no advisor name or local
  geography.
- `page_name` returned by the scraper is **not reliable** as an identity
  check — it came back as a literal zero-width space for one brand in this
  run. Confirm identity instead on (a) the brand name as the first line of
  `body_text` on multiple cards, and (b) the landing-page domain in
  `link_url`.

### Google Ads Transparency Center (optional, often not available)

```bash
curl -sG "https://serpapi.com/search.json" \
  --data-urlencode "engine=google_ads_transparency_center" \
  --data-urlencode "text=<brand domain or name>" \
  --data-urlencode "api_key=$SERPAPI_API_KEY"
```

If this returns no advertiser records, **don't guess an ID.** An absent
source is better than a wrong or indirect-entity one (the JD Sports and
Wegmans deployments both rejected candidates on this basis). Note in the
config that it wasn't wired and why.

---

## Phase 4 — Write the config + quickstart

Copy the most similar existing pair as a starting template —
`config/competitors_jdsports.yaml` is the most heavily annotated and worth
copying structurally even for an unrelated vertical.

```
config/competitors_<client>.yaml   # competitor registry + full rationale
quickstart_<client>.sh             # isolated pipeline wrapper
```

**What the config header must capture**, based on what turned out to matter
later: the competitive-set rationale (why each competitor, what was excluded
and why), the resolved+verified page IDs with a one-line note on *how* each
was confirmed, and any caveat that changes how the output should be read (the
mixed-business note above). Future readers — including you, next time — will
not re-derive any of this from the data alone.

**What the quickstart must set**, isolated per client so runs never collide:

```bash
export INTEL_DB_PATH="$ROOT/data/<client>.db"
export INTEL_DATA_DIR="$ROOT/data/<client>_assets"
export INTEL_COMPETITORS_FILE="$ROOT/config/competitors_<client>.yaml"
```

Add a page-ID gate as an early step that checks for empty `page_id` fields and
fails loudly with the resolver command to run — cheaper than discovering it
mid-ingest. (`meta_ads.py` itself already refuses to scrape with no
`page_id`; the gate just surfaces that before you've waited for other sources
to finish.)

---

## Phase 5 — Ingest pipeline run

```bash
./quickstart_<client>.sh --days 30
```

Runs init → ingest (website + meta_ads sources) → landing-page capture →
(vision analysis, if keyed) → readouts → comparison → briefing → dashboards.

**Known truncation:** `meta_ads_scrape.scrape_page_ads` defaults to
`max_cards=200`. Any brand that hits exactly 200 has a **floor, not a true
count** — three of six brands did in this run. Check for this after every
ingest:

```bash
.venv/bin/python -c "
import sqlite3
c = sqlite3.connect('data/<client>.db')
for r in c.execute('SELECT competitor_id, COUNT(*) FROM ads GROUP BY 1 ORDER BY 2 DESC'):
    print(r, '⚠ capped' if r[1] >= 200 else '')
"
```

Do not compare ad-volume across brands until this is either raised or
equalized across the set.

**Known bot-blocks (fixed, but know the symptom):** some bank/finance sites
fingerprint and block Playwright's legacy `chrome-headless-shell` binary.
`fidelity.com` failed outright (`ERR_HTTP2_PROTOCOL_ERROR`); worse,
`schwab.com`'s homepage capture *silently succeeded* while actually capturing
an anti-bot "unable to authorize your request" page — which would have been
vision-analyzed as if it were real content had a subagent not caught it. This
is now fixed at the shared adapter level (`src/intel/adapters/website.py`,
`BROWSER_CHANNEL = "chromium"` — Playwright's new headless mode, launched at
both `chromium.launch()` call sites). If a future site still fails or a
landing capture looks suspiciously short/blank, this class of fix is the first
thing to try.

---

## Phase 6 — Vision analysis

Check `.env` for `ANTHROPIC_API_KEY` first.

**If set:**

```bash
.venv/bin/intel analyze-creatives
```

Cost is roughly $0.005–0.015/image (README's figure). For a set this size
(415 unique images after dedup) that's $2–6.

**If not set (this run's case) — the Claude Code subagent path:**

```bash
INTEL_DB_PATH=data/<client>.db .venv/bin/python scripts/cc_vision_prep.py /tmp/tasks.json --max-image 0 --max-video 0
```

This dedups by perceptual hash first — 415 representative images covered 807
of 836 total creatives here, most of the savings from Schwab's 200+ raw ads
collapsing to far fewer distinct images. Split the task list into batches
(~30 tasks each worked well — big enough to be efficient, small enough that
one bad batch doesn't waste much), write the shared schema prompt once, and
fan out one subagent per batch **in parallel** (background `Agent` calls, not
sequential) — 14 batches ran concurrently and the whole pass took well under
an hour instead of batches × single-batch-time.

**Validate before writing to the db** — every batch, every time:

```python
# ids match the input batch exactly, no dupes, no missing entries, uniform schema keys
```

We caught two real problems this way: one batch's landing-page screenshot was
analyzed *before* a same-session bot-block fix landed, so the "analysis" was
of an error page — had to be dropped and redone against the corrected asset.
And several images were genuinely blank/corrupt (solid black or white
frames) — expected, not a batch failure; those get `confidence: 0.0` and move
on.

```bash
INTEL_DB_PATH=data/<client>.db .venv/bin/python scripts/cc_vision_write.py merged_results.json
```

Writes the analysis and propagates it to every phash-identical sibling —
watch the "propagated to N siblings" count in the output; it's usually much
larger than the batch size and is how 415 analyzed images covered 836
creatives.

---

## Phase 7 — Rebuild reports

```bash
./quickstart_<client>.sh --skip-ingest --days 30
```

Regenerates readouts, comparison, briefing and both dashboard variants from
whatever's in the db now — cheap, re-run freely after any vision-analysis or
data change.

---

## Phase 8 — Git staging + commit

**Historical precedent, confirmed across TREX/Philo/Wegmans/JD Sports:**
`config/`, `quickstart_*.sh`, `data/<brand>.db`, `data/<brand>_assets/` (100%
of files, every brand checked), and `reports/<brand>/` (md/html/log only, no
images) are all tracked. There is no category that's "normally left
untracked" among these — the assets and db go in.

```bash
git status --porcelain          # see everything that changed
git diff --cached --name-only | grep -iE 'env|secret|token|key' # sanity check before commit
```

**Watch for scope creep in staging.** If something stages the whole working
tree (a broad `git add .` habit, or an editor/IDE action), check for files
that aren't part of your changeset before committing — in this run, three
unrelated files (a spec doc + a script) from someone else's *already-pushed*
work got swept in. Diff them against `origin/main` before deciding to unstage
— if identical, they're just noise to remove from your commit, not something
you're at risk of losing.

```bash
git diff --cached --name-only | xargs -I{} sh -c 'git hash-object "{}" ; git rev-parse "origin/main:{}" 2>/dev/null'
```

---

## Phase 9 — Push

```bash
git push origin main
```

**If rejected as non-fast-forward,** someone else pushed while you worked.
Don't force-push. Fetch and inspect the real overlap before rebasing blind:

```bash
git fetch origin
MB=$(git merge-base HEAD origin/main)
comm -12 <(git diff --name-only $MB origin/main | sort) <(git diff --name-only $MB HEAD | sort)
```

If the overlap is empty or trivial (in this run it was exactly one line in
`.gitignore` — two people appending different lines at the same spot), rebase
is safe and mechanical:

```bash
git rebase origin/main
# resolve the conflict by keeping BOTH additions if they're each other's non-conflicting intent
git add .gitignore && git rebase --continue
git push origin main
```

Confirm after: `git rev-list --left-right --count origin/main...HEAD` should
read `0  0`.

---

## Phase 10 — Netlify build + publish

The site is Git-connected — per `NETLIFY.md`, a push to `main` should trigger
CI to run `python3 scripts/build_site.py` and publish `dist/`. `build_site.py`
auto-discovers every deployment under `reports/**`, so a new client needs zero
Netlify-side configuration to appear.

**Confirm the build works locally before assuming CI will do the same:**

```bash
python3 scripts/build_site.py
open dist/index.html
open dist/<client>/<date>/dashboard/index.html
```

This needs no Netlify access and catches build-script problems (missing
assets, broken refs) independently of anything Netlify-side. `dist/` is
gitignored, so this never touches what gets pushed.

**Gotcha 8 — the one that cost the most time on this run, RESOLVED
2026-09-02:** prod can be stale for reasons that have nothing to do with the
repo, the commit, or who authored it. In this case: **Netlify was configured
to only build deploys triggered by commits from the original repo owner
(Andrew)'s GitHub account.** Two consecutive pushes from two different
non-owner accounts (a collaborator's `spectrum` commits, then this Edward
Jones commit) landed on `main` and neither triggered a deploy — confirmed a
repo/commit-side false lead, since the collaborator's work was clean and still
didn't publish. Andrew confirmed he fixed the build-permission setting on the
Netlify side, and prod updated immediately after — that update is the
validation this is actually the root cause, not just a plausible one.

If prod doesn't update ~10–15 minutes after a push:

1. Check the Netlify deploy log for the site directly (not the repo) — it
   will show *no deploy triggered*, a failed build, or a stale success.
2. If no deploy triggered at all, suspect a build-permission/collaborator
   restriction on the Netlify side before debugging the repo further. This is
   an infra setting, not something fixable via git — it needs whoever has
   admin access to the Netlify team/site to widen the allowed committers
   (Site settings → Build & deploy, or the GitHub App installation's
   permitted accounts). Confirm with them that this is actually fixed org-wide
   (not just re-run for one deploy) so the next new-client push isn't the one
   that rediscovers it.
3. As a stopgap that needs no admin fix, deploy manually from a local build —
   works for anyone with `netlify login` access to the site regardless of who
   committed:

```bash
npm install -g netlify-cli
netlify login
./scripts/deploy_netlify.sh --prod
```

---

## Reference — what a new client deployment adds to the repo

```
config/competitors_<client>.yaml
quickstart_<client>.sh
scripts/resolve_meta_page_ids.py        # shared, not per-client — added once
data/<client>.db
data/<client>_assets/creative/...
reports/<client>/<date>/{briefing.md, creative_comparison.md, by-brand/*.md, dashboard*/index.html, *.log}
```

For a 6-competitor set of comparable scope to Edward Jones: ~670 ads, ~836
creatives, ~140 MB of assets, ~150 MB total staged in the commit.

## Reference — sizing this run for comparison

- 6 competitors, 670 ads ingested, 836 creatives (415 unique after phash
  dedup), 836/836 vision-analyzed.
- 3 of 6 brands hit the 200-ad scrape cap (volume truncated, not exhaustive).
- 2 site-level bot-blocks hit and fixed at the shared adapter level.
- 1 `.gitignore` merge conflict, trivially resolved.
- 1 Netlify build-permission issue (owner-only builds), found after the fact
  and fixed by Andrew on the Netlify side — not resolved by anything in this
  repo, but confirmed fixed once prod updated.
