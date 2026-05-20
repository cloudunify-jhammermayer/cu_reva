.PHONY: dev dev-build prod build deploy logs logs-api logs-scheduler logs-worker \
        shell-api shell-worker psql redis-cli test test-worker test-api test-scheduler \
        scale-workers status restart

# ── Local development ─────────────────────────────────────────────────────────

dev:
	docker compose up

dev-build:
	docker compose up --build

# ── Production ────────────────────────────────────────────────────────────────

prod:
	docker compose -f docker-compose.prod.yml up -d

build:
	docker compose -f docker-compose.prod.yml build

deploy:
	./scripts/deploy.sh

# ── Logs ──────────────────────────────────────────────────────────────────────

logs:
	docker compose logs -f

logs-api:
	docker compose logs -f api

logs-scheduler:
	docker compose logs -f scheduler

logs-worker:
	docker compose logs -f worker

# ── Shells ────────────────────────────────────────────────────────────────────

shell-api:
	docker compose exec api sh

shell-worker:
	docker compose exec worker sh

psql:
	docker compose exec postgres psql -U review reviews

redis-cli:
	docker compose exec redis redis-cli

# ── Tests (run locally, not in containers) ────────────────────────────────────

test: test-worker test-api test-scheduler

test-worker:
	cd worker && .venv/bin/python -m pytest tests/ -q

test-api:
	cd api && .venv/bin/python -m pytest tests/ -q

test-scheduler:
	cd scheduler && .venv/bin/python -m pytest tests/ -q

# ── Ops ───────────────────────────────────────────────────────────────────────

scale-workers:
	docker compose up -d --scale worker=$(N)

status:
	docker compose ps

restart:
	docker compose restart
