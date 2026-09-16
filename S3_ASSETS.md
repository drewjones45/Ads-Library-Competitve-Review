# Hosting dashboard creative on S3

Status: **tested and working for the Spectrum performance dashboard** (2026-09-07),
using presigned URLs. One bucket-policy change is needed to make it permanent.

## Why

Dashboard HTML references creative by absolute filesystem path — whatever the
machine that generated it used. The Spectrum dashboard was built on Windows, so
every thumbnail pointed at
`C:\Users\AnJones\OneDrive - HorizonMedia\...\data\spectrum_assets\...\preview.png`.
On any other machine that is `ERR_UNKNOWN_URL_SCHEME`: **0 of 202 thumbnails
rendered**, locally or on Netlify.

The workaround so far has been `scripts/build_site.py`, which copies every
referenced byte into `dist/`. That works but does not scale — the current `dist/`
is **983 MB**, of which jdsports alone is 168 MB. Netlify has to receive all of it
on every deploy, and the assets are gitignored, so a CI build from a clean
checkout silently 404s.

Pointing the refs at S3 removes both problems. After the rewrite, Spectrum's
`dist/` output is **336 KB of HTML and zero copied assets**.

## Doing it

```bash
export AWS_ACCESS_KEY_ID=...  AWS_SECRET_ACCESS_KEY=...
B=next-ext-commerce-us-east-1
P=outbound/competitive-intel

# 1. mirror the local assets tree to S3 (idempotent — re-uploads only changed files)
python3 scripts/s3_assets.py upload --local-dir data/spectrum_assets --bucket $B --prefix $P

# 2. repoint the dashboard at them
python3 scripts/s3_assets.py rewrite \
  --html reports/spectrum/2026-09-01/performance-dashboard/index.html \
  --bucket $B --prefix $P --mode presign

# 3. confirm every URL actually serves
python3 scripts/s3_assets.py verify \
  --html reports/spectrum/2026-09-01/performance-dashboard/index.html
```

## Layout: content-addressed, so nothing is stored twice

Keys are `<prefix>/by-hash/<ab>/<sha256>.<ext>` — the object's own content hash,
not a mirror of its local path. That is load-bearing, because the same bytes
appear at many local paths:

| Tree | Files | Distinct blobs | Waste |
|---|---|---|---|
| spectrum | 93 | 51 | 4.0 MB (40%) |
| all six deployments | 20,131 | 10,002 | 624 MB (34.5%) |

One creative reused across ten ads is ten files on disk and **one** object in S3.
Re-running a report re-uploads nothing whose bytes are unchanged — the second run
of the Spectrum refresh reported `51 already in S3, 0 to upload` — and that holds
even if the report reorganises its paths, because the key never depended on the
path in the first place.

Two consequences worth knowing:

* Keys are not human-browsable, so `upload` also writes
  `<prefix>/manifest/<tree>.json` mapping every local relative path to its key.
* `rewrite` must be able to READ the local file, since the key is a hash of it. A
  ref it cannot resolve is left alone and reported, rather than being pointed at a
  key that was never uploaded.

The key deliberately contains no `data/` segment — `build_site.py` keys its own
rewriting off `/data/` and would otherwise try to pull the finished URLs back
into local copies.

### Housekeeping

`prune` lists every object under the prefix that no local asset tree accounts
for — retired path-mirrored keys, and hashes whose source files are gone. It
reports by default and only deletes with `--delete`:

```bash
python3 scripts/s3_assets.py prune --bucket $B --prefix $P            # report
python3 scripts/s3_assets.py prune --bucket $B --prefix $P --delete   # act
```

If a dashboard published under the old path-mirrored layout still needs to work
after a prune, re-key it first with `rewrite --relink`, which recognises those
URLs and points them at the content-addressed objects.

`rewrite` is idempotent: run it twice and the second pass reports
"no local asset refs" rather than double-rewriting.

Rebuild order matters: `intel perf-dashboard` writes local paths, so `rewrite`
runs **after** every rebuild. `scripts/refresh_spectrum_perf.sh` now does the
whole sequence — ingest, series, dashboard, upload, rewrite, verify — and skips
the S3 step with a warning when `AWS_ACCESS_KEY_ID` is unset.

