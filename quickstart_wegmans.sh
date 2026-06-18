#!/usr/bin/env bash
#
# quickstart_wegmans.sh — run the full intel pipeline end-to-end for the WEGMANS
# grocery competitive set. Fully isolated from the Bobs, TREX and Philo
# deployments (its own DB, asset dir, competitors file and reports tree).
#
# Isolation:
#   - INTEL_DB_PATH          → data/wegmans.db
#   - INTEL_DATA_DIR         → data/wegmans_assets/
#   - INTEL_COMPETITORS_FILE → config/competitors_wegmans.yaml
#   - reports                → reports/wegmans/<UTC-date>/
#
# Competitive set: Wegmans (subject) vs ShopRite, Stop & Shop, Giant Food,
# ACME Markets, Price Chopper. Competitors are pulled from the Meta Ad Library
# (verified page_ids); Wegmans — which runs no Meta brand-page ads — is pulled
# from Google Ads Transparency Center (advertiser AR18314717603664232449) + its
# website. See config/competitors_wegmans.yaml for the full source map.
#
# Vision analysis note: this environment has NO ANTHROPIC_API_KEY, so the
# in-pipeline `intel analyze-creatives` step is a no-op. Creative vision
# analysis is instead produced by Claude Code subagents that read each asset
# and write schema-matched JSON straight into creatives.analysis_json (the
# scripts/cc_vision_prep.py + cc_vision_write.py path). Run that step between
# [4] and [6].
#
# Usage:
#   ./quickstart_wegmans.sh                  # full pipeline (ingest + reports)
#   ./quickstart_wegmans.sh --skip-ingest    # use existing data, just rebuild reports
#   ./quickstart_wegmans.sh --reports-only   # alias for --skip-ingest
#   ./quickstart_wegmans.sh --days 14        # widen the analysis window (default 7)

set -u

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

# ---- isolation env vars (the whole point of this wrapper) ----
export INTEL_DB_PATH="$ROOT/data/wegmans.db"
export INTEL_DATA_DIR="$ROOT/data/wegmans_assets"
export INTEL_COMPETITORS_FILE="$ROOT/config/competitors_wegmans.yaml"

mkdir -p "$INTEL_DATA_DIR"

# ---- args ----
SKIP_INGEST=0
DAYS=7
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-ingest|--reports-only) SKIP_INGEST=1; shift ;;
    --days)        DAYS="$2"; shift 2 ;;
    -h|--help)
      head -30 "$0" | grep -E '^#' | sed 's/^# \?//'
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
bold "[1/9] preflight (WEGMANS deployment)"
rule
if [[ ! -x ".venv/bin/intel" ]]; then
  red "  ✗ .venv/bin/intel not found. Run:"
  echo "      python3.13 -m venv .venv && .venv/bin/pip install -e '.[browser]' && .venv/bin/playwright install chromium"
  exit 1
fi
green "  ✓ venv present"
green "  ✓ INTEL_DB_PATH          = $INTEL_DB_PATH"
green "  ✓ INTEL_DATA_DIR         = $INTEL_DATA_DIR"
green "  ✓ INTEL_COMPETITORS_FILE = $INTEL_COMPETITORS_FILE"

if [[ ! -f "$INTEL_COMPETITORS_FILE" ]]; then
  red "  ✗ $INTEL_COMPETITORS_FILE not found"
  exit 1
fi

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

HAS_ANTHROPIC=0
HAS_META=0
HAS_SERPAPI=0
[[ -n "${ANTHROPIC_API_KEY:-}" ]] && HAS_ANTHROPIC=1
[[ -n "${META_AD_LIBRARY_ACCESS_TOKEN:-}" ]] && HAS_META=1
[[ -n "${SERPAPI_API_KEY:-}" ]] && HAS_SERPAPI=1
[[ $HAS_ANTHROPIC -eq 1 ]] && green "  ✓ ANTHROPIC_API_KEY set"     || yellow "  ⚠ ANTHROPIC_API_KEY missing — vision analysis runs via Claude Code subagents (see header)"
[[ $HAS_META      -eq 1 ]] && green "  ✓ META_AD_LIBRARY_ACCESS_TOKEN set" || yellow "  ⚠ META token missing — competitor Meta ads sources will fail"
[[ $HAS_SERPAPI   -eq 1 ]] && green "  ✓ SERPAPI_API_KEY set (Google ATC for Wegmans)" || yellow "  ⚠ SERPAPI_API_KEY missing — Wegmans Google ATC source will fail"

