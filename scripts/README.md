# scripts/ — operational scripts

Helper scripts for deploying and exercising REVA. Not imported by any service.

| Script | Purpose |
|---|---|
| `deploy.sh` | Production deploy: `git pull origin main` → build → stop → `up -d` → poll `/health`. Run on the prod host. |
| `setup-letsencrypt.sh` | One-time, before the first prod deploy: obtains the TLS cert via certbot standalone for `$REVA_DOMAIN`. |
| `fake-webhook.py` | Local testing: posts a correctly HMAC-signed GitHub webhook payload to a running API so you can exercise the pipeline without GitHub. |

## Why these exist

`deploy.sh` and `setup-letsencrypt.sh` encode the exact prod sequence so deploys
are repeatable rather than remembered. `fake-webhook.py` lets you drive the full
webhook → debounce → worker path end-to-end locally without configuring a real
GitHub App.

> `deploy.sh` pulls and builds whatever is on `main` with no tag pinning — fine
> for a single-maintainer setup; pin to a reviewed ref if that changes.
