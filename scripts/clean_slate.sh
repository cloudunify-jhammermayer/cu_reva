#!/usr/bin/env bash
# Reset REVA's operational history for a go-live clean slate — WITHOUT touching
# identity/config and WITHOUT reusing row ids.
#
#   ./scripts/clean_slate.sh [--wipe-feedback] [--yes]
#
# What it does, in order:
#   1. refuses to run while api/worker/scheduler containers are up
#   2. takes a backup via scripts/backup.sh (restore with scripts/restore.sh)
#   3. TRUNCATEs history tables — deliberately NO "RESTART IDENTITY":
#      Odoo records still hold reva_analysis_id / reva_issue_request_id from
#      the old world, and the heal flow (Check Status / watchdog) trusts that
#      ids are never reused. Reused ids could apply ticket B's result to
#      ticket A. Sequences keep counting; stale Odoo pointers just 404.
#   4. flushes the Redis RQ database (queued jobs reference truncated rows)
#
# KEPT (config/identity, not history): odoo_instances (instance keys —
# wiping them 401s every Odoo instance), repositories, schema_migrations,
# the core-knowledge registry, admin_audit (audit trail), and — unless
# --wipe-feedback — muted_categories + repo_review_memory (operator-set
# mutes lift silently if wiped; learned memory is expensive to re-earn).
# review_feedback always dies with its findings (FK CASCADE).
#
# AFTER running: start the stack, then re-click "Create Issues" on any
# in-flight Odoo ticket (open issues) — the hidden body markers let REVA
# re-adopt the existing GitHub issues and re-link the loop.

set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE="docker compose -f docker-compose.prod.yml"
DB_USER="review"
DB_NAME="reviews"

WIPE_FEEDBACK=0
ASSUME_YES=0
for arg in "$@"; do
  case "$arg" in
    --wipe-feedback) WIPE_FEEDBACK=1 ;;
    --yes) ASSUME_YES=1 ;;
    *) echo "usage: $0 [--wipe-feedback] [--yes]"; exit 2 ;;
  esac
done

# 1) Guard: the stack must be down (only postgres/redis may run) — a live
# worker mid-job would write into half-truncated tables.
running="$($COMPOSE ps --status running --format '{{.Service}}' 2>/dev/null | grep -vE '^(postgres|redis)$' || true)"
if [ -n "$running" ]; then
  echo "REFUSING: stop these services first: $(echo "$running" | tr '\n' ' ')"
  echo "  $COMPOSE stop api worker scheduler nginx"
  exit 1
fi

# History tables. NOTE: no RESTART IDENTITY — see header.
TABLES=(
  pull_requests pending_reviews review_runs review_findings github_events
  review_jobs ticket_analyses ticket_issue_runs change_notes value_reports
  timesheet_review_runs timesheet_review_lines audit_runs audit_findings
  claude_spend ops_events weekly_reports prompt_versions
)
if [ "$WIPE_FEEDBACK" = "1" ]; then
  TABLES+=(muted_categories repo_review_memory review_feedback)
fi

echo "Will TRUNCATE (no identity restart): ${TABLES[*]}"
echo "Will keep: odoo_instances repositories schema_migrations core-knowledge admin_audit$(
  [ "$WIPE_FEEDBACK" = "0" ] && echo ' muted_categories repo_review_memory')"
echo "Will FLUSH the Redis RQ database."

if [ "$ASSUME_YES" != "1" ]; then
  printf 'Type the database name (%s) to proceed: ' "$DB_NAME"
  read -r answer
  [ "$answer" = "$DB_NAME" ] || { echo "aborted"; exit 1; }
fi

# 2) Backup first (same pipeline as the nightly cron; restore.sh undoes this).
./scripts/backup.sh

# 3) One TRUNCATE statement: CASCADE resolves FK order (findings→runs etc.).
joined="$(IFS=,; echo "${TABLES[*]}")"
$COMPOSE exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 \
  -c "TRUNCATE ${joined} CASCADE;"
echo "Truncated."

# 4) Queued RQ jobs reference truncated row ids — flush them.
$COMPOSE exec -T redis redis-cli FLUSHDB >/dev/null
echo "Redis RQ database flushed."

echo
echo "Done. Next steps:"
echo "  $COMPOSE up -d                       # start the stack"
echo "  re-click 'Create Issues' on in-flight Odoo tickets to re-link them"
echo "  (backup kept under \${REVA_BACKUP_DIR:-./backups}; scripts/restore.sh to undo)"
