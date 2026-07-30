#!/usr/bin/env bash
# One-shot refresh of the owned-Meta performance data for the JD Sports set.
# Brings the reporting window current (today is the --until) across all four
# accounts: summary insights (+attribution) → weekly series (sparklines) →
# daily series (scale/kill timeline). Cooldowns between calls keep Meta's
# app-level rate limiter (the 403 "request limit reached") from tripping.
set -uo pipefail
ROOT="/Users/andrewjones/Documents/Ads Library Competitve Review"
cd "$ROOT"
export INTEL_DB_PATH="$ROOT/data/jdsports.db"
export INTEL_DATA_DIR="$ROOT/data/jdsports_assets"
INTEL="$ROOT/.venv/bin/intel"

SINCE="2026-04-30"
UNTIL="2026-07-29"
COOL=20

# account_id competitor human-name previews-flag
ACCOUNTS=(
  "263673744705629|jdsports|JD Sports US Brand & COOP|--max-previews 30"
  "399719270800712|jdsports|JD Sports - US|--max-previews 30"
  "27213418|finishline|Finish Line Official|--no-previews"
  "627413344530702|finishline|Finish Line Brand & COOP|--no-previews"
)

echo "############ SUMMARY INGEST ($SINCE → $UNTIL) ############"
for row in "${ACCOUNTS[@]}"; do
  IFS='|' read -r acct comp name pflag <<< "$row"
  echo "=== $name ($acct) → $comp ==="
  "$INTEL" perf-ingest --account "$acct" --competitor "$comp" \
    --account-name "$name" --since "$SINCE" --until "$UNTIL" $pflag
  echo "--- cooldown ${COOL}s ---"; sleep "$COOL"
done

echo "############ WEEKLY SERIES (sparklines) ############"
for row in "${ACCOUNTS[@]}"; do
  IFS='|' read -r acct comp name pflag <<< "$row"
  echo "=== $name ($acct) → $comp ==="
  "$INTEL" perf-series --account "$acct" --competitor "$comp" \
    --since "$SINCE" --until "$UNTIL" --increment 7
  echo "--- cooldown ${COOL}s ---"; sleep "$COOL"
done

echo "############ DAILY SERIES (scale/kill timeline) ############"
for row in "${ACCOUNTS[@]}"; do
  IFS='|' read -r acct comp name pflag <<< "$row"
  echo "=== $name ($acct) → $comp ==="
  "$INTEL" perf-series --account "$acct" --competitor "$comp" \
    --since "$SINCE" --until "$UNTIL" --increment 1
  echo "--- cooldown ${COOL}s ---"; sleep "$COOL"
done

echo "############ REFRESH COMPLETE ############"
