#!/usr/bin/env bash
# Restore the REVA Postgres database from a gzipped pg_dump produced by
# scripts/backup.sh.
#
# DESTRUCTIVE: the dumps are taken with --clean --if-exists, so restoring drops
# and recreates every object — the current contents of `reviews` are replaced.
#
# Usage (run from the project root):
#   ./scripts/restore.sh                          # restore the NEWEST backup
#   ./scripts/restore.sh backups/reva-...sql.gz   # restore a specific file
#   REVA_RESTORE_YES=1 ./scripts/restore.sh ...   # skip the confirmation prompt
#
# Recovery drill (R10 — proves the backup is actually restorable):
#   ./scripts/backup.sh
#   ./scripts/restore.sh           # confirm; then check the table count it prints
set -euo pipefail

COMPOSE="docker compose -f docker-compose.prod.yml"
BACKUP_DIR="${REVA_BACKUP_DIR:-./backups}"
# Target DB — default is the live 'reviews'. Override for a non-destructive
# recovery drill into a throwaway database (must already exist):
#   REVA_RESTORE_DB=reva_restore_test ./scripts/restore.sh
TARGET_DB="${REVA_RESTORE_DB:-reviews}"

file="${1:-}"
if [ -z "$file" ]; then
  file="$(ls -1t "${BACKUP_DIR}"/reva-*.sql.gz 2>/dev/null | head -1 || true)"
  [ -n "$file" ] || { echo "ERROR: no backups found in ${BACKUP_DIR}" >&2; exit 1; }
  echo "==> No file given; using newest backup: ${file}"
fi
[ -s "$file" ] || { echo "ERROR: backup file missing or empty: ${file}" >&2; exit 1; }

# Verify the archive is intact BEFORE we touch the live database.
gunzip -t "$file" || { echo "ERROR: ${file} is not a valid gzip (corrupt backup)" >&2; exit 1; }

echo "==> About to restore ${file} into the '${TARGET_DB}' database."
echo "    THIS OVERWRITES that database (objects are dropped + recreated)."
if [ "${REVA_RESTORE_YES:-}" != "1" ]; then
  read -r -p "Type 'restore' to proceed: " confirm
  [ "$confirm" = "restore" ] || { echo "Aborted."; exit 1; }
fi

echo "==> Restoring..."
# ON_ERROR_STOP=1 turns a mid-restore SQL error into a hard failure rather than a
# silent half-restore. --single-transaction makes it all-or-nothing.
gunzip -c "$file" \
  | $COMPOSE exec -T postgres psql -v ON_ERROR_STOP=1 --single-transaction -U review -d "$TARGET_DB"

echo "==> Restore complete. Sanity check:"
$COMPOSE exec -T postgres psql -U review -d "$TARGET_DB" -tAc \
  "SELECT count(*) || ' public tables' FROM information_schema.tables WHERE table_schema='public';"
$COMPOSE exec -T postgres psql -U review -d "$TARGET_DB" -tAc \
  "SELECT 'schema version: ' || COALESCE(max(version)::text, 'none') FROM schema_migrations;" \
  2>/dev/null || echo "(schema_migrations not present — check the dump)"
