# 09 — Docker and Deployment

## Docker Compose (Production)

```yaml
# docker-compose.prod.yml

services:
  nginx:
    build: ./nginx
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./nginx/conf.d:/etc/nginx/conf.d:ro
      - letsencrypt_data:/etc/letsencrypt:ro
      - certbot_www:/var/www/certbot:ro
    depends_on:
      - api
    networks:
      - reviewer-net
    restart: unless-stopped

  certbot:
    image: certbot/certbot
    volumes:
      - letsencrypt_data:/etc/letsencrypt
      - certbot_www:/var/www/certbot
    entrypoint: "/bin/sh -c 'trap exit TERM; while :; do certbot renew; sleep 12h; done'"
    networks:
      - reviewer-net
    restart: unless-stopped

  api:
    build: ./api
    environment:
      DATABASE_URL: postgresql+asyncpg://review:${POSTGRES_PASSWORD}@postgres:5432/reviews
      DATABASE_SYNC_URL: postgresql://review:${POSTGRES_PASSWORD}@postgres:5432/reviews
      REDIS_URL: redis://redis:6379/0
      GITHUB_APP_ID: ${GITHUB_APP_ID}
      GITHUB_WEBHOOK_SECRET: ${GITHUB_WEBHOOK_SECRET}
      GITHUB_PRIVATE_KEY_PATH: /run/secrets/github_private_key
      DEBOUNCE_SECONDS: "600"
      GOOGLE_CHAT_WEBHOOK_URL: ${GOOGLE_CHAT_WEBHOOK_URL}
    secrets:
      - github_private_key
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_started
    networks:
      - reviewer-net
    restart: unless-stopped
    logging:
      driver: json-file
      options:
        max-size: "50m"
        max-file: "5"

  worker:
    build: ./worker
    environment:
      DATABASE_URL: postgresql://review:${POSTGRES_PASSWORD}@postgres:5432/reviews
      REDIS_URL: redis://redis:6379/0
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}
      GITHUB_APP_ID: ${GITHUB_APP_ID}
      GITHUB_PRIVATE_KEY_PATH: /run/secrets/github_private_key
      GOOGLE_CHAT_WEBHOOK_URL: ${GOOGLE_CHAT_WEBHOOK_URL}
    secrets:
      - github_private_key
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_started
    deploy:
      resources:
        limits:
          cpus: "2.0"
          memory: 1G
        reservations:
          cpus: "0.5"
          memory: 256M
    networks:
      - reviewer-net
    restart: unless-stopped
    logging:
      driver: json-file
      options:
        max-size: "50m"
        max-file: "5"

  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: reviews
      POSTGRES_USER: review
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U review -d reviews"]
      interval: 10s
      timeout: 5s
      retries: 5
    networks:
      - reviewer-net
    restart: unless-stopped
    logging:
      driver: json-file
      options:
        max-size: "20m"
        max-file: "3"

  redis:
    image: redis:7-alpine
    command: redis-server --maxmemory 256mb --maxmemory-policy allkeys-lru
    networks:
      - reviewer-net
    restart: unless-stopped

volumes:
  postgres_data:
  letsencrypt_data:
  certbot_www:

networks:
  reviewer-net:
    driver: bridge

secrets:
  github_private_key:
    file: ./secrets/github-app-private-key.pem
```

## Nginx Configuration

### Main Config

```nginx
# nginx/nginx.conf
worker_processes auto;
error_log /var/log/nginx/error.log warn;
pid /var/run/nginx.pid;

events {
    worker_connections 1024;
}

http {
    include /etc/nginx/mime.types;
    default_type application/octet-stream;

    log_format json escape=json '{'
        '"time":"$time_iso8601",'
        '"remote_addr":"$remote_addr",'
        '"method":"$request_method",'
        '"uri":"$request_uri",'
        '"status":$status,'
        '"body_bytes_sent":$body_bytes_sent,'
        '"request_time":$request_time,'
        '"upstream_response_time":"$upstream_response_time",'
        '"http_user_agent":"$http_user_agent"'
    '}';

    access_log /var/log/nginx/access.log json;

    sendfile on;
    keepalive_timeout 65;
    client_max_body_size 10m;  # webhook payloads can be large

    include /etc/nginx/conf.d/*.conf;
}
```

### Site Config

```nginx
# nginx/conf.d/reviewer.conf

# Rate limiting for webhook endpoint
limit_req_zone $binary_remote_addr zone=webhooks:10m rate=30r/m;

# Redirect HTTP to HTTPS
server {
    listen 80;
    server_name reviews.yourdomain.com;

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        return 301 https://$host$request_uri;
    }
}

# HTTPS server
server {
    listen 443 ssl http2;
    server_name reviews.yourdomain.com;

    ssl_certificate /etc/letsencrypt/live/reviews.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/reviews.yourdomain.com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;

    # Webhook endpoint (public, rate-limited)
    location /webhooks/ {
        limit_req zone=webhooks burst=10 nodelay;
        proxy_pass http://api:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Internal API (restrict to internal network or VPN)
    location /api/ {
        # Option 1: Allow only from internal IPs
        # allow 10.0.0.0/8;
        # allow 172.16.0.0/12;
        # allow 192.168.0.0/16;
        # deny all;

        # Option 2: No restriction (if server is behind firewall)
        proxy_pass http://api:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Health check
    location /health {
        proxy_pass http://api:8080;
    }

    # Deny everything else
    location / {
        return 404;
    }
}
```

