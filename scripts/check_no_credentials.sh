#!/usr/bin/env bash
#
# check_no_credentials.sh — fail if anything tracked by git carries a credential.
#
# This repo is PUBLIC on GitHub, and the S3 workflow deliberately puts presigned
# URLs into the deployed site. A presigned URL embeds the AWS access key id, so
# the one rule that must hold is: presigned URLs live in dist/, never in git.
#
# The key id is not the secret half, but publishing an AKIA string to a public
# repo trips GitHub secret scanning, which notifies AWS, which can quarantine the
# key — an outage caused by a leak that was never dangerous on its own.
#
# scripts/s3_assets.py already refuses to presign into reports/. This is the
# backstop for every other way a credential could get staged.
#
# Usage:
#   ./scripts/check_no_credentials.sh            # scan tracked + staged files
#   ./scripts/check_no_credentials.sh --staged   # staged only (pre-commit hook)
#
# Install as a pre-commit hook:
#   ln -sf ../../scripts/check_no_credentials.sh .git/hooks/pre-commit
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

STAGED=0
[[ "${1:-}" == "--staged" ]] && STAGED=1

# AWS access key ids (AKIA/ASIA + 16), a presigned-URL signature parameter, and
# long Meta graph tokens. Deliberately narrow: this must not cry wolf, or it gets
# bypassed and stops protecting anything.
# The Meta pattern is anchored to an assignment on purpose. Bare `EAA[A-Za-z0-9]+`
# matches base64 image data — it fired on every strategy doc's embedded thumbnails
# and on captured third-party landing pages, none of which contain a credential.
# A check that reports ten false positives is a check people learn to skip.
PATTERNS=(
  '(AKIA|ASIA)[0-9A-Z]{16}'
  '[?&](X-Amz-Signature|Signature)=[A-Za-z0-9%+/]{20,}'
  '(access_token|ACCESS_TOKEN)["'"'"']?[[:space:]]*[:=][[:space:]]*["'"'"']?EAA[A-Za-z0-9]{80,}'
)

if [[ $STAGED -eq 1 ]]; then
  FILES=$(git diff --cached --name-only --diff-filter=ACM)
else
  FILES=$(git ls-files)
fi
[[ -z "$FILES" ]] && { echo "✓ nothing to scan"; exit 0; }

HITS=""
for pat in "${PATTERNS[@]}"; do
  # -I skips binaries; the DB and image files are tracked and would match nothing
  # useful anyway.
  # A file matching two patterns is still one problem file — collect, dedupe.
  HITS+=$(echo "$FILES" | tr '\n' '\0' | xargs -0 grep -IlE "$pat" 2>/dev/null)$'\n'
done
HITS=$(echo "$HITS" | sed '/^$/d' | sort -u)

if [[ -n "$HITS" ]]; then
  echo "✗ credential-shaped strings in files tracked by git:" >&2
  echo "$HITS" | sed 's/^/    /' >&2
  cat >&2 <<'MSG'

  This repo is public. Presigned S3 URLs belong in dist/ (built at deploy time),
  not in reports/ or anywhere else git tracks.

  To fix a report that was presigned by mistake:
    scripts/s3_assets.py rewrite --html <that file> \
      --bucket "$AWS_S3_BUCKET" --prefix "$AWS_S3_PREFIX" --mode public
MSG
  exit 1
fi

echo "✓ no credentials in tracked files ($(echo "$FILES" | wc -l | tr -d ' ') scanned)"
