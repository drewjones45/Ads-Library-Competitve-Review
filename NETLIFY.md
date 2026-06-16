# Hosting the dashboards on Netlify

The `intel dashboard` HTML is built for **local** (`file://`) viewing — it embeds
absolute filesystem paths and pulls images from `data/*_assets/`, which is
gitignored and large. To put the dashboards online, we build a **self-contained
static site** (`dist/`) that copies + rewrites every asset reference to be
portable, then deploy that folder to Netlify.

Because the assets only exist on the machine that ran the ingest, **the site is
built locally, not in Netlify CI.** A Git-connected build would have no `data/`
and would render broken images.

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
