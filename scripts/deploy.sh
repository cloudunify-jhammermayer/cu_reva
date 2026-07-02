#!/usr/bin/env bash
# Deploy REVA to production.
# Run from the project root: ./scripts/deploy.sh
# Requires: docker, docker compose v2, .env with all required vars.

set -euo pipefail

COMPOSE="docker compose -f docker-compose.prod.yml"

echo "==> Pulling latest code..."
git pull origin main

echo "==> Building images..."
$COMPOSE build

echo "==> Stopping old containers (zero-downtime: postgres/redis stay up)..."
$COMPOSE stop api scheduler worker nginx

echo "==> Starting all services..."
$COMPOSE up -d

echo "==> Waiting for API health check..."
# The api image has no curl (python:3.14-slim); use urllib, matching the
# compose healthcheck. /health returns non-200 (raises) unless DB+Redis are up.
healthy=false
for i in $(seq 1 12); do
    if $COMPOSE exec -T api python -c "import urllib.request as u; u.urlopen('http://localhost:8080/health', timeout=3)" > /dev/null 2>&1; then
        echo "    API healthy."
        healthy=true
        break
    fi
    echo "    Attempt $i/12 — waiting 5s..."
    sleep 5
done

echo "==> Service status:"
$COMPOSE ps

if [ "$healthy" != true ]; then
    echo "==> Deploy FAILED: API did not become healthy." >&2
    exit 1
fi

echo "==> Deploy complete."
