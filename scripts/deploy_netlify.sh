#!/usr/bin/env bash
#
# deploy_netlify.sh — build the static dashboard site and deploy it to Netlify.
#
# The dashboards reference local, gitignored assets (data/*_assets/), so the
# site MUST be built on a machine that has that data (i.e. here), then the
# resulting dist/ folder is uploaded. This does both.
#
# Prereqs (one-time):
#   npm install -g netlify-cli      # or: brew install netlify-cli
#   netlify login                   # opens a browser to authorize
#
# Usage:
#   ./scripts/deploy_netlify.sh                 # draft deploy (preview URL)
#   ./scripts/deploy_netlify.sh --prod          # publish to the production URL
#   ./scripts/deploy_netlify.sh --prod --all    # include every report date
#
# First-ever deploy: run `netlify init` (or `netlify sites:create`) once to
# create/link the site, then re-run this.

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PROD=0
BUILD_ARGS=()
DEPLOY_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --prod) PROD=1; shift ;;
    --all)  BUILD_ARGS+=(--all); shift ;;
    *) DEPLOY_ARGS+=("$1"); shift ;;
  esac
done

echo "==> building dist/ (scripts/build_site.py ${BUILD_ARGS[*]:-})"
python3 scripts/build_site.py ${BUILD_ARGS[@]+"${BUILD_ARGS[@]}"}

if ! command -v netlify >/dev/null 2>&1; then
  echo
  echo "netlify CLI not found. Either:"
  echo "  • install it:  npm install -g netlify-cli  &&  netlify login"
  echo "  • or drag-and-drop the dist/ folder onto https://app.netlify.com/drop"
  exit 1
fi

if [[ $PROD -eq 1 ]]; then
  echo "==> deploying to PRODUCTION"
  netlify deploy --dir dist --prod ${DEPLOY_ARGS[@]+"${DEPLOY_ARGS[@]}"}
else
  echo "==> draft deploy (preview URL; add --prod to publish)"
  netlify deploy --dir dist ${DEPLOY_ARGS[@]+"${DEPLOY_ARGS[@]}"}
fi
