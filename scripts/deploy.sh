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
for i in $(seq 1 12); do
    if $COMPOSE exec -T api curl -sf http://localhost:8080/health > /dev/null 2>&1; then
        echo "    API healthy."
        break
    fi
    echo "    Attempt $i/12 — waiting 5s..."
    sleep 5
done

echo "==> Service status:"
$COMPOSE ps

echo "==> Deploy complete."
