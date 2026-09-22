# Moving off personal-account Netlify: AWS Amplify Hosting scoping

Status: **scoping only, nothing built yet.** This exists to turn "should we
move to Amplify" into a concrete plan and a concrete ask to Horizon IT, so the
next step is a real decision, not more research.

This covers **both dashboard products this repo hosts**, not just competitive
intelligence: the competitor-tracking dashboards (TREX, others) and the
**Creative Performance Dashboard** (owned-Meta-account creative + spend
analysis — see `PERFORMANCE_DASHBOARD_SPEC.md`), currently live for Spectrum.
Both go through the exact same `build_site.py` → Netlify pipeline this doc is
about, and Spectrum's dashboard specifically is the one already depending on
the S3/presigning setup this scoping addresses — it's also **active client
work with a delivery timeline** (Creative Dashboard agent work currently
underway), which is the concrete reason to move on the IT asks below promptly
rather than let this sit as a nice-to-have infra cleanup.

## Why Amplify over "just move Netlify to a Horizon org account"

Both fix the immediate problem (the live site runs under Andrew's personal
Netlify account). The recommendation is Amplify anyway, because of a second
problem the research for this doc surfaced that a same-vendor account swap
wouldn't touch — see "The actual IAM-role-vs-static-keys answer" below. The
short version: getting off Netlify-under-a-personal-account is a same-day
netlify.toml-unchanged fix either way; Amplify additionally lets us kill the
S3 credential sitting in a hosting vendor's env vars *entirely*, not just move
whose vendor account it sits in.

## The actual IAM-role-vs-static-keys answer