The Spectrum dashboard carries 93 refs resolving to 51 distinct objects: 48 card
thumbnails plus the gallery assets the creative lightbox pages through (see
PERFORMANCE_DASHBOARD_SPEC.md §7.8).

## The one blocker: the bucket is private

The `commerce` IAM key (`arn:aws:iam::254947843672:user/commerce`) can put, get,
list and delete objects. It **cannot** read or set bucket configuration —
`GetBucketPolicy`, `GetPublicAccessBlock`, `GetBucketAcl` and friends all return
`AccessDenied` — and the bucket has ACLs disabled (`AccessControlListNotSupported`),
so an object cannot make itself public either.

Measured result, anonymous GET on an uploaded object:

| URL mode | HTTP | Renders in browser |
|---|---|---|
| `--mode public` (plain object URL) | 403 | no — `ERR_BLOCKED_BY_ORB`, 0/202 thumbs |
| `--mode presign` | 200 | **yes — 202/202 thumbs** |

So presigning is what works today, and it is what the Spectrum dashboard
currently uses.

### Permanent fix

Ask whoever administers the bucket to allow anonymous reads on this one prefix
(this is the whole change — it does not open the rest of the bucket):

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "PublicReadCompetitiveIntelAssets",
    "Effect": "Allow",
    "Principal": "*",
    "Action": "s3:GetObject",
    "Resource": "arn:aws:s3:::next-ext-commerce-us-east-1/outbound/competitive-intel/*"
  }]
}
```

The bucket's Block Public Access settings must also allow it — specifically
`BlockPublicPolicy` and `RestrictPublicBuckets` off. Then re-run `rewrite` with
`--mode public` and the URLs become permanent, cacheable, and free of any
credential material.

If public reads are not acceptable at all, put CloudFront in front of the bucket
with an Origin Access Control and pass the distribution domain to
`rewrite --mode public --base-url https://dxxxx.cloudfront.net`.

## Two Meta tokens, and what neither of them buys

`.env` carries two Meta credentials because the lanes need different apps, and
each app is refused by the other's endpoints (verified 2026-09-07, both directions
return `(#10) Application does not have permission`):

| Var | Lane | State |
|---|---|---|
| `META_OWNED_ACCESS_TOKEN` | owned accounts (`act_*` insights) | system user, **never expires**, `ads_management`+`ads_read` |
| `META_AD_LIBRARY_ACCESS_TOKEN` | competitor scraping (`/ads_archive`) | **expired 2026-08-01**, needs reissue |

`meta_account._token()` prefers the owned var and falls back to the Ad Library one
so older deployments keep working.

Settled while testing the new token: **the `/{video_id}` node is refused entirely**
— not just `source`, but `picture`, `format`, `thumbnails` and `title` too. So
there is no route to an mp4 and no route to a thumbnail larger than the ~160px
`thumbnail_url` already embedded in the creative. The production ingest reports it
plainly: `0 video file(s) downloaded · 41 unavailable to this token`. Video ads
therefore render as stills with a "Play in Ads Manager" link, and that is a
permission ceiling, not a bug to keep poking at.

## Operating model: presign at deploy, public in git

Presigned URLs are how the images actually reach the deployed site, because the
bucket policy needed for plain public reads is not available (see below). The two
copies therefore carry different URL modes, and this split is load-bearing:

| Copy | Mode | Why |
|---|---|---|
| `reports/**` — committed | `--mode public`, credential-free | This repo is PUBLIC. A presigned URL embeds the AWS access key id, and an `AKIA…` string in a public repo trips GitHub secret scanning → AWS → possible key quarantine. It would also freeze a dead link into permanent history. |
| `dist/**` — deployed | presigned, SigV2, 1 year | Only the live site carries the signature, and **every deploy refreshes the expiry**, so the site cannot age out while it is being maintained. |

`scripts/deploy_netlify.sh` does the presigning automatically after
`build_site.py`, scoped to `dist/spectrum/` — Spectrum is the only deployment
whose creative lives in S3, and the explicit path keeps enabling another one a
deliberate act. Override with `S3_SIGNATURE` / `S3_PRESIGN_TTL` if needed. With
no AWS credentials in the environment the step is skipped and `dist/` keeps its
public-mode URLs (which 403) — it never silently half-works.

Two guards enforce the split, because "remember not to commit the presigned one"
is not a control:

