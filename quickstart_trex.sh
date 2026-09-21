#!/usr/bin/env bash
#
# quickstart_trex.sh — run the full intel pipeline end-to-end for the TREX
# competitive set (decking). Fully isolated from the Bobs deployment.
#
# Isolation:
#   - INTEL_DB_PATH         → data/trex.db
#   - INTEL_DATA_DIR        → data/trex_assets/
#   - INTEL_COMPETITORS_FILE → config/competitors_trex.yaml
#   - reports                → reports/trex/<UTC-date>/
#
# Usage:
#   ./quickstart_trex.sh                  # full pipeline
#   ./quickstart_trex.sh --skip-ingest    # use existing data, just rebuild reports
#   ./quickstart_trex.sh --days 14        # widen the analysis window (default 7)

set -u

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

# ---- isolation env vars (the whole point of this wrapper) ----
export INTEL_DB_PATH="$ROOT/data/trex.db"
export INTEL_DATA_DIR="$ROOT/data/trex_assets"
export INTEL_COMPETITORS_FILE="$ROOT/config/competitors_trex.yaml"

mkdir -p "$INTEL_DATA_DIR"

# ---- args ----
SKIP_INGEST=0
DAYS=7
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-ingest) SKIP_INGEST=1; shift ;;
    --days)        DAYS="$2"; shift 2 ;;
    -h|--help)
      head -16 "$0" | grep -E '^#' | sed 's/^# \?//'
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
bold "[1/10] preflight (TREX deployment)"
rule
if [[ ! -x ".venv/bin/intel" ]]; then
  red "  ✗ .venv/bin/intel not found. Run:"
  echo "      python3.13 -m venv .venv && .venv/bin/pip install -e '.[browser]' && .venv/bin/playwright install chromium"
  exit 1
fi
green "  ✓ venv present"
green "  ✓ INTEL_DB_PATH         = $INTEL_DB_PATH"
green "  ✓ INTEL_DATA_DIR        = $INTEL_DATA_DIR"
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
[[ -n "${ANTHROPIC_API_KEY:-}" ]] && HAS_ANTHROPIC=1
[[ -n "${META_AD_LIBRARY_ACCESS_TOKEN:-}" ]] && HAS_META=1
[[ $HAS_ANTHROPIC -eq 1 ]] && green "  ✓ ANTHROPIC_API_KEY set"     || yellow "  ⚠ ANTHROPIC_API_KEY missing — vision + briefing will be limited"
[[ $HAS_META      -eq 1 ]] && green "  ✓ META_AD_LIBRARY_ACCESS_TOKEN set" || yellow "  ⚠ META token missing — Meta ads sources will fail"
[[ -n "${SERPAPI_API_KEY:-}" ]] && green "  ✓ SERPAPI_API_KEY set (Google ATC via API)" || yellow "  ⚠ SERPAPI_API_KEY missing — Google ads (if configured) fall back to a Playwright scrape"

# ---- output dir ----
DATE="$(date -u +%Y-%m-%d)"
REPORTS="reports/trex/${DATE}"
mkdir -p "$REPORTS"
green "  ✓ writing reports to: $REPORTS/"
echo

# ---- 2. sync db from S3 ----
# data/trex.db is no longer git-tracked (moved to S3, see S3_ASSETS.md) — on
# a machine that's never run this before, or one that's behind another
# operator's last audit run, this pulls the latest before init/ingest touch
# it. Skips gracefully (not a hard fail) if S3 isn't configured, so a
# local-only/offline run still works, same spirit as the ANTHROPIC/META
# token checks above.
bold "[2/10] sync db from S3"; rule
if [[ -n "${AWS_S3_BUCKET:-}" ]] && .venv/bin/python3 -c "import boto3" >/dev/null 2>&1; then
  .venv/bin/python3 scripts/export_db_to_s3.py sync --db "$INTEL_DB_PATH" \
    --bucket "$AWS_S3_BUCKET" --prefix "${AWS_S3_PREFIX:-outbound/competitive-intel}" \
    --region "${AWS_REGION:-us-east-1}"
else
  yellow "  skipped — AWS_S3_BUCKET not set or boto3 not installed (.venv/bin/pip install -e '.[s3,etl]')"
fi
echo

# ---- 3. init ----
bold "[3/10] init db (trex.db)"; rule
.venv/bin/intel init
echo

# ---- 4. ingest ----
bold "[4/10] ingest"; rule
if [[ $SKIP_INGEST -eq 1 ]]; then
  yellow "  skipped (--skip-ingest)"
else
  .venv/bin/intel ingest 2>&1 | tee "$REPORTS/ingest.log"
fi
echo

# ---- 5. capture landing pages ----
# Screenshot the on-brand pages ads send traffic to (needs ad link_urls from
# ingest above). The next step then analyzes these alongside ad creatives.
bold "[5/10] capture landing pages"; rule
if [[ $SKIP_INGEST -eq 1 ]]; then
  yellow "  skipped (--skip-ingest)"
else
  .venv/bin/intel capture-landing-pages 2>&1 | tee "$REPORTS/landing_capture.log"
fi
echo

# ---- 6. analyze creatives ----
bold "[6/10] vision-analyze creatives"; rule
if [[ $HAS_ANTHROPIC -eq 1 ]]; then
  .venv/bin/intel analyze-creatives 2>&1 | tee "$REPORTS/creative_analysis.log"
else
  yellow "  skipped — no ANTHROPIC_API_KEY"
fi
echo

# ---- 7. per-brand readouts ----
bold "[7/10] per-brand creative readouts"; rule
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

# ---- 8. cross-set comparison ----
bold "[8/10] cross-set comparison"; rule
.venv/bin/intel creative-comparison --days "$DAYS" --save "$REPORTS/creative_comparison.md"
echo

# ---- 9. briefing ----
bold "[9/10] briefing"; rule
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

# ---- 10. HTML dashboard ----
bold "[10/10] HTML dashboard"; rule
.venv/bin/intel dashboard --out "$REPORTS/dashboard" --days "$DAYS"
# Separate "with Google" report set (adds Google ATC ads: platform filter +
# Text-ads section). The Meta report above is unchanged; lands in with-google/.
.venv/bin/intel dashboard --platform all --out "$REPORTS/with-google/dashboard"    --days "$DAYS"
.venv/bin/intel dashboard --platform all --out "$REPORTS/with-google/dashboard-v2" --days "$DAYS" --v2
echo

# ---- summary ----
bold "done (TREX)"; rule
echo "outputs:"
find "$REPORTS" -maxdepth 2 -type f | sort | sed 's/^/  /'
echo
green "view a report:"
# Once this dashboard has been through scripts/s3_assets.py rewrite, its
# index.html holds S3 URLs and a local-path preview copy (index.local.html)
# sits beside it — that's the one that actually renders images on this
# machine. A fresh run has no such sibling yet, so fall back to index.html.
if [[ -f "$REPORTS/dashboard/index.local.html" ]]; then
  echo "    open $REPORTS/dashboard/index.local.html  ← local-path preview (images from disk)"
else
  echo "    open $REPORTS/dashboard/index.html        ← single-page HTML dashboard"
fi
echo "    open $REPORTS/creative_comparison.md"
echo "    open $REPORTS/by-brand/trex.md"
echo "    open $REPORTS/briefing.md"
