#!/usr/bin/env bash
# Back up the REVA Postgres database (gzipped pg_dump) to a local directory.
#
# Run from the project root. Intended for a host cron job; for real disaster
# recovery, sync REVA_BACKUP_DIR off-host (rsync/S3) afterwards.
#
#   crontab -e:
#   0 2 * * *  cd /opt/cu_reva && ./scripts/backup.sh >> /var/log/reva-backup.log 2>&1
#
# Restore (DESTRUCTIVE — overwrites the current DB): use scripts/restore.sh.

set -euo pipefail

COMPOSE="docker compose -f docker-compose.prod.yml"
BACKUP_DIR="${REVA_BACKUP_DIR:-./backups}"
RETENTION_DAYS="${REVA_BACKUP_RETENTION_DAYS:-14}"

mkdir -p "$BACKUP_DIR"
ts="$(date -u +%Y%m%dT%H%M%SZ)"
outfile="${BACKUP_DIR}/reva-${ts}.sql.gz"

# Don't leave a partial/empty .gz behind if the dump pipeline fails (set -e would
# otherwise exit before the empty-check below, stranding a corrupt file).
trap 'rm -f "$outfile"' ERR

echo "==> Dumping database to ${outfile}..."
# pipefail (set above) makes a pg_dump failure fail the whole pipeline.
$COMPOSE exec -T postgres pg_dump -U review -d reviews --no-owner --clean --if-exists \
  | gzip -c > "$outfile"

# Guard against a silent empty/partial dump (e.g. postgres container down).
if [ ! -s "$outfile" ]; then
  echo "ERROR: backup file is empty — dump failed." >&2
  rm -f "$outfile"
  exit 1
fi

# The backup is safely written; stop guarding it (don't let prune/ls errors below
# trip the ERR trap and delete a good backup).
trap - ERR

echo "==> Backup complete ($(du -h "$outfile" | cut -f1))."

echo "==> Pruning backups older than ${RETENTION_DAYS} days..."
find "$BACKUP_DIR" -name 'reva-*.sql.gz' -type f -mtime "+${RETENTION_DAYS}" -delete

echo "==> Current backups:"
ls -1t "${BACKUP_DIR}"/reva-*.sql.gz 2>/dev/null | head -5
