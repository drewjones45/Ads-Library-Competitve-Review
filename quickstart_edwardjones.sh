#!/usr/bin/env bash
#
# quickstart_edwardjones.sh — run the full intel pipeline end-to-end for the
# EDWARD JONES (US) wealth-management competitive set: Edward Jones vs Ameriprise
# Financial, Merrill, Charles Schwab, Fidelity Investments and Wells Fargo.
#
# Fully isolated from the Bobs, TREX, Philo, Wegmans and JD Sports deployments —
# it shares no db, no asset dir, no competitor file and no reports dir with any.
#
# Isolation:
#   - INTEL_DB_PATH          → data/edwardjones.db
#   - INTEL_DATA_DIR         → data/edwardjones_assets/
#   - INTEL_COMPETITORS_FILE → config/competitors_edwardjones.yaml
#   - reports                → reports/edwardjones/<UTC-date>/
#
# ⚠ PAGE-ID GATE. Five of the six meta_ads sources ship with an EMPTY page_id and
# the sixth (Edward Jones, 202226521097) is client-supplied and UNVERIFIED. Step
# [0] below resolves and verifies them. Until that is done and the ids are pasted
# into the yaml, the Meta half of this deployment cannot run — by design, per the
# JD Sports incident where a client-supplied id was the UK page.
#
# ⚠ INTERPRETATION CAVEAT. Wells Fargo, Fidelity and Merrill run mixed-business
# Meta pages (retail banking / self-directed brokerage / 401k) while Edward Jones
# and Ameriprise are near pure-play wealth advisory. Raw ad-volume comparisons
# OVERSTATE those three as wealth competitors. See the CAVEAT block in
# config/competitors_edwardjones.yaml before quoting any volume number.
#
# Vision analysis note: this environment has NO ANTHROPIC_API_KEY, so the
# in-pipeline `intel analyze-creatives` step is a no-op. Creative vision analysis
# is instead produced by Claude Code subagents that read each asset and write
# schema-matched JSON straight into creatives.analysis_json. Run that step
# between [5] and [6].
#
# Usage:
#   ./quickstart_edwardjones.sh                  # full pipeline (ingest + reports)
#   ./quickstart_edwardjones.sh --skip-ingest    # use existing data, rebuild reports
#   ./quickstart_edwardjones.sh --reports-only   # alias for --skip-ingest
#   ./quickstart_edwardjones.sh --days 14        # widen the analysis window (default 7)
#   ./quickstart_edwardjones.sh --resolve-ids    # run ONLY the page-id resolver, then stop

set -u

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

# ---- isolation env vars (the whole point of this wrapper) ----
export INTEL_DB_PATH="$ROOT/data/edwardjones.db"
export INTEL_DATA_DIR="$ROOT/data/edwardjones_assets"
export INTEL_COMPETITORS_FILE="$ROOT/config/competitors_edwardjones.yaml"

mkdir -p "$INTEL_DATA_DIR"

# ---- args ----
SKIP_INGEST=0
RESOLVE_ONLY=0
DAYS=7
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-ingest|--reports-only) SKIP_INGEST=1; shift ;;
    --resolve-ids) RESOLVE_ONLY=1; shift ;;
    --days)        DAYS="$2"; shift 2 ;;
    -h|--help)
      grep -E '^#' "$0" | sed 's/^# \?//'
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
bold "[1/9] preflight (EDWARD JONES deployment)"
rule
if [[ ! -x ".venv/bin/intel" ]]; then
  red "  ✗ .venv/bin/intel not found. Run:"
  echo "      brew install python@3.13"
  echo "      python3.13 -m venv .venv && .venv/bin/pip install -e '.[browser]' && .venv/bin/playwright install chromium"
  exit 1
fi
# The venv shipped in this repo was built on another machine — catch a venv whose
# interpreter shebang no longer resolves, which fails with a confusing exec error.
if ! .venv/bin/intel --help >/dev/null 2>&1; then
  red "  ✗ .venv/bin/intel exists but will not execute (stale interpreter path?)."
  echo "      Rebuild: rm -rf .venv && python3.13 -m venv .venv \\"
  echo "               && .venv/bin/pip install -e '.[browser]' && .venv/bin/playwright install chromium"
  exit 1
