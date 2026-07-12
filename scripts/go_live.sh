#!/usr/bin/env bash
# One-shot, manually-triggered go-live reset: deploy the latest code, wipe the
# operational history (clean_slate.sh — backs up first, keeps instances/config,
# never reuses ids), restart the stack, and verify.
#
#   cd ~/cu_reva && git pull && ./scripts/go_live.sh [--wipe-feedback] [--yes]
#
# Flags are passed through to clean_slate.sh. Without --yes it asks for the
# typed database name before truncating. Safe to re-run: deploy is a no-op on
# unchanged code and truncating empty tables is harmless (a fresh backup is
# taken each run).

set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE="docker compose -f docker-compose.prod.yml"

echo "==> [1/4] Deploy latest code (pull + build + rolling restart + health gate)"
./scripts/deploy.sh

echo "==> [2/4] Stop the stack for the reset (postgres/redis stay up)"
$COMPOSE stop api worker scheduler nginx

echo "==> [3/4] Clean slate (backup -> truncate history -> flush RQ)"
./scripts/clean_slate.sh "$@"

echo "==> [4/4] Start + verify"
$COMPOSE up -d
healthy=false
for i in $(seq 1 12); do
    if $COMPOSE exec -T api python -c "import urllib.request as u; u.urlopen('http://localhost:8080/health', timeout=3)" >/dev/null 2>&1; then
        healthy=true; break
    fi
    echo "    waiting for API health ($i/12)..."; sleep 5
done
[ "$healthy" = "true" ] || { echo "API did not become healthy — check $COMPOSE logs api"; exit 1; }

$COMPOSE exec -T postgres psql -U review -d reviews -P pager=off -c "
SELECT (SELECT count(*) FROM review_runs)        AS reviews,
       (SELECT count(*) FROM ticket_analyses)    AS analyses,
       (SELECT count(*) FROM ticket_issue_runs)  AS issue_runs,
       (SELECT count(*) FROM odoo_instances)     AS instances_kept,
       (SELECT count(*) FROM repositories)       AS repos_kept,
       (SELECT count(*) FROM schema_migrations)  AS migrations;"

echo "==> Go-live reset complete. History empty, instances/config kept, API healthy."
