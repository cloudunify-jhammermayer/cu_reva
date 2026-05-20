#!/usr/bin/env bash
# Obtain the initial Let's Encrypt certificate.
# Run once on the server BEFORE starting the full docker-compose.prod.yml stack.
#
# Prerequisites:
#   - Port 80 is open and not bound by another process.
#   - REVA_DOMAIN DNS A record points to this server's public IP.
#   - .env is present with REVA_DOMAIN set.
#
# Usage: REVA_DOMAIN=reviews.example.com EMAIL=admin@example.com ./scripts/setup-letsencrypt.sh

set -euo pipefail

DOMAIN="${REVA_DOMAIN:-}"
EMAIL="${EMAIL:-}"

if [[ -z "$DOMAIN" ]]; then
    echo "ERROR: REVA_DOMAIN is not set. Export it or add it to .env."
    exit 1
fi

if [[ -z "$EMAIL" ]]; then
    echo "ERROR: EMAIL is not set. Export it: EMAIL=admin@example.com"
    exit 1
fi

echo "==> Obtaining certificate for $DOMAIN ..."

mkdir -p ./letsencrypt ./certbot-www

docker run --rm \
    -p 80:80 \
    -v "$(pwd)/letsencrypt:/etc/letsencrypt" \
    -v "$(pwd)/certbot-www:/var/www/certbot" \
    certbot/certbot certonly \
    --standalone \
    --email "$EMAIL" \
    --agree-tos \
    --no-eff-email \
    -d "$DOMAIN"

echo ""
echo "Certificate obtained. Start the stack with:"
echo "  docker compose -f docker-compose.prod.yml up -d"
