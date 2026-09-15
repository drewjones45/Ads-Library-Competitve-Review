#!/usr/bin/env python3
"""s3_assets.py — host dashboard creative on S3 instead of shipping it in the repo.

The dashboards `intel dashboard` / `intel perf-dashboard` emit reference creative
by ABSOLUTE filesystem path (whatever the generating machine used), and the files
themselves live in the gitignored data/*_assets/ tree. That means a dashboard only
renders its thumbnails on the machine that built it, and `build_site.py` has to
copy every referenced byte into dist/ for a Netlify deploy.

This script breaks that coupling:

    upload   mirror a local data/<name>_assets tree into an S3 prefix
    rewrite  point a dashboard's asset refs at those S3 objects
    verify   fetch every rewritten URL and report the HTTP status
    prune    list (or delete) objects under the prefix nothing references

Objects are stored CONTENT-ADDRESSED, at `<prefix>/by-hash/<ab>/<sha256>.<ext>`
rather than under a mirror of their local path. That is not a cosmetic choice —
the same bytes appear at many local paths, so a path mirror stores them many
times: across the six deployments here, 20,131 asset files hold only 10,002
distinct blobs (34.5%, 624 MB, of pure duplication), and spectrum alone is 93
files for 51 distinct blobs. Hashing the content collapses those to one object
each, and makes re-running a report a no-op for every asset whose bytes did not
change, no matter how the report reorganises its paths.

The trade is that keys are no longer human-browsable, so `upload` also writes a
`manifest/<tree>.json` next to them mapping each local relative path to its key.

Two URL modes, because they have very different operational costs:

  --mode public   https://<bucket>.s3.<region>.amazonaws.com/<key>
                  Permanent, cacheable, no credentials in the HTML. Requires the
                  bucket to allow anonymous s3:GetObject on the prefix (a bucket
                  policy — ACLs are disabled on modern buckets).
  --mode presign  the same object with a query signature appended. Works against
                  a fully private bucket and needs no policy change, so it is the
                  only mode that works out of the box — but the URLs expire, and
                  when they do the dashboard's images go 403 until it is rewritten.
                  --signature picks the scheme, and the scheme sets the ceiling:
                    s3v4 (default) — current, supported, expiry capped at 7 days.
                    s3             — legacy SigV2. Accepts any expiry (a year is
                                     fine) and still works on buckets created
                                     before 2020-06-24, but AWS has deprecated it
                                     and it can stop working without notice.
                  Note the signature embeds the access key ID, so a presigned
                  dashboard published to a public URL exposes that key ID (not the
                  secret). Prefer --mode public for anything client-facing.

  --base-url      overrides the host entirely (e.g. a CloudFront distribution in
                  front of the bucket). Use with --mode public.

Credentials come from the standard boto3 chain (AWS_ACCESS_KEY_ID /
AWS_SECRET_ACCESS_KEY, ~/.aws/credentials, instance role...). Nothing is read
from or written to the repo.

Examples:
  export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=...
  python3 scripts/s3_assets.py upload  --local-dir data/spectrum_assets \
      --bucket next-ext-commerce-us-east-1 --prefix outbound/competitive-intel
  python3 scripts/s3_assets.py rewrite --html reports/spectrum/2026-09-01/performance-dashboard/index.html \
      --bucket next-ext-commerce-us-east-1 --prefix outbound/competitive-intel --mode presign
  python3 scripts/s3_assets.py verify  --html reports/spectrum/2026-09-01/performance-dashboard/index.html
"""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import re
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Media extensions the dashboards can reference. Kept in sync with build_site.py's
# ASSET_RE so the two agree on what counts as an asset.
EXTS = ("jpg", "jpeg", "png", "webp", "gif", "mp4", "svg")

# A quoted string somewhere inside the HTML that names a file under a
# data/<something>_assets/ tree. Matches both slash flavours because the refs are
# baked in by whichever OS generated the dashboard: a Windows run leaves
# "C:\\Users\\...\\data\\spectrum_assets\\...\\preview.png" (doubled inside the
# embedded JSON), a POSIX run leaves "/Users/.../data/spectrum_assets/...".
REF_RE = re.compile(
    r"""(["'])((?:[^"']*?[\\/])?data[\\/]{1,2}[^"']*?_assets[\\/]{1,2}[^"']+?\.(?:%s))\1"""
    % "|".join(EXTS),
    re.IGNORECASE,
)