This needs stating plainly because it changes the plan: **Amplify Hosting's
build phase does not give a static site an assumable IAM role for calling
other AWS services.** That "compute role" mechanism exists, but only for
server-side rendering (Lambda-based SSR) at *runtime* — not for `amplify.yml`
build commands, and this site has no SSR component to attach one to. A build
step that needs to call S3 (which `ci_presign.py` does, to presign every
S3-hosted dashboard) still needs static `AWS_ACCESS_KEY_ID`/
`AWS_SECRET_ACCESS_KEY` as Amplify environment variables — the same category
of exposure as it has in Netlify's env vars today, just under a Horizon-owned
account instead of Andrew's personal one. ([AWS Amplify Hosting — IAM
compute roles for SSR](https://docs.aws.amazon.com/amplify/latest/userguide/amplify-SSR-compute-role.html))

This exact class of friction already bit us on the Netlify side while wiring
up the interim setup (2026-09-22): Netlify's own Site environment variables
reject `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` outright ("is a reserved
environment variable") — the workaround was custom-named
`S3_ACCESS_KEY_ID`/`S3_SECRET_ACCESS_KEY` vars plus a small `_client()`
fallback in `scripts/s3_assets.py`. Amplify may or may not have the identical
reservation (untested), but the underlying lesson generalizes: any hosting
vendor's build environment is liable to have its own opinions about the
standard AWS SDK env var names, which is one more reason the Lambda-presign
design below — no AWS credential of any name sitting in a hosting vendor's
env vars at all — is the sturdier target, not just the more secure one.

So there are two real options, not one:

* **Phase 1 (lift-and-shift):** same architecture as today — `build_site.py`
  writes plain S3 URLs, `ci_presign.py` signs them at build/deploy time —
  just running on Amplify with static AWS keys as Amplify env vars instead of
  Netlify env vars. Fast, low-risk, no code changes beyond the build spec
  translation below. Does not close the "static credentials in a hosting
  vendor's env vars" gap, only moves which account owns the vendor.
* **Phase 2 — superseded, do not pursue:** the original target here was
  CloudFront + Origin Access Control (OAC) in front of the private bucket.
  **Jesse (Horizon IT) has previously discounted introducing CloudFront**,
  so this is off the table regardless of its technical merits — see "Could
  dashboards be served dynamically" below for the replacement target, which
  reaches the same "no credentials in the build" outcome without it.

Recommendation: do Phase 1 only if getting off the personal account is
urgent on its own and the dynamic-presigning work below isn't ready yet;
otherwise go straight to the Lambda-presign design below — it's a bigger
one-time build but leaves nothing to migrate again later, closes the
SigV2-deprecation risk too, and directly satisfies the already-logged
"commerce key is over-privileged" concern by removing the need for *any*
credential in the hosting build, all without CloudFront.

## Could dashboards be served dynamically instead of static HTML?

Asked because static HTML means presigned URLs get baked in once at build
time using SigV2 (deprecated, chosen only because SigV4 caps at 7 days — too
short to survive between infrequent rebuilds; see `S3_ASSETS.md`). Serving
dynamically would let each image request get a fresh, short-lived SigV4 URL
instead — SigV4 isn't a fallback then, it's the actively-recommended scheme,
never deprecated, and each URL only needs to outlive one page view. This is
now **the recommended target** (not just an alternative to CloudFront+OAC),
since Jesse has ruled CloudFront out regardless of its merits.

**This is really two different changes, at very different cost, and they
should not be conflated:**

1. **Dynamic image URLs only, dashboard HTML stays static (small, targeted
   — this is the recommendation).** Keep `build_site.py`'s HTML-generation
   model exactly as it is; change what an `<img>` resolves to.
2. **Fully dynamic dashboards — pages rendered per request, not prebuilt
   HTML (large, not scoped here).** This is a materially bigger change: it
   means moving the actual dashboard-generation logic
   (`src/intel/synthesis/performance_dashboard.py` and equivalents) out of a
   build-time script and into a request-time backend — real routing, real
   query-per-request instead of "inline everything into one HTML file once."
   It also does **not** fit Amplify Hosting's SSR feature as a shortcut: that
   feature's IAM "compute role" only applies to its supported frameworks
   (Next.js, Nuxt, Astro, SvelteKit, Express — Node.js processes), and this
   tool is Python. Doing this for real means a separate backend (Lambda +
   API Gateway, or a small container service) independent of Amplify Hosting,
   which then only serves whatever static shell remains. That's a real
   application rewrite, not an infra change, and not something to fold into
   an already-time-boxed client delivery. Not pursued here.

### The design for (1): batch presign, not per-image

The obvious first design — one `<img>` per Lambda call, each hitting
`/img/<sha256>` and 302-redirecting to a fresh signed URL — has a real
problem specific to this app: **dashboards are image-heavy**, and a naive
per-image redirect means one Lambda invocation *per image, per page load*.
Measured this session: TREX's dashboard has **134 distinct images**,
Spectrum's has **43**. A single operator opening the TREX dashboard would
fire ~134 near-simultaneous Lambda invocations; multiple operators or more
S3-hosted deployments push that further, toward Lambda's shared
per-account concurrency ceiling. That's a real throttling/reliability risk
this app's own numbers create, not a hypothetical one — so it changes the
design, not just a footnote:

**Batch the signing into one call per page load, not one per image.**
`build_site.py` emits `<img data-s3-key="<sha256>">` placeholders (no URL at
all) plus one small inline `<script>` per page. On load, that script makes a
**single** fetch to the Lambda endpoint with the full list of keys the page
needs, gets back one JSON map of `{key: presigned_url}`, and rewrites every
`<img>`'s `src` from it. Cost per page view: **one** Lambda invocation
regardless of how many images are on the page (134 or 43 or 1000), each
still individually signed with a fresh, short-lived SigV4 URL scoped to
exactly the keys that page asked for.

### Security / performance / management check, since that was the condition

* **Security: a net improvement**, not a wash. No static AWS keys anywhere
  (Lambda's execution role provides temporary, auto-rotated credentials —
  this is the part that actually delivers "no credentials in the build/serve
  path," which Amplify's own build phase can't). The bucket stays fully
  private — no public bucket policy, no public CDN origin at all, which is
  a smaller exposure than CloudFront+OAC would have been, not just an
  equivalent one. URLs are short-lived (minutes, not the 1-year SigV2 URLs
  today), shrinking the window a leaked/shared link stays valid. One caveat
  to be explicit about, not a regression: the Lambda endpoint itself must
  accept unauthenticated requests (a browser's own JS calling it directly
  can't present AWS-signed auth), so it inherits the same "URL/key is the
  only privacy boundary" model this site already runs on (see `netlify.toml`
  — no login exists today). It doesn't make that model worse; it's the same
  boundary the plain and presigned-URL modes already have.
* **Performance: roughly comparable to CloudFront for this app's actual
  traffic** (a handful of internal operators, not consumer-scale), *given*
  the batch design above — one extra round trip per page load to resolve
  the key→URL map, then normal direct-to-S3 image fetches after that. What
  it does **not** get, and CloudFront would have: edge caching across
  separate page loads/users. Every fresh page view re-signs every key, even
  if the last viewer loaded the exact same dashboard a minute ago. Acceptable
  at this app's scale; would need revisiting if traffic ever grows past
  "a handful of operators running periodic audits."
* **Management: the one real, ongoing cost, worth accepting deliberately
  rather than glossing over.** This introduces a live piece of infrastructure
  we own and must keep working — a Lambda function, its IAM role, CloudWatch
  alarms worth having on invocation errors/throttles, and periodic runtime
  version bumps (Lambda Python runtimes get deprecated on a schedule). It's
  also a **single point of failure for every S3-hosted dashboard's images at
  once** — if this function breaks, every image on every S3-hosted dashboard
  breaks simultaneously, versus CloudFront+OAC's "configure once, essentially
  never touch again" failure profile. None of this is a reason not to do it
  — it's a small, well-scoped function — but it's a different category of
  ongoing ownership than the CloudFront option would have been, and the
  honest answer to "does it introduce management issues" is "one small,
  bounded, worth-it one," not "none at all."

### Does this change the IT asks?

Yes — it **replaces** the CloudFront+OAC ask entirely:

* Drop: the CloudFront distribution + OAC ask.
* Add: **one Lambda function** (Python is fine — this isn't Amplify SSR)
  **with an IAM execution role** scoped to `s3:GetObject` on
  `arn:aws:s3:::next-ext-commerce-us-east-1/outbound/competitive-intel/*/static/*`
  only, exposed via a Lambda Function URL (or a single API Gateway route).
  No static AWS keys requested or stored anywhere for this piece, and no
  CloudFront anywhere in the ask.
* Unchanged: the Amplify Hosting app itself, the read-only `tables/`/
  `sidecars/` key ask, the SSE/logging questions.

The "Exact asks" list below has been updated to this as the default.

## Build spec translation (netlify.toml → amplify.yml + customHttp.yml)

Amplify has no single `netlify.toml`-equivalent file; build commands, headers,
and artifacts split across `amplify.yml` (build) and `customHttp.yml`
(headers) — both live at repo root, same pattern as `netlify.toml` today.
Confirmed via AWS's own docs during this scoping pass, not assumed from
memory: ([build spec
reference](https://docs.aws.amazon.com/amplify/latest/userguide/yml-specification-syntax.html),
[custom header YAML
reference](https://docs.aws.amazon.com/amplify/latest/userguide/custom-header-YAML-format.html)).

**amplify.yml** — no `package.json` exists in this repo (it's a Python tool,
not a JS framework app), so Amplify's autodetection has nothing to key off;
this file is required, not optional, unlike some JS-framework Amplify setups:

```yaml
version: 1
frontend:
  phases:
    preBuild:
      commands:
        - pyenv global 3.11          # repo requires >=3.11 (pyproject.toml); AL2023 build image ships 3.10/3.11
        - pip install boto3          # ci_presign.py's only dependency; build_site.py itself is stdlib-only
    build:
      commands:
        - python3 scripts/build_site.py
        - python3 scripts/ci_presign.py     # Phase 1 only — delete this line entirely once the Lambda-presign design ships
  artifacts:
    baseDirectory: dist
    files:
      - '**/*'
  cache:
    paths: []          # nothing here benefits from caching between builds; dist/ is regenerated from reports/ every time
```

**customHttp.yml** — direct translation of `netlify.toml`'s `[[headers]]`
blocks, pattern-matched instead of glob-in-TOML but the same two rules:

```yaml
customHeaders:
  - pattern: '/*/*/assets/*'
    headers:
      - key: 'Cache-Control'
        value: 'public, max-age=31536000, immutable'
  - pattern: '/*.html'
    headers:
      - key: 'Cache-Control'
        value: 'public, max-age=0, must-revalidate'
  - pattern: '/*'
    headers:
      - key: 'X-Robots-Tag'
        value: 'noindex, nofollow, noarchive, nosnippet, noimageindex'
```

No redirects/rewrites exist in `netlify.toml` today, so none need translating.

**Manual/CLI deploy path** (`deploy_netlify.sh`) — Amplify's equivalent for a
build-from-a-machine-with-the-full-data-tree deploy (needed for `bobs`, whose
assets live under gitignored `data/creative/` and can never build in any
Git-connected CI) is `aws amplify start-deployment` with a zipped `dist/`, or
the Amplify CLI's manual publish. Would need a small rewrite of
`deploy_netlify.sh` to call that instead of `netlify deploy --no-build` — not
started; scoping only confirms the equivalent exists, doesn't require a
different architecture.

## Domain

No custom domain exists today — the site is on Netlify's default subdomain
(no CNAME or `netlify.app` reference found anywhere in this repo). Amplify's
equivalent default is an `*.amplifyapp.com` subdomain, so there's no
regression either way. The request for a Horizon-owned subdomain (e.g. `tools.horizoncommerce.com`, via
Amplify's custom-domain feature + Horizon's DNS) is listed as an ask below.

## What does NOT need to change

* `build_site.py` itself — stdlib-only, framework-agnostic, no Netlify- or
  Amplify-specific assumptions in it today.
* The noindex/no-crawl privacy posture — `X-Robots-Tag` carries over exactly.
* The `bobs`-deployment caveat (gitignored assets, can't build in any
  Git-connected CI, manual deploy only) — true of Amplify's Git-triggered
  build exactly as it's true of Netlify's today; not something migrating
  fixes or breaks.

## Related, but a separate decision: this repo's GitHub ownership

The repo itself (`github.com/drewjones45/Ads-Library-Competitve-Review`) is
also under Andrew's personal GitHub account, not a Horizon-owned org. Amplify
can connect to it either way — via the AWS Amplify GitHub App installed with
access granted to just this repo, no repo transfer required — but if "owned
infra" is meant to include the source repo too, that's a second, independent
migration (repo → a Horizon GitHub org) worth deciding on explicitly rather
than assuming either way.

---

## Exact asks for Horizon IT

This request is for our dashboard tooling currently hosted on Netlify under a personal account.
We have active client work underway a Spectrum POC that depends on the S3 infrastructure this ask covers, so a prompt turnaround directly affects that delivery date.

We want to move hosting to AWS Amplify under a Horizon-owned AWS account and implement dynamic SigV4
pre-signing for S3 image access. This supersedes an earlier request about a public S3 bucket policy for `next-ext-commerce-us-east-1`.

### A. AWS Account & Amplify Hosting

1. **Confirm which AWS account this should live in.** Our working assumption
   is the same account that already owns the `next-ext-commerce-us-east-1` S3
   bucket and the `commerce` IAM user we currently use (account ID
   `254947843672`, `us-east-1`). Please confirm or correct.
2. **Provision an AWS Amplify Hosting app** in that account/region, connected
   to this GitHub repository (`github.com/HorizonMedia/Ads-Library-Competitive-Review`)
   via the AWS Amplify GitHub App. Repo-level access grant is sufficient.
   - Build image: AL2023 (default)
   - Build command: `python3 scripts/build_site.py`
   - Artifacts directory: `dist/`

### B. Lambda Pre-signing & IAM Execution Role

3. **One AWS Lambda function** (name: `ads-library-s3-presign` or equivalent),
   Python 3.11+ runtime, with a dedicated IAM execution role, exposed via Lambda
   Function URL (preferred) or a single API Gateway route.
   
   **Function spec:**
   - **Purpose:** On demand, sign short-lived SigV4 URLs for S3 objects in the
     `next-ext-commerce-us-east-1` bucket's competitive-intel asset path.
   - **Trigger:** HTTP POST requests with JSON input: `{"keys": ["outbound/competitive-intel/trex/static/...", ...]}`
   - **Response:** JSON map `{key: presigned_url}` where each URL is a SigV4 pre-signed GET request valid for 15 minutes.
   - **Authorization:** Public endpoint (unauthenticated requests allowed; URL serves as the authorization token).
   
   **IAM Execution Role spec:**
   - **Permissions:** `s3:GetObject` only
   - **Resource ARN:** `arn:aws:s3:::next-ext-commerce-us-east-1/outbound/competitive-intel/*/static/*`
   - Standard Lambda execution role trust policy (allow `lambda.amazonaws.com`)

### C. Additional S3 Bucket Configuration

4. **A read-only IAM policy/role scoped to `.../tables/*` and `.../sidecars/*`** for the `next-ext-commerce-us-east-1` bucket.
   (Purpose: dashboards read raw competitive-intel data from these paths, separate from the static asset images.)

5. **Confirm bucket configuration:** does `next-ext-commerce-us-east-1` have default server-side encryption and CloudTrail logging enabled?
   (Purpose: verify security baseline for this bucket.)

### D. Custom Domain

6. **A subdomain under a Horizon-owned domain** (e.g. `competitive-intel.horizoncommerce.com`) pointed at the Amplify app, preferred over Amplify's default `*.amplifyapp.com` domain.
   (Not a blocker. It can be added later.)
