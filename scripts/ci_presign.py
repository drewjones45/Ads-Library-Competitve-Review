#!/usr/bin/env python3
"""ci_presign.py — presign every S3-hosted dashboard already built into dist/.

Meant to run right after `build_site.py`, before dist/ gets published — either
by Netlify's own Git-triggered build (once netlify.toml's `command` is updated
to chain this in), or by a CI pipeline (e.g. GitHub Actions) that does the
build+presign+publish itself. Either way this script only touches dist/, never
reports/ — s3_assets.py's own rewrite() refuses to write a presigned URL into
reports/ regardless, since that's committed to a public repo.

Reuses s3_assets.py's rewrite/verify directly rather than reimplementing them,
and deploy_netlify.sh now calls this too instead of carrying its own inline
loop — one implementation, one S3_TREES list, so the manual-deploy path and
whatever CI path gets wired up can never drift apart.

Signature scheme matches deploy_netlify.sh's existing choice: SigV2 ("s3"),
not the default SigV4 — SigV4 caps at 7 days, which would leave dist/ dead
between infrequent rebuilds. SigV2 is deprecated by AWS and could stop working
without notice, but is verified working against this bucket at up to 3 years;
see S3_ASSETS.md's presigned-URL-caveats section.

Fails soft, not loud, on missing config: no AWS_S3_BUCKET/AWS_S3_PREFIX set
means dist/ simply keeps whatever plain public-mode URLs build_site.py already
carried through (403 until a bucket policy exists) — never a hard CI failure
for an environment that hasn't been given S3 credentials at all.

Usage:
    python3 scripts/ci_presign.py                  # dist/, every S3_TREES entry
    python3 scripts/ci_presign.py --dist dist --dry-run
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import s3_assets as s3a  # noqa: E402 — reuse cmd_rewrite/cmd_verify, no duplication

# Deployments whose creative lives in S3 — same deliberate-act philosophy as
# deploy_netlify.sh's own S3_TREES (which now just imports this): a deployment
# migrating onto S3 gets a one-line addition here, on purpose, never picked up
# silently just because a dashboard happens to reference our bucket.
S3_TREES = ["spectrum", "trex", "philo", "jdsports", "wegmans", "edwardjones"]


def _presign_one(idx: Path, *, bucket: str, prefix: str, region: str,
                 expires: int, dry_run: bool) -> bool:
    """Rewrite + verify a single dist/<tree>/.../index.html. Returns True on
    success (or dry-run), False on any failure — never raises, since one bad
    dashboard shouldn't stop the rest from getting presigned."""
    rel = idx.relative_to(ROOT) if ROOT in idx.parents else idx
    print(f"==> presigning {rel}")
    if dry_run:
        return True
    try:
        rc = s3a.cmd_rewrite(argparse.Namespace(
            html=str(idx), bucket=bucket, prefix=prefix, region=region,
            mode="presign", signature="s3", expires=expires,
            base_url="", relink=False, out="", map_out="", allow_reports=False,
        ))
        if rc != 0:
            return False
        rc = s3a.cmd_verify(argparse.Namespace(
            html=str(idx), bucket=bucket, region=region, warn_days=30.0,
        ))
        return rc == 0
    except Exception as exc:  # noqa: BLE001 — surface it, keep going
        print(f"  FAILED {rel}: {exc}", file=sys.stderr)
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dist", default="dist")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    bucket = os.environ.get("AWS_S3_BUCKET")
    prefix = os.environ.get("AWS_S3_PREFIX")
    region = os.environ.get("AWS_REGION", "us-east-1")
    expires = int(os.environ.get("S3_PRESIGN_TTL", 31_536_000))  # 1 year, matches deploy_netlify.sh

    if not bucket or not prefix:
        print("AWS_S3_BUCKET / AWS_S3_PREFIX not set — skipping presign step.", file=sys.stderr)
        print("dist/ keeps whatever public-mode S3 URLs build_site.py carried through "
              "(403 until a bucket policy exists).", file=sys.stderr)
        return 0

    dist = (ROOT / args.dist) if not Path(args.dist).is_absolute() else Path(args.dist)
    if not dist.is_dir():
        sys.exit(f"not a directory: {dist} — run build_site.py first")

    signed = failed = 0
    for tree in S3_TREES:
        tree_dir = dist / tree
        if not tree_dir.is_dir():
            print(f"  {tree}: no {dist.name}/{tree}/ this run — nothing to presign")
            continue
        for idx in sorted(tree_dir.rglob("index.html")):
            ok = _presign_one(idx, bucket=bucket, prefix=prefix, region=region,
                              expires=expires, dry_run=args.dry_run)
            signed += ok
            failed += not ok

    print(f"\npresigned {signed} dashboard(s), {failed} failure(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
