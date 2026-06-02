.PHONY: dev dev-build prod build deploy logs logs-api logs-scheduler logs-worker \
        shell-api shell-worker psql redis-cli test test-worker test-api test-scheduler \
        test-integration scale-workers status restart

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

# Real-Postgres concurrency tests (D1). Spins a throwaway PG, runs, tears down.
test-integration:
	docker rm -f reva_pg_test >/dev/null 2>&1 || true
	docker run -d --name reva_pg_test -e POSTGRES_USER=review -e POSTGRES_PASSWORD=test \
		-e POSTGRES_DB=reviews -p 55433:5432 postgres:16-alpine >/dev/null
	@for i in $$(seq 1 30); do docker exec reva_pg_test pg_isready -U review -d reviews >/dev/null 2>&1 && break; sleep 1; done
	-cd worker && REVA_TEST_POSTGRES_URL=postgresql://review:test@localhost:55433/reviews \
		.venv/bin/python -m pytest tests/test_pg_integration.py -q
	docker rm -f reva_pg_test >/dev/null 2>&1 || true

# ── Ops ───────────────────────────────────────────────────────────────────────

scale-workers:
	docker compose up -d --scale worker=$(N)

status:
	docker compose ps

restart:
	docker compose restart
