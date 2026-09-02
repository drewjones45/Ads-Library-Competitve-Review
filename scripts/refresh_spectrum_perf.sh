#!/usr/bin/env bash
# One-shot refresh of the owned-Meta performance data for the Spectrum set.
# Mirrors refresh_jdsports_perf.sh: summary insights (+attribution) -> weekly
# series (sparklines) -> daily series (scale/kill timeline). Cooldowns between
# calls keep Meta's app-level rate limiter (403 "request limit reached") quiet.
#
# Portability note: refresh_jdsports_perf.sh hardcodes a macOS ROOT and
# .venv/bin. This one derives ROOT from its own location and picks the right
# venv layout, so it runs on Windows (Git Bash) and macOS/Linux alike.
#
# Usage:
#   ./scripts/refresh_spectrum_perf.sh                       # default 90d window
#   ./scripts/refresh_spectrum_perf.sh 2026-06-03 2026-09-01 # explicit window
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export INTEL_DB_PATH="$ROOT/data/spectrum.db"
export INTEL_DATA_DIR="$ROOT/data/spectrum_assets"
export INTEL_COMPETITORS_FILE="$ROOT/config/competitors_spectrum.yaml"
mkdir -p "$INTEL_DATA_DIR"

# venv layout differs by platform
if   [[ -x "$ROOT/.venv/Scripts/intel.exe" ]]; then INTEL="$ROOT/.venv/Scripts/intel.exe"
elif [[ -x "$ROOT/.venv/bin/intel"         ]]; then INTEL="$ROOT/.venv/bin/intel"
else echo "intel CLI not found in .venv — run: py -m venv .venv && .venv/Scripts/python -m pip install -e ." >&2; exit 1
fi

# load credentials (META_AD_LIBRARY_ACCESS_TOKEN drives the owned-account adapter too)
if [[ -f "$ROOT/.env" ]]; then set -a; . "$ROOT/.env"; set +a; fi
if [[ -z "${META_AD_LIBRARY_ACCESS_TOKEN:-}" ]]; then
  echo "META_AD_LIBRARY_ACCESS_TOKEN not set — owned-account ingest needs ads_read" >&2; exit 1
fi

UNTIL="${2:-$(date -u +%Y-%m-%d)}"
SINCE="${1:-$(date -u -d '90 days ago' +%Y-%m-%d 2>/dev/null || date -u -v-90d +%Y-%m-%d)}"
COOL=20

# account_id | competitor | human-name | previews-flag
# Spectrum Networks is included deliberately even though it has $0 lifetime
# spend and returns no insights rows: if it ever activates, this picks it up
# with no edit. Expect "0 ads" from it — that is correct, not a failure.
ACCOUNTS=(
  "144017769649506|spectrum|Spectrum Reach|--max-previews 30"
  "2144122622552367|spectrum|Spectrum Networks|--max-previews 30"
)

echo "############ SUMMARY INGEST ($SINCE -> $UNTIL) ############"
for row in "${ACCOUNTS[@]}"; do
  IFS='|' read -r acct comp name pflag <<< "$row"
  echo "=== $name ($acct) -> $comp ==="
  "$INTEL" perf-ingest --account "$acct" --competitor "$comp" \
    --account-name "$name" --since "$SINCE" --until "$UNTIL" $pflag
  echo "--- cooldown ${COOL}s ---"; sleep "$COOL"
done

echo "############ WEEKLY SERIES (sparklines) ############"
for row in "${ACCOUNTS[@]}"; do
  IFS='|' read -r acct comp name pflag <<< "$row"
  echo "=== $name ($acct) -> $comp ==="
  "$INTEL" perf-series --account "$acct" --competitor "$comp" \
    --since "$SINCE" --until "$UNTIL" --increment 7
  echo "--- cooldown ${COOL}s ---"; sleep "$COOL"
done

echo "############ DAILY SERIES (scale/kill timeline) ############"
for row in "${ACCOUNTS[@]}"; do
  IFS='|' read -r acct comp name pflag <<< "$row"
  echo "=== $name ($acct) -> $comp ==="
  "$INTEL" perf-series --account "$acct" --competitor "$comp" \
    --since "$SINCE" --until "$UNTIL" --increment 1
  echo "--- cooldown ${COOL}s ---"; sleep "$COOL"
done

echo "############ REFRESH COMPLETE ############"
