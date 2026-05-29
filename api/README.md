# api/ — FastAPI webhook receiver + internal REST API

Two responsibilities in one process:

1. **`POST /webhooks/github`** — the public ingress. Verifies the GitHub HMAC
   signature, stores every delivery, and turns reviewable events into
   `pending_reviews` rows (for the scheduler) or directly enqueued jobs.
2. **`/api/v1/*`** — the internal read/admin API consumed by the Go TUI
   (metrics, reviews, findings, failures, repos, pending, tickets, plus admin
   actions like requeue and on-demand audit).

The API does **no LLM work** — it records events and enqueues jobs, keeping the
heavy, retry-prone work isolated in the worker. The one exception: on a
`/review` comment it posts an immediate best-effort "on it" acknowledgement
comment so the developer gets instant feedback while the review is queued.

## Layout

| Path | Role |
|---|---|
| `app/main.py` | Lifespan: load settings, run DB migrations, build the Redis queue + GitHub client onto `app.state`. Mounts routers. |
| `app/settings.py` | Frozen `Settings`; `from_env()`. Fails closed when `REVA_REQUIRE_API_KEY=true` but `REVA_API_KEY` is empty. |
| `app/security.py` | `verify_signature` — constant-time HMAC-SHA256 over the **raw** request body. |
| `app/dependencies.py` | DI providers (`get_db`, `get_settings`, `get_queue`, `get_github_client`) and `require_api_key` (Bearer-token gate on `/api/v1`). |
| `app/routes/webhooks.py` | Signature check → record event → dispatch. Blocking DB work runs in the threadpool so it never stalls the event loop. PR pushes upsert a debounced `pending_review`; `/review` & `/deep-review` comments trigger immediately (gated to OWNER/MEMBER/COLLABORATOR, bots skipped); inline-comment replies enqueue `run_comment_reply`. |
| `app/routes/health.py` | `GET /health` — `SELECT 1` liveness. |
| `app/routes/v1/*` | One router per resource: metrics, reviews, findings, failures, repos, pending, ticket_analyses, admin. |
| `app/queries/*` | Read-side SQL (kept out of the route handlers). |
| `app/schemas/*` | Pydantic response models. |

## Auth — why it matters

`/api/v1` exposes operational data and admin actions (requeue, audit), and in
production it's reachable through nginx. `require_api_key` enforces an
`Authorization: Bearer <REVA_API_KEY>`. It is a **no-op when the key is unset**
(convenient for local dev), so production sets `REVA_REQUIRE_API_KEY=true`,
which makes the app refuse to start without a key — fail closed, not open.
`/webhooks/github` is authenticated separately by the GitHub HMAC signature.

## Tests

```bash
cd api && python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/     # 57
```

`TestClient` + SQLite in-memory (`StaticPool`, `check_same_thread=False` so the
threadpool handlers see the same DB). No network, Redis, or Docker required.