* `s3_assets.py rewrite --mode presign` **refuses** to write into `reports/`
  (`--allow-reports` to override).
* `scripts/check_no_credentials.sh` fails if anything git tracks carries an
  AWS key id or signature. Install it as a pre-commit hook with:
  `ln -sf ../../scripts/check_no_credentials.sh .git/hooks/pre-commit`

### On SigV2, and the expiry

`--signature s3` (SigV2) is used rather than SigV4 because SigV4 caps expiry at 7
days, and a manually-deployed site would go dark 8 days after the last deploy.
SigV2 accepts any expiry — verified against this bucket at 90 days, 1 year and 3
years, all HTTP 206 — because it predates 2020-06-24. AWS has deprecated SigV2
and could withdraw it without notice, which would break every image at once.

`s3_assets.py verify` therefore leads with the expiry and exits non-zero when it
is inside `--warn-days` (default 30) or already past, so staleness surfaces as a
failed check rather than as blank thumbnails nobody can explain.

### What this does NOT solve

The Netlify site has no authentication — the URL is the only thing keeping it
private. A presigned URL on that site is readable by anyone who has the site URL
for the whole validity window, which in practical terms is close to what a public
bucket policy would give. It additionally publishes the AWS account's key id.

So this route answers "we cannot get an admin to change the bucket", not "these
images must not be publicly readable". If the latter is ever the requirement,
Netlify password protection is the cheap fix and applies equally to either route.

## The web app publishes both builds

Until the bucket policy lands, an S3-backed dashboard renders grey boxes on
Netlify. So Spectrum ships two variants for the same date and the landing page
labels which is which:

| Report path | Site label | Renders today |
|---|---|---|
| `2026-09-07/bundled/performance-dashboard/` | Creative performance — images bundled (works now) | **yes** — 202/202, assets copied into `dist/` |
| `2026-09-07/performance-dashboard/` | … — images via S3 (needs bucket policy) | no — 403 until the policy exists |

Both come from the same `intel perf-dashboard` run; the S3 one just has
`s3_assets.py rewrite` applied afterwards. Regenerate the bundled one with no
rewrite step:

```bash
intel perf-dashboard --out reports/spectrum/<date>/bundled/performance-dashboard
```

`build_site.py` flattens nested variant dirs, so `bundled/performance-dashboard`
becomes the single directory `bundled-performance-dashboard` in `dist/` and its
`../assets/` refs resolve. The 93 asset files are committed, so a Netlify CI
build from a clean checkout resolves them too.

Once the policy is in place the bundled variant can simply be deleted — that is
the ~10 MB of duplicated bytes in `dist/` that moving to S3 was meant to remove,
and it is only being carried while the permission is pending.

## Presigned-URL caveats

Real, and the reason this is a stopgap rather than the destination:

* **They expire.** `--signature s3v4` (the default) is capped at 7 days by S3 —
  anything longer gets a 400. The current Spectrum dashboard's URLs expire
  **2026-09-14 17:26 UTC**, after which the thumbnails 403 until `rewrite` runs
  again. Refreshing is one command and needs no re-upload.
* **A longer TTL is available but deprecated.** `--signature s3` produces legacy
  SigV2 URLs, which accept any expiry (a 1-year signature was tested and returns
  206). It works because this bucket predates 2020-06-24, but AWS has deprecated
  SigV2 and can withdraw it. Reasonable as a bridge if the policy change will
  take more than a week; not a destination.
  Note that botocore picks SigV2 *by default* for us-east-1 presigning, which is
  why `s3_assets.py` pins the scheme explicitly instead of inheriting it.
* **The signature embeds the access key ID.** A presigned dashboard deployed to a
  public Netlify URL exposes `AKIA...` in its page source. The secret is not
  exposed and the signature only grants read on that one object until it expires,
  but it is one more reason to prefer `--mode public` for client-facing builds.

## Verifying rendering

`verify` only checks HTTP status. To confirm the browser actually paints the
thumbnails — they are `loading="lazy"` and only exist after a drill-down row is
clicked — load the page in a headless browser, expand the `tr.sum` rows, and
count `img.thumb` elements with `naturalWidth > 0`. Tested both from `file://`
and from a local HTTP server over the built `dist/`, which is the Netlify shape:
202/202 in both.
