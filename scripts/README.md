# scripts/ — operational scripts

Helper scripts for deploying and exercising REVA. Not imported by any service.

| Script | Purpose |
|---|---|
| `deploy.sh` | Production deploy: `git pull origin main` → build → stop → `up -d` → poll `/health`. Run on the prod host. |
| `setup-letsencrypt.sh` | One-time, before the first prod deploy: obtains the TLS cert via certbot standalone for `$REVA_DOMAIN`. |
| `backup.sh` | Gzipped `pg_dump` of the Postgres DB to `REVA_BACKUP_DIR` (default `./backups`), pruning dumps older than `REVA_BACKUP_RETENTION_DAYS` (default 14). Run from a host cron job. |
| `restore.sh` | Restore the DB from a `backup.sh` dump (newest by default, or a given file). Verifies the gzip, confirms before overwriting, restores in a single transaction with `ON_ERROR_STOP`, and prints a sanity check. |
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
./scripts/restore.sh                          # newest backup
./scripts/restore.sh backups/reva-….sql.gz    # a specific dump
```

### Recovery drill (R10 — do this once, then periodically)

A backup you've never restored is not a backup. Prove the path end-to-end:

```bash
./scripts/backup.sh                 # produce a dump
# (optionally insert a sentinel row, or just note the current table/row counts)
./scripts/restore.sh                # type 'restore' to confirm
# restore.sh prints the public-table count + schema version — confirm they
# match expectations. For a non-destructive drill, restore into a throwaway
# database/container instead of the live one.
```

Until this drill has actually been run on the prod host, R10 is **not** closed —
the scripts are syntax-checked but an unexercised recovery path is still a risk.

## Why these exist

`deploy.sh` and `setup-letsencrypt.sh` encode the exact prod sequence so deploys
are repeatable rather than remembered. `fake-webhook.py` lets you drive the full
webhook → debounce → worker path end-to-end locally without configuring a real
GitHub App.

> `deploy.sh` pulls and builds whatever is on `main` with no tag pinning — fine
> for a single-maintainer setup; pin to a reviewed ref if that changes.
