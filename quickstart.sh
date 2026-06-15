#!/usr/bin/env bash
#
# quickstart.sh — run the full intel pipeline end-to-end.
#
# What it does:
#   1. Sanity-check the environment (Python venv, .env, required tools)
#   2. Initialize the SQLite db (no-op if already initialized)
#   3. Ingest every source for every competitor (websites + Meta ad library)
#   4. Vision-analyze all unanalyzed creative images (skipped if no API key)
#   5. Per-brand creative readout for each competitor
#   6. Cross-set creative comparison
#   7. Competitive briefing for the week (LLM if key set; deterministic otherwise)
#   8. HTML dashboard at reports/<date>/dashboard/index.html
#
# All artifacts land in reports/<UTC-date>/.
#
# Safe to re-run any time — every step is idempotent.
#
# Usage:
#   ./quickstart.sh                  # full pipeline
#   ./quickstart.sh --skip-ingest    # use existing data, just rebuild reports
#   ./quickstart.sh --days 14        # widen the analysis window (default 7)

set -u
# NOTE: not `set -e` — we want to continue past missing-key skips and per-brand
# readout failures rather than abort the whole run.

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

# ---- args ----
SKIP_INGEST=0
DAYS=7
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-ingest) SKIP_INGEST=1; shift ;;
    --days)        DAYS="$2"; shift 2 ;;
    -h|--help)
      head -25 "$0" | grep -E '^#' | sed 's/^# \?//'
      exit 0
      ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# ---- helpers ----
bold()   { printf "\033[1m%s\033[0m\n" "$*"; }
green()  { printf "\033[32m%s\033[0m\n" "$*"; }
yellow() { printf "\033[33m%s\033[0m\n" "$*"; }
red()    { printf "\033[31m%s\033[0m\n" "$*"; }
rule()   { printf "\033[2m%s\033[0m\n" "────────────────────────────────────────────────────────────"; }

# ---- preflight ----
bold "[1/9] preflight"
rule
if [[ ! -x ".venv/bin/intel" ]]; then
  red "  ✗ .venv/bin/intel not found. Run:"
  echo "      python3.13 -m venv .venv && .venv/bin/pip install -e '.[browser]' && .venv/bin/playwright install chromium"
  exit 1
fi
green "  ✓ venv present"

if [[ ! -f ".env" ]]; then
  yellow "  ⚠ .env not found — creating from .env.example (you'll need to fill in keys)"
  cp .env.example .env 2>/dev/null || true
fi

# Load .env. Only `KEY=value` and `# comment` lines are supported in .env;
# anything else will trip bash and should be moved out.
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

HAS_ANTHROPIC=0
HAS_META=0
[[ -n "${ANTHROPIC_API_KEY:-}" ]] && HAS_ANTHROPIC=1
[[ -n "${META_AD_LIBRARY_ACCESS_TOKEN:-}" ]] && HAS_META=1
[[ $HAS_ANTHROPIC -eq 1 ]] && green "  ✓ ANTHROPIC_API_KEY set"     || yellow "  ⚠ ANTHROPIC_API_KEY missing — vision + briefing will be limited"
[[ $HAS_META      -eq 1 ]] && green "  ✓ META_AD_LIBRARY_ACCESS_TOKEN set" || yellow "  ⚠ META token missing — Meta ads sources will fail"
[[ -n "${SERPAPI_API_KEY:-}" ]] && green "  ✓ SERPAPI_API_KEY set (Google ATC via API)" || yellow "  ⚠ SERPAPI_API_KEY missing — Google ads (if configured) fall back to a Playwright scrape"

# ---- output dir ----
DATE="$(date -u +%Y-%m-%d)"
REPORTS="reports/${DATE}"
mkdir -p "$REPORTS"
green "  ✓ writing reports to: $REPORTS/"
echo

# ---- 2. init ----
bold "[2/9] init db"; rule
.venv/bin/intel init
echo

# ---- 3. ingest ----
bold "[3/9] ingest"; rule
if [[ $SKIP_INGEST -eq 1 ]]; then
  yellow "  skipped (--skip-ingest)"
else
  .venv/bin/intel ingest 2>&1 | tee "$REPORTS/ingest.log"
fi
echo

# ---- 4. capture landing pages ----
# Screenshot the on-brand pages ads send traffic to (needs ad link_urls from
# ingest above). The next step then analyzes these alongside ad creatives.
bold "[4/9] capture landing pages"; rule
if [[ $SKIP_INGEST -eq 1 ]]; then
  yellow "  skipped (--skip-ingest)"
else
  .venv/bin/intel capture-landing-pages 2>&1 | tee "$REPORTS/landing_capture.log"
fi
echo

