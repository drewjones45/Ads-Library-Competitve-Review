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
bold "[1/9] preflight (TREX deployment)"
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

# ---- output dir ----
DATE="$(date -u +%Y-%m-%d)"
REPORTS="reports/trex/${DATE}"
mkdir -p "$REPORTS"
green "  ✓ writing reports to: $REPORTS/"
echo

# ---- 2. init ----
bold "[2/9] init db (trex.db)"; rule
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

# ---- 8. HTML dashboard ----
bold "[9/9] HTML dashboard"; rule
.venv/bin/intel dashboard --out "$REPORTS/dashboard" --days "$DAYS"
echo

# ---- summary ----
bold "done (TREX)"; rule
echo "outputs:"
find "$REPORTS" -maxdepth 2 -type f | sort | sed 's/^/  /'
echo
green "view a report:"
echo "    open $REPORTS/dashboard/index.html        ← single-page HTML dashboard"
echo "    open $REPORTS/creative_comparison.md"
echo "    open $REPORTS/by-brand/trex.md"
echo "    open $REPORTS/briefing.md"