# Already-rewritten refs, for `verify` and for keeping `rewrite` idempotent.
URL_RE = re.compile(r"""(["'])(https://[^"']+?\.(?:%s)(?:\?[^"']*)?)\1""" % "|".join(EXTS), re.IGNORECASE)

# An https URL still pointing at the retired path-mirrored layout, i.e. one whose
# key contains `<tree>_assets/<path under it>`. Used by `rewrite --relink` to move
# an already-published dashboard onto content-addressed keys, so pruning the old
# layout does not silently break dashboards from earlier runs.
LEGACY_URL_RE = re.compile(
    r"""(["'])(https://[^"']*?/([A-Za-z0-9_.-]+_assets/[^"'?]+?\.(?:%s))(?:\?[^"']*)?)\1"""
    % "|".join(EXTS),
    re.IGNORECASE,
)

# A content-addressed key can never point at different bytes, so it is safe to
# tell caches to keep it forever.
CACHE_CONTROL = "public, max-age=31536000, immutable"
MAX_PRESIGN_V4 = 604800  # SigV4 rejects anything longer with a 400
HASH_SEG = "by-hash"


def content_key(path: Path, prefix: str) -> tuple[str, str]:
    """(sha256, key) for a local file. The two-char fan-out directory keeps any
    single S3 listing page small enough to page through comfortably.

    `prefix` is expected to already be client-scoped (see `client_prefix`) — this
    function itself has no notion of client, it just hashes and joins."""
    h = hashlib.sha256(path.read_bytes()).hexdigest()
    ext = path.suffix.lower()
    return h, f"{prefix}/{HASH_SEG}/{h[:2]}/{h}{ext}"


def client_slug(assets_dirname: str) -> str:
    """'spectrum_assets' -> 'spectrum'. The deployment-naming convention used
    throughout this repo (data/<client>_assets, INTEL_DATA_DIR, ...) already
    carries the client name — this just strips the trailing '_assets'."""
    return assets_dirname[:-len("_assets")] if assets_dirname.endswith("_assets") else assets_dirname


def client_prefix(base_prefix: str, assets_dirname: str) -> str:
    """Join a base prefix with the client segment derived from a data/<x>_assets
    dirname, e.g. ('outbound/competitive-intel', 'spectrum_assets') ->
    'outbound/competitive-intel/spectrum'.

    Scoping keys per client is deliberate: without it, `prune` (which has to
    reconstruct every client's wanted-set from local data/*_assets trees to know
    what's still referenced) has no way to tell one client's objects from
    another's under a shared flat prefix."""
    return f"{base_prefix.strip('/')}/{client_slug(assets_dirname)}"


def _client(region: str, signature: str = "s3v4"):
    """S3 client with the signing scheme pinned.

    Worth pinning explicitly: botocore picks SigV2 on its own for us-east-1
    presigned URLs, which silently changes the expiry ceiling (SigV4 caps at 7
    days, SigV2 does not) and quietly puts a deprecated scheme on the wire.
    """
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        sys.exit("boto3 not installed — run: .venv/bin/pip install -e '.[s3]'")
    return boto3.client("s3", region_name=region, config=Config(signature_version=signature))


def rel_under_data(raw: str) -> str | None:
    """'C:\\\\...\\\\data\\\\spectrum_assets\\\\x\\\\y.png' -> 'spectrum_assets/x/y.png'.

    The portable identity of an asset is everything after the LAST 'data/'
    segment — same rule build_site.py uses, so both tools land on the same
    relative path regardless of which machine wrote the reference.
    """
    norm = raw.replace("\\\\", "\\").replace("\\", "/")
    idx = norm.rfind("/data/")
    if idx >= 0:
        return norm[idx + len("/data/") :]
    if norm.startswith("data/"):
        return norm[len("data/") :]
    return None


