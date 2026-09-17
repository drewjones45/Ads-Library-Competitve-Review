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

# ---------------------------------------------------------------- presign step
# Only the DEPLOYED copy gets presigned URLs. reports/ stays on credential-free
# public-mode URLs because it is committed to a public repo (s3_assets.py refuses
# to presign into reports/ for that reason), and because those URLs become the
# permanent answer if a bucket policy ever lands.
#
# Presigning here also means every deploy resets the expiry clock, so the site
# cannot age out while it is being maintained.
#
# SCOPE: each deployment migrated onto S3-hosted creative gets added here
# explicitly — turning it on for a deployment stays a deliberate act, not
# something build_site.py silently picks up because a dashboard happens to
# reference an https:// URL. TREX added 2026-09-16 (data/trex_assets/
# untracked from git; see .gitignore's data/*_assets/ rule).
S3_TREES=("spectrum" "trex")

if [[ -n "${AWS_ACCESS_KEY_ID:-}" && -n "${AWS_S3_BUCKET:-}" ]]; then
  PY="$ROOT/.venv/bin/python"; [[ -x "$PY" ]] || PY="python3"
  for tree in "${S3_TREES[@]}"; do
    while IFS= read -r page; do
      echo "==> presigning $(realpath --relative-to="$ROOT" "$page" 2>/dev/null || echo "$page")"
      "$PY" "$ROOT/scripts/s3_assets.py" rewrite         --html "$page"         --bucket "$AWS_S3_BUCKET" --prefix "${AWS_S3_PREFIX:?}"         --region "${AWS_REGION:-us-east-1}"         --mode presign --signature "${S3_SIGNATURE:-s3}"         --expires "${S3_PRESIGN_TTL:-31536000}"
      "$PY" "$ROOT/scripts/s3_assets.py" verify         --html "$page" --bucket "$AWS_S3_BUCKET" --region "${AWS_REGION:-us-east-1}"
    done < <(find "$ROOT/dist/$tree" -name index.html 2>/dev/null)
  done
else
  echo "==> AWS creds or AWS_S3_BUCKET unset — skipping presign step." >&2
  echo "    dist/ keeps public-mode S3 URLs, which 403 until a bucket policy exists." >&2
fi

if ! command -v netlify >/dev/null 2>&1; then
  echo
  echo "netlify CLI not found. Either:"
  echo "  • install it:  npm install -g netlify-cli  &&  netlify login"
  echo "  • or drag-and-drop the dist/ folder onto https://app.netlify.com/drop"
  exit 1
fi

# --no-build is essential, not tidiness. netlify.toml declares a build `command`
# for the git-CI path, and the CLI runs it again at deploy time — which would
# regenerate dist/ from reports/ and silently throw away the presigned URLs this
# script just wrote, publishing a site whose every image 403s.
if [[ $PROD -eq 1 ]]; then
  echo "==> deploying to PRODUCTION"
  netlify deploy --dir dist --prod --no-build ${DEPLOY_ARGS[@]+"${DEPLOY_ARGS[@]}"}
else
  echo "==> draft deploy (preview URL; add --prod to publish)"
  netlify deploy --dir dist --no-build ${DEPLOY_ARGS[@]+"${DEPLOY_ARGS[@]}"}
fi
