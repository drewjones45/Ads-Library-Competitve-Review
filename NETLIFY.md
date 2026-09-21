# Hosting the dashboards on Netlify

⚠ This Netlify site runs under Andrew's personal account — moving it to a
Horizon-owned AWS Amplify setup is scoped out in **AMPLIFY_MIGRATION.md**
(nothing built yet, scoping + IT asks only). Everything below still describes
the live, current setup.

The `intel dashboard` HTML is built for **local** (`file://`) viewing — it embeds
absolute filesystem paths and pulls images from `data/*_assets/`, which is
gitignored and large. To put the dashboards online, we build a **self-contained
static site** (`dist/`) that copies + rewrites every asset reference to be
portable, then deploy that folder to Netlify.

**Git-push deploys work** (2026-07-14). The site is Git-connected: a push to
`main` triggers Netlify CI, which runs `netlify.toml`'s `command` (stdlib
only — nothing to install) and publishes `dist/`. This works because the
`philo`/`trex`/`wegmans` deployments' `data/*_assets/` ARE committed, and
because `build_site.py` resolves asset refs against the repo's own `data/` tree
rather than the absolute paths baked into the HTML.

That `command` is `build_site.py && ci_presign.py` — the second step presigns
every S3-hosted dashboard (Spectrum, TREX) so their images don't 403 on
Netlify's own build, not just the manual `deploy_netlify.sh` path. This needs
AWS credentials as Netlify's own environment variables (Site configuration →
Environment variables): `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`AWS_S3_BUCKET`, `AWS_S3_PREFIX`, `AWS_REGION`. See netlify.toml's decision
record comment for why this — an in-place `command` update using Netlify's own
env vars — won out over the alternative (a GitHub Actions workflow using
GitHub Secrets, which was PR #1 and got closed unmerged once this was picked).

That last part is the whole trick, and it used to be broken: the dashboards
embed absolute paths from the machine that generated them
(`/Users/<you>/…/data/…`). In a CI container those do not exist, so the asset was
silently skipped while its `<img>` ref was still rewritten — publishing a site
whose images all 404. If you see mass 404s, that is the failure mode to check.

Caveat: the **bobs** deployment's assets live under `data/creative/`, which IS
gitignored, so its dashboards still render broken images in a CI build. The
build now prints a loud `⚠ … could not be resolved` warning listing anything it
could not find. Deploy locally (below) if you need bobs' images.

Spectrum currently publishes TWO builds of the same dashboard: a bundled-asset
one that renders today, and an S3-backed one that will render once the bucket
policy lands. The landing page labels both. See S3_ASSETS.md.

There is now an alternative that sidesteps all of this: host the creative on S3
and point the dashboard at it, so `build_site.py` has nothing to copy and the
gitignored/committed distinction stops mattering. Spectrum is running that way —
its `dist/` output is 336 KB instead of the 168 MB jdsports needs for the same
kind of dashboard. See **S3_ASSETS.md**; it is tested and working, with one
bucket-policy change outstanding to make the URLs permanent rather than
7-day presigned.

## One-time setup

```bash
npm install -g netlify-cli     # or: brew install netlify-cli
netlify login                  # authorize in the browser
```

## Build + deploy

```bash
# Build the portable site (latest report date per deployment):
python3 scripts/build_site.py            # -> dist/
#   add --all to include every historical report date

# Deploy:
./scripts/deploy_netlify.sh              # draft deploy -> preview URL
./scripts/deploy_netlify.sh --prod       # publish to the production URL
```

First ever deploy: run `netlify init` once (creates/links a Netlify site to this
repo), then `./scripts/deploy_netlify.sh --prod`.

## No-CLI alternative (drag-and-drop)

```bash
python3 scripts/build_site.py
```

Then drag the `dist/` folder onto <https://app.netlify.com/drop>.

## What's published

`scripts/build_site.py` discovers every dashboard under `reports/**` and emits:

```
dist/
  index.html                      # landing page linking all deployments
  <deployment>/<date>/
    assets/...                    # only the assets that dashboard references
    dashboard/index.html
    dashboard-v2/index.html
    with-google-dashboard/index.html   # if present
```

Deployments: `philo`, `bobs`, `trex`, `revlon`. By default only the latest date
per deployment is published; pass `--all` to publish the full history.

## Refreshing after a new ingest

Re-run the relevant `quickstart_*.sh` (or just the dashboard build) so the
`reports/<deployment>/<date>/` HTML is current, then re-run the build + deploy
commands above. `dist/` is regenerated from scratch each time.