def scan(html_path: Path, *, relink: bool = False) -> tuple[str, dict[str, str]]:
    """Return (html_text, {raw_ref_as_written: rel_under_data}).

    With `relink`, https URLs from the retired path-mirrored layout are picked up
    too and treated exactly like local refs — same relative path, so they resolve
    to the same bytes and therefore the same content key.
    """
    text = html_path.read_text(encoding="utf-8")
    refs: dict[str, str] = {}
    for m in REF_RE.finditer(text):
        raw = m.group(2)
        if raw in refs:
            continue
        rel = rel_under_data(raw)
        if rel:
            refs[raw] = rel
    if relink:
        for m in LEGACY_URL_RE.finditer(text):
            raw, rel = m.group(2), m.group(3)
            if raw not in refs:
                refs[raw] = rel
    return text, refs


# --------------------------------------------------------------------------- upload


def cmd_upload(args) -> int:
    local = (ROOT / args.local_dir) if not Path(args.local_dir).is_absolute() else Path(args.local_dir)
    if not local.is_dir():
        sys.exit(f"not a directory: {local}")
    files = [p for p in sorted(local.rglob("*")) if p.is_file() and p.suffix.lower().lstrip(".") in EXTS]
    if not files:
        sys.exit(f"no media files under {local}")

    s3 = _client(args.region)
    prefix = client_prefix(args.prefix, local.name)

    # Hash first, upload second. Files that share bytes collapse onto one key here,
    # before anything touches the network — which is the whole point: the same
    # creative reused across ads must not become several S3 objects.
    by_key: dict[str, Path] = {}
    manifest: dict[str, str] = {}
    for p in files:
        _, key = content_key(p, prefix)
        by_key.setdefault(key, p)
        manifest[p.relative_to(local).as_posix()] = key

    local_bytes = sum(p.stat().st_size for p in files)
    uniq_bytes = sum(p.stat().st_size for p in by_key.values())
    dupes = len(files) - len(by_key)

    # One listing beats a HEAD per file. Keys are immutable by construction, so
    # "the key exists" is the whole check — no ETag comparison needed.
    existing: set[str] = set()
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=args.bucket, Prefix=f"{prefix}/{HASH_SEG}/"):
        for o in page.get("Contents", []):
            existing.add(o["Key"])

    todo = {k: v for k, v in by_key.items() if k not in existing}
    print(f"{len(files)} local file(s), {len(by_key)} distinct "
          f"({dupes} duplicate{'' if dupes == 1 else 's'} collapsed, "
          f"{(local_bytes - uniq_bytes) / 1e6:.1f} MB saved)")
    print(f"{len(by_key) - len(todo)} already in S3, {len(todo)} to upload")

    if args.dry_run:
        for k, p in list(todo.items())[:10]:
            print(f"  would put {k}  ({p.stat().st_size} B)  <- {p.name}")
        return 0

    put = failed = 0

    def send(item):
        nonlocal put, failed
        key, p = item
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        try:
            s3.put_object(
                Bucket=args.bucket, Key=key, Body=p.read_bytes(),
                ContentType=ctype, CacheControl=CACHE_CONTROL,
            )
            put += 1
        except Exception as exc:  # noqa: BLE001 — surface the AWS error verbatim
            failed += 1
            print(f"  FAIL {key}: {exc}", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(send, todo.items()))

    # Content-addressed keys say nothing about what they hold. The manifest is how
    # a human (or a later prune) gets from "spectrum ad 1234's preview" to a key.
    mkey = f"{prefix}/manifest/{local.name}.json"
    try:
        s3.put_object(
            Bucket=args.bucket, Key=mkey,
            Body=json.dumps(manifest, indent=1, sort_keys=True).encode(),
            ContentType="application/json", CacheControl="no-cache",
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  WARNING: manifest not written: {exc}", file=sys.stderr)

    print(f"uploaded {put}, failed {failed}  "
          f"({uniq_bytes / 1e6:.1f} MB unique of {local_bytes / 1e6:.1f} MB on disk)")
    print(f"prefix:   s3://{args.bucket}/{prefix}/{HASH_SEG}/")
    print(f"manifest: s3://{args.bucket}/{mkey}")
    return 1 if failed else 0


# -------------------------------------------------------------------------- rewrite


def cmd_rewrite(args) -> int:
    html_path = (ROOT / args.html) if not Path(args.html).is_absolute() else Path(args.html)
    text, refs = scan(html_path, relink=args.relink)

    base_prefix = args.prefix.strip("/")
    s3 = _client(args.region, args.signature) if args.mode == "presign" else None
    host = args.base_url.rstrip("/") if args.base_url else f"https://{args.bucket}.s3.{args.region}.amazonaws.com"

    def url_for(key: str) -> str:
        if args.mode == "presign":
            return s3.generate_presigned_url(
                "get_object", Params={"Bucket": args.bucket, "Key": key}, ExpiresIn=args.expires
            )
        return f"{host}/{key}"

    # Re-mode pass: URLs already pointing at our own content-addressed keys are
    # reissued in whatever --mode now asks for. Switching is a real workflow, not a
    # repair — presign to view a dashboard before the bucket policy exists, public
    # to commit one, since a presigned URL carries the access key id and expires.
    # The client segment between base_prefix and HASH_SEG is matched generically
    # (one path segment, any name) rather than pinned to a specific client, so a
    # dashboard is still recognised as "ours" regardless of which client it is.
    own = re.compile(
        r"""(["'])(https://[^"']*?/(%s/[^/"']+/%s/[0-9a-f]{2}/[0-9a-f]{64}\.(?:%s))(?:\?[^"']*)?)\1"""
        % (re.escape(base_prefix), re.escape(HASH_SEG), "|".join(EXTS)),
        re.IGNORECASE,
    )
    remoded = 0
    for m in own.finditer(text):
        raw, key = m.group(2), m.group(3)
        new_url = url_for(key)
        if new_url != raw:
            text = text.replace(f'"{raw}"', f'"{new_url}"').replace(f"'{raw}'", f"'{new_url}'")
            remoded += 1
    if remoded:
        dest0 = Path(args.out) if args.out else html_path
        if not dest0.is_absolute():
            dest0 = ROOT / dest0
        dest0.parent.mkdir(parents=True, exist_ok=True)
        dest0.write_text(text, encoding="utf-8")
        print(f"re-moded {remoded} existing S3 URL(s) -> {args.mode} in {dest0}")

    if not refs:
        if not remoded:
            already = len(set(m.group(2) for m in URL_RE.finditer(text)))
            print(f"no local asset refs in {html_path}"
                  + (f" ({already} already point at URLs)" if already else ""))
        elif args.mode == "presign":
            _presign_note(args)
        return 0

    # The key is a hash of the bytes, so the local file has to be readable here.
    # A ref we cannot resolve is SKIPPED rather than rewritten: emitting a URL for
    # a key that was never uploaded would swap a visibly-broken local path for an
    # invisibly-broken remote one.
    replacements: dict[str, str] = {}
    unresolved: list[str] = []
    keys_by_ref: dict[str, str] = {}
    for raw, rel in sorted(refs.items(), key=lambda kv: kv[1]):
        src = ROOT / "data" / rel
        if not src.is_file():
            unresolved.append(rel)
            continue
        # rel is "<client>_assets/..." (see rel_under_data) — the client segment
        # of the key comes from the SAME tree each ref actually lives under, not
        # from a single assumed client, so one dashboard's refs resolve correctly
        # even if (hypothetically) they spanned more than one _assets tree.
        tree_name = rel.split("/", 1)[0]
        prefix = client_prefix(base_prefix, tree_name)
        _, key = content_key(src, prefix)
        keys_by_ref[raw] = key
        if args.mode == "presign":
            url = s3.generate_presigned_url(
                "get_object", Params={"Bucket": args.bucket, "Key": key}, ExpiresIn=args.expires
            )
        else:
            url = f"{host}/{key}"
        replacements[raw] = url

    # The refs live inside an embedded `var ADS=[...]` JSON blob, so the ref text in
    # the file carries JSON escaping (a single backslash written as \\). Substituting
    # the exact matched span keeps that intact for anything we do not touch, and the
    # replacement URL has no characters that need JSON escaping.
    out = text  # already carries any re-moded URLs from the pass above
    for raw, url in replacements.items():
        out = out.replace(f'"{raw}"', f'"{url}"').replace(f"'{raw}'", f"'{url}'")

    dest = Path(args.out) if args.out else html_path
    if not dest.is_absolute():
        dest = ROOT / dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(out, encoding="utf-8")

    missing = unresolved
    distinct = len(set(keys_by_ref.values()))
    print(f"rewrote {len(replacements)} refs -> {args.mode} URLs in {dest}"
          f"  ({distinct} distinct object{'' if distinct == 1 else 's'})")
    if args.mode == "presign":
        _presign_note(args)
    if missing:
        print(f"  WARNING: {len(missing)} ref(s) left as-is — no local file to hash, "
              f"so nothing was uploaded for them:")
        for m in missing[:10]:
            print(f"    {m}")
    if args.map_out:
        mp = Path(args.map_out)
        mp.write_text(json.dumps({refs[k]: v for k, v in keys_by_ref.items()}, indent=2), encoding="utf-8")
        print(f"  wrote key->url map to {mp}")
    return 0


def _presign_note(args) -> None:
    import datetime
    when = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=args.expires)
    print(f"  NOTE: {args.signature} presigned URLs expire {when:%Y-%m-%d %H:%M UTC} "
          f"({args.expires / 86400:.1f} days) — re-run to refresh")


