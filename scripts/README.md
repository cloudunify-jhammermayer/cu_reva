# scripts/ — operational scripts

Helper scripts for deploying and exercising REVA. Not imported by any service.

| Script | Purpose |
|---|---|
| `deploy.sh` | Production deploy: `git pull origin main` → build → stop → `up -d` → poll `/health`. Run on the prod host. |
| `setup-letsencrypt.sh` | One-time, before the first prod deploy: obtains the TLS cert via certbot standalone for `$REVA_DOMAIN`. |
| `backup.sh` | Gzipped `pg_dump` of the Postgres DB to `REVA_BACKUP_DIR` (default `./backups`), pruning dumps older than `REVA_BACKUP_RETENTION_DAYS` (default 14). Run from a host cron job. |
| `fake-webhook.py` | Local testing: posts a correctly HMAC-signed GitHub webhook payload to a running API so you can exercise the pipeline without GitHub. |

## Backups & restore

`backup.sh` is the durability path the architecture doc assumed but that was
never implemented. Schedule it from host cron and **sync `REVA_BACKUP_DIR`
off-host** (rsync/S3) — a local-only backup doesn't survive host loss.

```bash
# Daily at 02:00, logging to a file:
0 2 * * *  cd /opt/cu_reva && ./scripts/backup.sh >> /var/log/reva-backup.log 2>&1
```

Restore (**destructive** — overwrites the live DB; stop the app first):

```bash
gunzip -c backups/reva-YYYYMMDDTHHMMSSZ.sql.gz \
  | docker compose -f docker-compose.prod.yml exec -T postgres psql -U review -d reviews
```

Verify a restore into a throwaway DB at least once so you know the dumps are
usable before you need them.

## Why these exist

`deploy.sh` and `setup-letsencrypt.sh` encode the exact prod sequence so deploys
are repeatable rather than remembered. `fake-webhook.py` lets you drive the full
webhook → debounce → worker path end-to-end locally without configuring a real
GitHub App.

> `deploy.sh` pulls and builds whatever is on `main` with no tag pinning — fine
> for a single-maintainer setup; pin to a reviewed ref if that changes.