### Nginx Dockerfile

```dockerfile
# nginx/Dockerfile
FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/nginx.conf
COPY conf.d/ /etc/nginx/conf.d/
```

## Let's Encrypt Setup

### Initial Certificate

Run once on the host before starting Docker Compose:

```bash
#!/bin/bash
# scripts/setup-letsencrypt.sh

DOMAIN="reviews.yourdomain.com"
EMAIL="admin@yourdomain.com"

# Create directories
mkdir -p ./nginx/certs

# Get initial certificate using standalone mode
docker run --rm -p 80:80 \
  -v "$(pwd)/letsencrypt:/etc/letsencrypt" \
  certbot/certbot certonly \
  --standalone \
  --email "$EMAIL" \
  --agree-tos \
  --no-eff-email \
  -d "$DOMAIN"

echo "Certificate obtained. Start Docker Compose."
```

The certbot sidecar container handles automatic renewal every 12 hours. Nginx reloads are handled via a cron job on the host:

```bash
# Cron: reload nginx after certbot renewal
0 */12 * * * docker compose exec nginx nginx -s reload
```

## Environment File

```bash
# .env.example (copy to .env, fill in values, chmod 600)

# PostgreSQL
POSTGRES_PASSWORD=generate-a-strong-password-here

# GitHub App
GITHUB_APP_ID=123456
GITHUB_WEBHOOK_SECRET=generate-with-openssl-rand-hex-32

# Claude
ANTHROPIC_API_KEY=sk-ant-...

# Google Chat
GOOGLE_CHAT_WEBHOOK_URL=https://chat.googleapis.com/v1/spaces/SPACE_ID/messages?key=KEY&token=TOKEN
```

## API Dockerfile

```dockerfile
# api/Dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY ../db/migrations/ ./db/migrations/

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "2"]
```

## Deploy Script

```bash
#!/bin/bash
# scripts/deploy.sh

set -euo pipefail

PROJECT_DIR="/opt/claude-reviewer"
COMPOSE_FILE="docker-compose.prod.yml"

cd "$PROJECT_DIR"

echo "Pulling latest code..."
git pull origin main

echo "Building images..."
docker compose -f "$COMPOSE_FILE" build

echo "Running migrations..."
docker compose -f "$COMPOSE_FILE" run --rm api python -m app.migrate

echo "Restarting services..."
docker compose -f "$COMPOSE_FILE" up -d

echo "Waiting for health check..."
sleep 5
curl -sf http://localhost:8080/health || echo "WARNING: Health check failed"

echo "Deploy complete."
```

## Makefile

```makefile
# Makefile

.PHONY: dev prod build deploy logs backup

dev:
	docker compose up --build

prod:
	docker compose -f docker-compose.prod.yml up -d

build:
	docker compose -f docker-compose.prod.yml build

deploy:
	./scripts/deploy.sh

logs:
	docker compose -f docker-compose.prod.yml logs -f

logs-api:
	docker compose -f docker-compose.prod.yml logs -f api

logs-worker:
	docker compose -f docker-compose.prod.yml logs -f worker

backup:
	./scripts/backup.sh

scale-workers:
	docker compose -f docker-compose.prod.yml up -d --scale worker=$(N)

psql:
	docker compose -f docker-compose.prod.yml exec postgres psql -U review reviews

restart:
	docker compose -f docker-compose.prod.yml restart

status:
	docker compose -f docker-compose.prod.yml ps
```

## Log Rotation

Docker's json-file driver handles per-container log rotation (configured in docker-compose.yml with `max-size: 50m, max-file: 5`). Host-level system logs use standard logrotate.

For structured log aggregation (optional future improvement), pipe Docker logs to a file and use a log shipper:

```bash
# View structured logs
docker compose logs worker --no-log-prefix | jq '.'

# Save last hour of logs
docker compose logs --since 1h worker > /var/log/claude-reviewer/worker-$(date +%Y%m%d_%H).json
```

## Server Prerequisites

Minimum Hetzner server requirements:
- 4 vCPU, 8GB RAM (CX31 or similar)
- 80GB SSD
- Ubuntu 24.04 LTS
- Docker Engine 26+
- Docker Compose v2
- Public IP with DNS A record pointing to `reviews.yourdomain.com`
- Firewall: allow 80, 443 inbound; restrict SSH to your IP