# --------------------------------------------------------------------------- verify


def cmd_verify(args) -> int:
    html_path = (ROOT / args.html) if not Path(args.html).is_absolute() else Path(args.html)
    text = html_path.read_text(encoding="utf-8")
    urls = sorted({m.group(2) for m in URL_RE.finditer(text)})
    if not urls:
        print(f"no https asset URLs in {html_path} — has it been rewritten?")
        return 1

    def head(u: str):
        # S3 rejects a presigned GET signature used on a HEAD, so fetch the object
        # and read a single byte instead of issuing a real HEAD.
        req = urllib.request.Request(u, headers={"Range": "bytes=0-0"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return u, r.status, r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            return u, e.code, ""
        except Exception as e:  # noqa: BLE001
            return u, 0, str(e)[:60]

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(head, urls))

    ok = [r for r in results if r[1] in (200, 206)]
    bad = [r for r in results if r[1] not in (200, 206)]
    print(f"{len(ok)}/{len(results)} asset URLs fetched OK")
    for u, code, info in bad[:10]:
        print(f"  {code or 'ERR'}  {u.split('?')[0]}  {info}")
    if ok:
        print(f"  sample content-type: {ok[0][2]}")
    return 1 if bad else 0


def cmd_prune(args) -> int:
    """Report (and optionally delete) objects under the prefix that nothing needs.

    Two things become garbage over time: keys from the old path-mirrored layout,
    and hashes whose bytes no longer exist locally because a report was rebuilt
    with different creative. Both are found the same way — take every object under
    the prefix, subtract the content keys of every local asset tree, and whatever
    is left is unreferenced.

    Deletion is opt-in (`--delete`). Everything here is reconstructible from local
    files by re-running `upload`, but this is a shared bucket, so the default is to
    say what would go rather than to go do it.
    """
    s3 = _client(args.region)
    base_prefix = args.prefix.strip("/")

    # Each local tree is scoped to its own client segment (client_prefix), same
    # as upload/rewrite — otherwise every client's wanted-set would collide under
    # one flat namespace and this could never tell them apart.
    wanted: set[str] = set()
    trees = 0
    for tree in sorted((ROOT / "data").glob("*_assets")):
        trees += 1
        full_prefix = client_prefix(base_prefix, tree.name)
        for f in tree.rglob("*"):
            if f.is_file() and f.suffix.lower().lstrip(".") in EXTS:
                wanted.add(content_key(f, full_prefix)[1])
    print(f"{len(wanted)} distinct object(s) referenced by {trees} local asset tree(s)")

    # size + last-modified per object — LastModified doesn't change the key
    # scheme, it's just carried through from the listing for the report below.
    live: dict[str, dict] = {}
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=args.bucket, Prefix=f"{base_prefix}/"):
        for o in page.get("Contents", []):
            live[o["Key"]] = {"size": o["Size"], "modified": o.get("LastModified")}
    print(f"{len(live)} object(s) currently under s3://{args.bucket}/{base_prefix}/")

    # The manifests describe the store; they are not themselves assets. Matched
    # as a path segment rather than a prefix startswith, since a manifest can now
    # sit under any client's sub-prefix (<base_prefix>/<client>/manifest/...).
    orphans = {k: v for k, v in live.items()
               if k not in wanted and "/manifest/" not in k}
    legacy = {k: v for k, v in orphans.items() if f"/{HASH_SEG}/" not in k}
    stale = {k: v for k, v in orphans.items() if f"/{HASH_SEG}/" in k}
    orphan_bytes = sum(v["size"] for v in orphans.values())
    print(f"\n{len(orphans)} unreferenced ({orphan_bytes / 1e6:.1f} MB):")
    print(f"  {len(legacy)} from the old path-mirrored layout "
          f"({sum(v['size'] for v in legacy.values()) / 1e6:.1f} MB)")
    print(f"  {len(stale)} content keys with no local source "
          f"({sum(v['size'] for v in stale.values()) / 1e6:.1f} MB)")
    for k, v in list(orphans.items())[:8]:
        mod = v["modified"]
        when = f"{mod:%Y-%m-%d}" if mod else "unknown date"
        print(f"    {k}  ({v['size'] / 1e6:.2f} MB, modified {when})")
    if len(orphans) > 8:
        print(f"    … and {len(orphans) - 8} more")

    if not orphans or not args.delete:
        if orphans:
            print("\nnothing deleted — pass --delete to remove these")
        return 0

    keys = list(orphans)
    deleted = 0
    for i in range(0, len(keys), 1000):  # delete_objects caps at 1000 per call
        batch = [{"Key": k} for k in keys[i:i + 1000]]
        r = s3.delete_objects(Bucket=args.bucket, Delete={"Objects": batch, "Quiet": True})
        deleted += len(batch) - len(r.get("Errors", []))
        for e in r.get("Errors", [])[:5]:
            print(f"  FAIL {e.get('Key')}: {e.get('Message')}", file=sys.stderr)
    print(f"\ndeleted {deleted}/{len(keys)} object(s)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--bucket", required=True)
        p.add_argument("--prefix", default="outbound/competitive-intel",
                        help="base prefix, shared across clients — a client segment "
                             "(from --local-dir's <client>_assets name, or from each "
                             "ref's own tree for rewrite/prune) is appended automatically")
        p.add_argument("--region", default="us-east-1")

    up = sub.add_parser("upload", help="mirror a local assets tree into S3")
    common(up)
    up.add_argument("--local-dir", required=True, help="e.g. data/spectrum_assets")
    up.add_argument("--workers", type=int, default=8)
    up.add_argument("--dry-run", action="store_true")
    up.set_defaults(func=cmd_upload)

    rw = sub.add_parser("rewrite", help="point a dashboard's asset refs at S3")
    common(rw)
    rw.add_argument("--html", required=True)
    rw.add_argument("--mode", choices=("public", "presign"), default="public")
    rw.add_argument("--expires", type=int, default=MAX_PRESIGN_V4,
                    help=f"presign TTL seconds (s3v4 max {MAX_PRESIGN_V4})")
    rw.add_argument("--signature", choices=("s3v4", "s3"), default="s3v4",
                    help="s3v4 = current scheme, 7-day cap; s3 = legacy SigV2, no cap, deprecated")
    rw.add_argument("--base-url", default="", help="serve from this origin instead (e.g. a CloudFront domain)")
    rw.add_argument("--relink", action="store_true",
                    help="also re-key https URLs left by the retired path-mirrored layout")
    rw.add_argument("--out", default="", help="write here instead of editing in place")
    rw.add_argument("--map-out", default="", help="also dump a {relpath: url} JSON map")
    rw.set_defaults(func=cmd_rewrite)

    vf = sub.add_parser("verify", help="fetch every rewritten URL and report status")
    vf.add_argument("--html", required=True)
    vf.set_defaults(func=cmd_verify)

    pr = sub.add_parser("prune", help="find objects under the prefix nothing references")
    common(pr)
    pr.add_argument("--delete", action="store_true",
                    help="actually delete them (default: report only)")
    pr.set_defaults(func=cmd_prune)

    args = ap.parse_args()
    if getattr(args, "signature", "s3v4") == "s3v4" and getattr(args, "expires", 0) > MAX_PRESIGN_V4:
        sys.exit(f"--expires cannot exceed {MAX_PRESIGN_V4} with s3v4 (S3 returns 400); "
                 f"pass --signature s3 for a longer TTL")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