fi
green "  ✓ venv present and executable"
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
[[ -n "${ANTHROPIC_API_KEY:-}" ]] && HAS_ANTHROPIC=1
[[ -n "${META_AD_LIBRARY_ACCESS_TOKEN:-}" ]] && HAS_META=1
[[ $HAS_ANTHROPIC -eq 1 ]] && green "  ✓ ANTHROPIC_API_KEY set"     || yellow "  ⚠ ANTHROPIC_API_KEY missing — vision analysis runs via Claude Code subagents (see header)"
[[ $HAS_META      -eq 1 ]] && green "  ✓ META_AD_LIBRARY_ACCESS_TOKEN present" || yellow "  ⚠ META token missing"
yellow "  note: every meta_ads source here uses method: scrape, which needs NO Meta"
yellow "        token (pure Playwright). An expired token does not block this run."

# ---- 0. page-id gate ----
bold "[0] Meta page_id gate"; rule
MISSING=$(.venv/bin/python - <<'PY'
import os, yaml
cfg = os.environ["INTEL_COMPETITORS_FILE"]
d = yaml.safe_load(open(cfg))
missing = []
for c in d.get("competitors", []):
    for s in c.get("sources", []):
        if s.get("type") == "meta_ads" and not str(s.get("page_id") or "").strip():
            missing.append(c["id"])
print(" ".join(missing))
PY
)
if [[ -n "${MISSING// /}" ]]; then
  yellow "  ⚠ unresolved page_id for: $MISSING"
  echo   "    resolve + verify them with:"
  echo   "      .venv/bin/python scripts/resolve_meta_page_ids.py \\"
  echo   "        charlesschwab merrilllynch wellsfargo fidelityinvestments ameriprisefinancial"
  echo   "    then paste the VERIFIED ids into $INTEL_COMPETITORS_FILE"
  echo
  yellow "    also verify the client-supplied Edward Jones id (202226521097):"
  echo   "      .venv/bin/python scripts/resolve_meta_page_ids.py --verify-only 202226521097"
  echo   "      .venv/bin/python scripts/resolve_meta_page_ids.py edwardjones   # cross-check"
  echo
  yellow "    Meta sources for those brands will FAIL this run (meta_ads.py:235)."
  yellow "    Website sources still ingest normally, so the run continues."
else
  green "  ✓ all meta_ads sources have a page_id"
fi
if [[ $RESOLVE_ONLY -eq 1 ]]; then
  bold "--resolve-ids given; stopping before ingest."
  exit 0
fi
echo

# ---- output dir ----
DATE="$(date -u +%Y-%m-%d)"
REPORTS="reports/edwardjones/${DATE}"
mkdir -p "$REPORTS"
green "  ✓ writing reports to: $REPORTS/"
echo

# ---- 2. init ----
bold "[2/9] init db (edwardjones.db)"; rule
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
  yellow "  (writes creatives.analysis_json) BEFORE the readout/dashboard steps."
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

# NOTE: no google_ads sources are wired for this set — SerpApi ATC returned no
# advertiser records for any of the six brands on 2026-09-02 (see the yaml header).
# The --platform all dashboards are therefore omitted; add them once ATC resolves.

# ---- 9. HTML dashboard ----
bold "[9/9] HTML dashboard"; rule
.venv/bin/intel dashboard    --out "$REPORTS/dashboard"    --days "$DAYS"
.venv/bin/intel dashboard --v2 --out "$REPORTS/dashboard-v2" --days "$DAYS"
echo

# ---- summary ----
bold "done (EDWARD JONES)"; rule
echo "outputs:"
find "$REPORTS" -maxdepth 2 -type f | sort | sed 's/^/  /'
echo
green "view a report:"
echo "    open $REPORTS/dashboard/index.html        ← single-page HTML dashboard"
echo "    open $REPORTS/creative_comparison.md"
echo "    open $REPORTS/by-brand/edwardjones.md"
echo "    open $REPORTS/briefing.md"