# ---- output dir ----
DATE="$(date -u +%Y-%m-%d)"
REPORTS="reports/wegmans/${DATE}"
mkdir -p "$REPORTS"
green "  ✓ writing reports to: $REPORTS/"
echo

# ---- 2. init ----
bold "[2/9] init db (wegmans.db)"; rule
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
  yellow "  no ANTHROPIC_API_KEY — run vision analysis via Claude Code subagents"
  yellow "  (scripts/cc_vision_prep.py → subagents → scripts/cc_vision_write.py)"
  yellow "  which writes creatives.analysis_json BEFORE the readout/dashboard steps."
fi
echo

# ---- 6. per-brand readouts ----
bold "[6/9] per-brand creative readouts"; rule
mkdir -p "$REPORTS/by-brand"
.venv/bin/python - "$REPORTS/by-brand" "$DAYS" <<'PY'
import os, sys, subprocess, sqlite3, pathlib
out_dir = pathlib.Path(sys.argv[1])
days = sys.argv[2]
db = os.environ["INTEL_DB_PATH"]
with sqlite3.connect(db) as c:
    ids = [r[0] for r in c.execute("SELECT id FROM competitors ORDER BY id").fetchall()]
for cid in ids:
    target = out_dir / f"{cid}.md"
    print(f"  → {cid} → {target}")
    subprocess.run([".venv/bin/intel", "creative-readout",
                    "--competitor", cid, "--days", days, "--save", str(target)],
                   check=False)
PY
echo

# ---- 7. cross-set comparison ----
bold "[7/9] cross-set comparison"; rule
.venv/bin/intel creative-comparison --days "$DAYS" --save "$REPORTS/creative_comparison.md"
echo

# ---- 8. briefing ----
bold "[8/9] briefing"; rule
BRIEF="$REPORTS/briefing.md"
if [[ $HAS_ANTHROPIC -eq 1 ]]; then
  green "  using LLM-synthesized briefing"
  .venv/bin/intel brief --days "$DAYS" >/dev/null
else
  green "  using deterministic (no-LLM) briefing"
  .venv/bin/intel brief --days "$DAYS" --no-llm >/dev/null
fi
.venv/bin/python - "$BRIEF" <<'PY'
import os, sys, sqlite3
out = sys.argv[1]
db = os.environ["INTEL_DB_PATH"]
with sqlite3.connect(db) as c:
    row = c.execute(
        "SELECT title, body_md, created_at, scope FROM briefings ORDER BY id DESC LIMIT 1"
    ).fetchone()
if row:
    with open(out, "w") as f:
        f.write(f"# {row[0]}\n\n_created: {row[2]} · scope: {row[3]}_\n\n---\n\n{row[1]}")
    print(f"  wrote {out}")
PY
echo

# ---- 9. HTML dashboard ----
# Plain (Meta) set + the all-platform "with-google" set. The with-google set is
# where Wegmans' Google ATC ads show alongside the competitors' Meta ads — it's
# the one to share, since Wegmans (the subject brand) only appears there.
bold "[9/9] HTML dashboard"; rule
.venv/bin/intel dashboard    --out "$REPORTS/dashboard"    --days "$DAYS"
.venv/bin/intel dashboard --v2 --out "$REPORTS/dashboard-v2" --days "$DAYS"
.venv/bin/intel dashboard --platform all    --out "$REPORTS/with-google/dashboard"    --days "$DAYS"
.venv/bin/intel dashboard --platform all --v2 --out "$REPORTS/with-google/dashboard-v2" --days "$DAYS"
echo

# ---- summary ----
bold "done (WEGMANS)"; rule
echo "outputs:"
find "$REPORTS" -maxdepth 2 -type f | sort | sed 's/^/  /'
echo
green "view a report:"
echo "    open $REPORTS/with-google/dashboard/index.html  ← includes Wegmans (Google ATC)"
echo "    open $REPORTS/creative_comparison.md"
echo "    open $REPORTS/by-brand/wegmans.md"
echo "    open $REPORTS/briefing.md"