# ---- 5. analyze creatives ----
bold "[5/9] vision-analyze creatives"; rule
if [[ $HAS_ANTHROPIC -eq 1 ]]; then
  .venv/bin/intel analyze-creatives 2>&1 | tee "$REPORTS/creative_analysis.log"
else
  yellow "  skipped — no ANTHROPIC_API_KEY"
fi
echo

# ---- 5. per-brand readouts ----
bold "[6/9] per-brand creative readouts"; rule
mkdir -p "$REPORTS/by-brand"
.venv/bin/python - "$REPORTS/by-brand" "$DAYS" <<'PY'
import sys, subprocess, sqlite3, pathlib
out_dir = pathlib.Path(sys.argv[1])
days = sys.argv[2]
with sqlite3.connect("data/intel.db") as c:
    ids = [r[0] for r in c.execute("SELECT id FROM competitors ORDER BY id").fetchall()]
for cid in ids:
    target = out_dir / f"{cid}.md"
    print(f"  → {cid} → {target}")
    subprocess.run([".venv/bin/intel", "creative-readout",
                    "--competitor", cid, "--days", days, "--save", str(target)],
                   check=False)
PY
echo

# ---- 6. cross-set comparison ----
bold "[7/9] cross-set comparison"; rule
.venv/bin/intel creative-comparison --days "$DAYS" --save "$REPORTS/creative_comparison.md"
echo

# ---- 7. briefing ----
bold "[8/9] briefing"; rule
BRIEF="$REPORTS/briefing.md"
if [[ $HAS_ANTHROPIC -eq 1 ]]; then
  green "  using LLM-synthesized briefing"
  .venv/bin/intel brief --days "$DAYS" >/dev/null
  # The briefing is in the db; export the most recent one to file.
  .venv/bin/python - "$BRIEF" <<'PY'
import sys, sqlite3
out = sys.argv[1]
with sqlite3.connect("data/intel.db") as c:
    row = c.execute(
        "SELECT title, body_md, created_at, scope FROM briefings ORDER BY id DESC LIMIT 1"
    ).fetchone()
if row:
    with open(out, "w") as f:
        f.write(f"# {row[0]}\n\n_created: {row[2]} · scope: {row[3]}_\n\n---\n\n{row[1]}")
    print(f"  wrote {out}")
PY
else
  green "  using deterministic (no-LLM) briefing"
  .venv/bin/intel brief --days "$DAYS" --no-llm >/dev/null
  .venv/bin/python - "$BRIEF" <<'PY'
import sys, sqlite3
out = sys.argv[1]
with sqlite3.connect("data/intel.db") as c:
    row = c.execute(
        "SELECT title, body_md, created_at, scope FROM briefings ORDER BY id DESC LIMIT 1"
    ).fetchone()
if row:
    with open(out, "w") as f:
        f.write(f"# {row[0]}\n\n_created: {row[2]} · scope: {row[3]}_\n\n---\n\n{row[1]}")
    print(f"  wrote {out}")
PY
fi
echo

# ---- 8. HTML dashboard ----
# Bob's competitive set excludes the Revlon control brand; Revlon gets its own
# dashboard under reports/revlon/<date>/ so it never muddies the furniture set.
bold "[9/9] HTML dashboard"; rule
.venv/bin/intel dashboard --out "$REPORTS/dashboard" --days "$DAYS" --exclude revlon
.venv/bin/intel dashboard --out "reports/revlon/${DATE}/dashboard" --days "$DAYS" --only revlon
# Separate "with Google" report set — adds Google ATC ads (in-dashboard platform
# filter + dedicated Text-ads section). The Meta reports above are unchanged;
# these land in a distinct with-google/ subfolder so nothing is clobbered. Empty
# of Google data until a google_ads source is configured in competitors.yaml.
.venv/bin/intel dashboard --platform all --out "$REPORTS/with-google/dashboard"    --days "$DAYS" --exclude revlon
.venv/bin/intel dashboard --platform all --out "$REPORTS/with-google/dashboard-v2" --days "$DAYS" --exclude revlon --v2
echo

# ---- summary ----
bold "done"; rule
echo "outputs:"
find "$REPORTS" -maxdepth 2 -type f | sort | sed 's/^/  /'
echo
green "view a report:"
echo "    open $REPORTS/dashboard/index.html        ← single-page HTML dashboard (excl. Revlon)"
echo "    open $REPORTS/with-google/dashboard/index.html    ← with Google ATC ads (platform filter + Text ads)"
echo "    open reports/revlon/${DATE}/dashboard/index.html  ← Revlon (control) — separate set"
echo "    open $REPORTS/creative_comparison.md"
echo "    open $REPORTS/by-brand/bobs.md"
echo "    open $REPORTS/briefing.md"
