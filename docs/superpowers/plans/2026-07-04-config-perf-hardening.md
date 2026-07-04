# Config & Performance Hardening Batch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix four bug-level config defects, land five cheap performance/cost wins, sync the config surface, and add per-Odoo-instance quotas.

**Architecture:** Sixteen small, independently shippable tasks in four parts (A defects → B perf/cost → C hygiene → D quotas). Parts A–C are mostly one-file changes with targeted tests; Part D is a thin feature slice over the existing multi-instance plumbing (columns → writers → API gates → worker gates → TUI).

**Tech Stack:** docker compose, Python 3.14 (FastAPI/RQ/SQLAlchemy/httpx), Go Bubble Tea (TUI). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-04-config-perf-hardening-design.md` — read it first.

## Global Constraints

- Per-service venvs: `cd worker && .venv/bin/python -m pytest tests/ -q` (same for `api/`, `scheduler/`). Missing venv: `python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`.
- Shared `reva/` changes affect all three services → final gate `make test` + `ruff check reva worker/worker api/app scheduler/scheduler`.
- TUI gate: `cd tui && go build ./... && go vet ./... && go test ./...`.
- Migrations idempotent; ORM mirrors them (SQLite `_PK` variant); **check the next free migration number before creating one** — as of writing 025/026 are free but the pending timesheet and metasoul plans both also claim 025.
- Both compose files must stay parseable: `docker compose -f docker-compose.yml config -q` and `docker compose -f docker-compose.prod.yml config -q` after every compose edit.
- Quota semantics: `NULL` = unlimited; budget = rolling 24h sum of `estimated_cost_usd` over the instance-scoped run tables (`ticket_analyses`, `ticket_issue_runs`).
- Commit after every task; each task is independently revertable.

---

### Task 1: A1 — worker stop_grace_period

**Files:**
- Modify: `docker-compose.prod.yml:189-193`

**Interfaces:** none (compose-only).

- [ ] **Step 1: Edit the grace period + comment**

Replace lines 189–193 (the comment block + `stop_grace_period`) with:

```yaml
    # On deploy/stop, give RQ's warm shutdown time to finish an in-flight review
    # before Docker SIGKILLs it. Must exceed REVIEW_JOB_TIMEOUT (= LOCK_WAIT_BUDGET
    # + SUBPROCESS_TIMEOUT + JOB_TIMEOUT_BUFFER = 2100s, reva/claude_code_runner.py);
    # 2160s = timeout + 60s margin. A review still in flight past this is SIGKILLed
    # and then swept by the scheduler's stale-running reaper (REVA_STALE_RUNNING_SECONDS).
    stop_grace_period: 2160s
```

- [ ] **Step 2: Verify compose parses and the value took**

Run: `docker compose -f docker-compose.prod.yml config | grep -A1 stop_grace_period`
Expected: `stop_grace_period: 36m0s` (compose normalizes 2160s to 36m)

- [ ] **Step 3: Commit**

```bash
git add docker-compose.prod.yml
git commit -m "fix(compose): worker stop_grace_period covers the real 2100s job timeout"
```

---

### Task 2: A2 — ticket_analyses.created_at index

**Files:**
- Create: `db/migrations/025_ticket_analyses_created_index.sql` (verify 025 is free: `ls db/migrations/ | sort | tail -3`)
- Modify: `reva/db/models.py:441-464` (`TicketAnalysis.__table_args__`)
- Test: `worker/tests/test_db.py` (append one test)

**Interfaces:** none new.

- [ ] **Step 1: Write the failing test**

Append to `worker/tests/test_db.py`:

```python
def test_ticket_analyses_has_created_at_index():
    """The list endpoint orders by created_at DESC; migration 025 backs it."""
    from reva.db.models import TicketAnalysis

    names = {idx.name for idx in TicketAnalysis.__table__.indexes}
    assert "idx_ticket_analyses_created_at" in names
```

- [ ] **Step 2: Run to verify failure**

Run: `cd worker && .venv/bin/python -m pytest tests/test_db.py::test_ticket_analyses_has_created_at_index -q`
Expected: FAIL — `AssertionError`

- [ ] **Step 3: Create the migration**

`db/migrations/025_ticket_analyses_created_index.sql`:

```sql
-- The ticket-analyses list endpoint orders by created_at DESC
-- (api/app/queries/ticket_analyses.py); every other run table already has a
-- created_at index — this one was missed. Mirrors
-- reva/db/models.py::TicketAnalysis.
CREATE INDEX IF NOT EXISTS idx_ticket_analyses_created_at
    ON ticket_analyses (created_at);
```

- [ ] **Step 4: Add the ORM index**

In `reva/db/models.py`, inside `TicketAnalysis.__table_args__`, after
`Index("idx_ticket_analyses_ticket_id", "ticket_id"),` add:

```python
        # List endpoint orders by created_at DESC — migration 025.
        Index("idx_ticket_analyses_created_at", "created_at"),
```

- [ ] **Step 5: Run to verify pass**

Run: `cd worker && .venv/bin/python -m pytest tests/test_db.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add db/migrations/025_ticket_analyses_created_index.sql reva/db/models.py worker/tests/test_db.py
git commit -m "fix(db): index ticket_analyses.created_at (unindexed list sort)"
```

---

### Task 3: A3 — worker healthcheck

**Files:**
- Create: `scripts/worker_healthcheck.py`
- Modify: `docker-compose.prod.yml` (worker service), `docker-compose.yml` (worker service), `worker/Dockerfile` (copy the script)
- Test: `worker/tests/test_worker_healthcheck.py`

**Interfaces:**
- Produces: `scripts/worker_healthcheck.py::check(redis_url: str, hostname: str, connection_factory=None) -> bool` (importable, exit-code CLI wrapper in `__main__`).

- [ ] **Step 1: Write the failing test**

Create `worker/tests/test_worker_healthcheck.py`:

```python
"""Worker liveness healthcheck: an RQ worker key for this hostname must exist.

RQ refreshes each worker's `rq:worker:<name>` hash with a TTL; key-existence
therefore means the worker process is alive. Worker names start with the
hostname (RQ default: `<hostname>.<pid>`... older RQ: hostname-based) — match
on prefix after the `rq:worker:` namespace.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "worker_healthcheck",
    Path(__file__).resolve().parents[2] / "scripts" / "worker_healthcheck.py",
)
worker_healthcheck = importlib.util.module_from_spec(_SPEC)
sys.modules["worker_healthcheck"] = worker_healthcheck
_SPEC.loader.exec_module(worker_healthcheck)


class FakeRedis:
    def __init__(self, keys: list[bytes]) -> None:
        self._keys = keys

    def scan_iter(self, match: str, count: int = 100):
        # emulate glob match on rq:worker:*
        yield from self._keys


def test_healthy_when_worker_key_matches_hostname():
    fake = FakeRedis([b"rq:worker:abc123.42"])
    assert worker_healthcheck.check(
        "redis://ignored", "abc123", connection_factory=lambda url: fake
    ) is True


def test_unhealthy_when_no_key_for_hostname():
    fake = FakeRedis([b"rq:worker:otherhost.7"])
    assert worker_healthcheck.check(
        "redis://ignored", "abc123", connection_factory=lambda url: fake
    ) is False


def test_unhealthy_when_redis_unreachable():
    def boom(url):
        raise ConnectionError("redis down")

    assert worker_healthcheck.check(
        "redis://ignored", "abc123", connection_factory=boom
    ) is False
```

- [ ] **Step 2: Run to verify failure**

Run: `cd worker && .venv/bin/python -m pytest tests/test_worker_healthcheck.py -q`
Expected: FAIL — `FileNotFoundError` (script does not exist)

- [ ] **Step 3: Create `scripts/worker_healthcheck.py`**

```python
"""Container healthcheck for the RQ worker.

RQ registers each worker under `rq:worker:<name>` (name defaults to
`<hostname>.<pid>`) and keeps the key alive with a TTL-refreshed heartbeat —
so "a worker key for this container's hostname exists" means the worker
process is alive. Exit 0 = healthy, 1 = unhealthy (incl. Redis unreachable).
"""

from __future__ import annotations

import os
import socket
import sys


def _default_connection(url: str):
    from redis import Redis

    return Redis.from_url(url, socket_connect_timeout=5, socket_timeout=5)


def check(redis_url: str, hostname: str, connection_factory=None) -> bool:
    factory = connection_factory or _default_connection
    try:
        conn = factory(redis_url)
        prefix = f"rq:worker:{hostname}".encode()
        for key in conn.scan_iter(match="rq:worker:*", count=100):
            if key.startswith(prefix):
                return True
        return False
    except Exception:
        return False


if __name__ == "__main__":
    url = os.environ.get("REDIS_URL", "")
    ok = bool(url) and check(url, socket.gethostname())
    sys.exit(0 if ok else 1)
```

- [ ] **Step 4: Run to verify pass**

Run: `cd worker && .venv/bin/python -m pytest tests/test_worker_healthcheck.py -q`
Expected: PASS (3 tests)

- [ ] **Step 5: Ship the script in the worker image**

In `worker/Dockerfile`, next to the existing `COPY` lines that bring in
`worker/` source (locate them with `grep -n COPY worker/Dockerfile`), add:

```dockerfile
COPY scripts/worker_healthcheck.py /app/scripts/worker_healthcheck.py
```

- [ ] **Step 6: Wire the healthcheck in BOTH compose files**

In `docker-compose.prod.yml`, worker service, after `stop_grace_period` add:

```yaml
    healthcheck:
      # Liveness via RQ's TTL-refreshed rq:worker:<hostname>.* Redis key.
      test: ["CMD-SHELL", "python /app/scripts/worker_healthcheck.py"]
      interval: 60s
      timeout: 10s
      retries: 3
      start_period: 60s
```

Same block in `docker-compose.yml`'s worker service (after `restart: unless-stopped` or the volumes block — match surrounding indentation).

- [ ] **Step 7: Verify compose parses**

Run: `docker compose -f docker-compose.yml config -q && docker compose -f docker-compose.prod.yml config -q`
Expected: no output, exit 0

- [ ] **Step 8: Commit**

```bash
git add scripts/worker_healthcheck.py worker/Dockerfile docker-compose.yml docker-compose.prod.yml worker/tests/test_worker_healthcheck.py
git commit -m "fix(worker): liveness healthcheck via RQ worker key"
```

---

### Task 4: A4 — Redis pressure (failure TTL + maxmemory)

**Files:**
- Modify: `api/app/routes/v1/ticket_analyses.py:41-43`, `api/app/routes/v1/ticket_issues.py` (the `_FAILURE_TTL` constant)
- Modify: `docker-compose.prod.yml:255,261-265` (redis command + limits)
- Test: `api/tests/test_v1_ticket_analyses.py` (append one test)

**Interfaces:** none new.

- [ ] **Step 1: Write the failing test**

Append to `api/tests/test_v1_ticket_analyses.py`:

```python
def test_failure_ttl_bounded_to_one_day(client_db_queue):
    """Failed-job Redis payloads carry full request bodies (incl. base64
    attachments); with noeviction, week-long retention can fill maxmemory and
    reject all enqueues. Requeue rebuilds params from the DB row, so 24h is
    plenty."""
    client, _, queue, headers = client_db_queue
    client.post("/api/v1/ticket-analysis", json=BASE_PAYLOAD, headers=headers)
    _, _, kwargs = queue.enqueued[0]
    assert kwargs["failure_ttl"] <= 24 * 3600
```

- [ ] **Step 2: Run to verify failure**

Run: `cd api && .venv/bin/python -m pytest tests/test_v1_ticket_analyses.py::test_failure_ttl_bounded_to_one_day -q`
Expected: FAIL — `assert 604800 <= 86400`

- [ ] **Step 3: Change the constants**

In `api/app/routes/v1/ticket_analyses.py`, replace the `_FAILURE_TTL` line and its comment:

```python
# Failed jobs keep their serialized args (incl. the customer ticket text and
# any base64 attachment) in Redis; requeue rebuilds params from the DB row, so
# retention buys nothing past debugging. 24h keeps redis noeviction headroom.
_FAILURE_TTL = 24 * 3600
```

Apply the same replacement to the `_FAILURE_TTL` definition in `api/app/routes/v1/ticket_issues.py` (locate: `grep -n _FAILURE_TTL api/app/routes/v1/ticket_issues.py`).

- [ ] **Step 4: Bump Redis memory in prod compose**

In `docker-compose.prod.yml` redis service: change `--maxmemory 256mb` to `--maxmemory 512mb` in the `command:` line, and the deploy limit `memory: 320M` to `memory: 640M`.

- [ ] **Step 5: Run to verify pass**

Run: `cd api && .venv/bin/python -m pytest tests/test_v1_ticket_analyses.py tests/test_v1_ticket_issues.py -q && docker compose -f docker-compose.prod.yml config -q`
Expected: PASS, compose parses

- [ ] **Step 6: Commit**

```bash
git add api/app/routes/v1/ticket_analyses.py api/app/routes/v1/ticket_issues.py docker-compose.prod.yml api/tests/test_v1_ticket_analyses.py
git commit -m "fix(redis): 24h failure_ttl + 512mb maxmemory (noeviction headroom)"
```

---

### Task 5: B5 — blob-filtered partial clones

**Files:**
- Modify: `reva/claude_code_runner.py:213-221` (the clone branch in `ensure_repo`)
- Test: `worker/tests/test_claude_code_runner.py` (append one test)

**Interfaces:** none new.

- [ ] **Step 1: Write the failing test**

Append to `worker/tests/test_claude_code_runner.py` (reuse the file's existing
`ClaudeCodeRunner` construction pattern if one exists; otherwise this
self-contained version):

```python
def test_clone_uses_blob_filter(tmp_path, monkeypatch):
    """B5: new clones are partial (--filter=blob:none) — full history metadata
    (delta ancestry checks still work) without downloading every blob."""
    from reva.claude_code_runner import ClaudeCodeRunner

    runner = ClaudeCodeRunner(
        repo_cache_dir=str(tmp_path), api_key="k",
        skills_dir=str(tmp_path), prompts_dir=str(tmp_path),
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(runner, "_run_git_transient", lambda args: calls.append(list(args)))
    monkeypatch.setattr(runner, "_run_git_permanent", lambda args: calls.append(list(args)))

    runner.ensure_repo("owner", "repo", "a" * 40, token="tok")

    clone = next(c for c in calls if "clone" in c)
    assert "--filter=blob:none" in clone
    # The filter must sit between `clone` and the URL (git flag ordering).
    assert clone.index("clone") < clone.index("--filter=blob:none")
```

- [ ] **Step 2: Run to verify failure**

Run: `cd worker && .venv/bin/python -m pytest tests/test_claude_code_runner.py::test_clone_uses_blob_filter -q`
Expected: FAIL — `StopIteration` or assert (no `--filter=blob:none`)

- [ ] **Step 3: Change the clone invocation**

In `reva/claude_code_runner.py` `ensure_repo`, replace:

```python
                self._run_git_transient(auth_args + ["clone", clean_url, repo_path])
```

with:

```python
                # Partial clone: full commit history (delta-base ancestry
                # checks keep working) but blobs download on demand at
                # checkout — faster first clone, less repo-cache disk. The
                # on-demand fetches hit the same allowlisted GitHub host, so
                # this composes with the A2 egress lock.
                self._run_git_transient(
                    auth_args + ["clone", "--filter=blob:none", clean_url, repo_path]
                )
```

- [ ] **Step 4: Run to verify pass**

Run: `cd worker && .venv/bin/python -m pytest tests/test_claude_code_runner.py -q`
Expected: PASS (whole file — existing clone tests must still pass; if one asserts the exact clone argv, update it to include the filter flag)

- [ ] **Step 5: Commit**

```bash
git add reva/claude_code_runner.py worker/tests/test_claude_code_runner.py
git commit -m "perf(worker): blob-filtered partial clones for the repo cache"
```

Note for the operator (goes in the PR/commit body, not code): staging live-gate — run one full review on a repo not yet in the cache; confirm the clone is filtered (`git -C /repos/<owner>/<name> config remote.origin.partialclonefilter` → `blob:none`) and the review completes.

---

### Task 6: B6 — verifier prompt caching

**Files:**
- Modify: `reva/finding_verifier.py:199,226-228`
- Test: `worker/tests/test_finding_verifier.py` (append)

**Interfaces:** none new.

- [ ] **Step 1: Write the failing test**

Append to `worker/tests/test_finding_verifier.py` (reuse the file's existing
fake-Claude pattern if present; otherwise this self-contained fake):

```python
def test_system_prompts_are_cache_controlled():
    """B6: the verifier makes up to ~20 Haiku calls per review with an identical
    static system prompt — mark it ephemeral-cached like the other
    Messages-API paths. (If the prefix is under the model's minimum cacheable
    length this is a silent no-op — measure on staging per the spec.)"""
    from reva.finding_verifier import FindingVerifier, StoredFinding

    captured = {}

    class _Fake:
        def review(self, *, system_blocks, user_prompt, tools, tool_choice,
                   model, max_tokens):
            captured["system_blocks"] = system_blocks
            class R:
                model = "claude-haiku-4-5"
                input_tokens = output_tokens = 0
                cache_read_tokens = cache_creation_tokens = 0
                tool_use_input = {"resolved": True, "reason": "x"}
            return R()

    finding = StoredFinding(file_path="a.py", line_start=1, title="t",
                            body="b", severity="major", category="bug")
    FindingVerifier(_Fake()).is_resolved(finding, "content")
    assert captured["system_blocks"][0]["cache_control"] == {"type": "ephemeral"}

    captured.clear()

    class _FakePresent(_Fake):
        def review(self, **kwargs):
            captured["system_blocks"] = kwargs["system_blocks"]
            class R:
                model = "claude-haiku-4-5"
                input_tokens = output_tokens = 0
                cache_read_tokens = cache_creation_tokens = 0
                tool_use_input = {"substantiated": True, "reason": "x"}
            return R()

    FindingVerifier(_FakePresent()).is_substantiated(finding, "content")
    assert captured["system_blocks"][0]["cache_control"] == {"type": "ephemeral"}
```

- [ ] **Step 2: Run to verify failure**

Run: `cd worker && .venv/bin/python -m pytest tests/test_finding_verifier.py::test_system_prompts_are_cache_controlled -q`
Expected: FAIL — `KeyError: 'cache_control'`

- [ ] **Step 3: Add the cache markers**

In `reva/finding_verifier.py::is_resolved`, replace:

```python
        system_blocks: list[ContentBlock] = [{"type": "text", "text": _SYSTEM_PROMPT}]
```

with:

```python
        # Static across every call of a run — cache it (same pattern as the
        # ticket analyzer). Below the model's minimum cacheable prefix this is
        # a silent no-op; see the spec's B6 acceptance criterion.
        system_blocks: list[ContentBlock] = [
            {"type": "text", "text": _SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
        ]
```

and in `is_substantiated`, replace:

```python
        system_blocks: list[ContentBlock] = [
            {"type": "text", "text": _VERIFY_PRESENT_SYSTEM_PROMPT}
        ]
```

with:

```python
        system_blocks: list[ContentBlock] = [
            {"type": "text", "text": _VERIFY_PRESENT_SYSTEM_PROMPT,
             "cache_control": {"type": "ephemeral"}}
        ]
```

- [ ] **Step 4: Run to verify pass**

Run: `cd worker && .venv/bin/python -m pytest tests/test_finding_verifier.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add reva/finding_verifier.py worker/tests/test_finding_verifier.py
git commit -m "perf(verifier): cache the static verifier system prompts"
```

Operator note for the PR body: staging measurement — on a review with ≥3
verified findings check `cache_read_tokens > 0` on calls 2+; if it stays 0
(prefix under Haiku's 2048-token cache minimum), revert this commit and record
the measurement.

---

### Task 7: B7 — two worker replicas (prod)

**Files:**
- Modify: `docker-compose.prod.yml` (worker service `deploy:` block)

**Interfaces:** none (compose-only).

- [ ] **Step 1: Confirm the prerequisite**

Run: `grep -n container_name docker-compose.prod.yml`
Expected: no output (replicas are incompatible with `container_name`; if any service has one on the worker, remove it).

- [ ] **Step 2: Add replicas**

In the worker service's `deploy:` block, add `replicas: 2` above `resources:`:

```yaml
    deploy:
      # Two concurrent jobs: a customer-facing ticket analysis no longer queues
      # behind a 35-min Opus audit. Cross-replica safety is designed in
      # (advisory repo locks, atomic claims, is_already_posted) and was
      # validated 2026-06-14. NOTE: resource limits below are PER REPLICA.
      replicas: 2
      resources:
        limits:
          cpus: "2.0"
          memory: 1G
        reservations:
          cpus: "0.5"
          memory: 256M
```

- [ ] **Step 3: Verify**

Run: `docker compose -f docker-compose.prod.yml config | grep -B2 -A2 replicas`
Expected: `replicas: 2` present, config parses.

- [ ] **Step 4: Commit**

```bash
git add docker-compose.prod.yml
git commit -m "perf(worker): 2 replicas — second concurrent job slot"
```

Operator note for the PR body: confirm prod-host memory headroom before deploy (2 × 1G worker limits alongside postgres 1G, api 512M, redis 640M).

---

### Task 8: B8 — API uvicorn workers

**Files:**
- Modify: `api/Dockerfile` (last line)

**Interfaces:** none.

- [ ] **Step 1: Edit the CMD**

Replace the final line of `api/Dockerfile`:

```dockerfile
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
```

with:

```dockerfile
# Two processes: webhook bursts + TUI polling + docs traffic stop sharing one
# event loop / sync-dep threadpool (the container has a 2-CPU limit). The
# in-memory rate limiter becomes per-process — it is documented best-effort;
# nginx limit_req zones remain the real gate.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "2"]
```

- [ ] **Step 2: Verify the image builds**

Run: `docker build -f api/Dockerfile -t reva-api-test . && docker rmi reva-api-test`
Expected: build succeeds. (Skip if Docker unavailable in the environment; then verification is the next prod deploy.)

- [ ] **Step 3: Commit**

```bash
git add api/Dockerfile
git commit -m "perf(api): uvicorn --workers 2"
```

---

### Task 9: B9 — strict structured outputs

**Files:**
- Modify: `reva/ticket_tool.py:48-52`, `reva/ticket_issue_tool.py:39-43`, `reva/finding_verifier.py:54-71,95-112`
- Test: `worker/tests/test_strict_tools.py` (new)

**Interfaces:** none new (the tool dicts gain a `"strict": True` key).

- [ ] **Step 1: Write the failing test**

Create `worker/tests/test_strict_tools.py`:

```python
"""B9: every Messages-API forced-tool definition opts into strict structured
outputs — the API then guarantees schema-conformant tool input, eliminating
the JSON-string-list workaround class of PermanentErrors over time."""

from __future__ import annotations

from reva.finding_verifier import _VERIFY_PRESENT_TOOL, _VERIFY_TOOL
from reva.ticket_issue_tool import build_ticket_issue_tool_schema
from reva.ticket_tool import build_ticket_tool_schema


def test_ticket_tool_is_strict():
    assert build_ticket_tool_schema()["strict"] is True


def test_ticket_issue_tool_is_strict():
    assert build_ticket_issue_tool_schema()["strict"] is True


def test_verifier_tools_are_strict():
    assert _VERIFY_TOOL["strict"] is True
    assert _VERIFY_PRESENT_TOOL["strict"] is True
```

- [ ] **Step 2: Run to verify failure**

Run: `cd worker && .venv/bin/python -m pytest tests/test_strict_tools.py -q`
Expected: FAIL — `KeyError: 'strict'`

- [ ] **Step 3: Add the flag to all four tool definitions**

`reva/ticket_tool.py` — in the returned dict of `build_ticket_tool_schema`:

```python
    return {
        "name": TICKET_TOOL_NAME,
        "description": _TOOL_DESCRIPTION,
        # Strict structured outputs: the API validates tool input against the
        # schema server-side, so list-as-JSON-string drift can't reach us.
        "strict": True,
        "input_schema": input_schema,
    }
```

`reva/ticket_issue_tool.py` — same addition in `build_ticket_issue_tool_schema`'s returned dict.

`reva/finding_verifier.py` — add `"strict": True,` after the `"description"` key in both `_VERIFY_TOOL` and `_VERIFY_PRESENT_TOOL` dicts.

- [ ] **Step 4: Run to verify pass**

Run: `cd worker && .venv/bin/python -m pytest tests/test_strict_tools.py tests/test_ticket_analyzer.py tests/test_finding_verifier.py tests/test_ticket_issue_planner.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add reva/ticket_tool.py reva/ticket_issue_tool.py reva/finding_verifier.py worker/tests/test_strict_tools.py
git commit -m "feat(claude): strict structured outputs on all forced-tool paths"
```

Operator note for the PR body: staging live-gate — run one real ticket
analysis and one verifier call; if the API rejects a schema under strict mode
(4xx naming the tool), fix that schema (strict mode supports a JSON-Schema
subset) before prod.

---

### Task 10: C10 — config surface sync

**Files:**
- Rewrite: `.env.example`
- Modify: `docker-compose.yml` (worker env), `docker-compose.prod.yml` (worker env)
- Test: `worker/tests/test_env_example.py` (new)

**Interfaces:** none new.

- [ ] **Step 1: Write the failing drift test**

Create `worker/tests/test_env_example.py`:

```python
"""C10: every REVA_* env var the code reads must be documented in .env.example.

Prevents the config surface from silently drifting again (18 undocumented
tunables were found in the 2026-07-04 review).
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

_SOURCES = [
    _ROOT / "api" / "app" / "settings.py",
    _ROOT / "worker" / "worker" / "settings.py",
    _ROOT / "scheduler" / "scheduler" / "settings.py",
    _ROOT / "reva" / "config.py",
    _ROOT / "reva" / "logging.py",
]

# Deliberately absent from .env.example:
_ALLOWLIST = {
    "REVA_TEST_POSTGRES_URL",   # integration-test only
    "REVA_VERIFY_HIGH_COST",    # deprecated alias, code-honored but not advertised
}


def _vars_read_by_code() -> set[str]:
    pattern = re.compile(r"[\"'](REVA_[A-Z0-9_]+)[\"']")
    found: set[str] = set()
    for src in _SOURCES:
        found.update(pattern.findall(src.read_text()))
    return found - _ALLOWLIST


def test_env_example_documents_every_reva_var():
    example = (_ROOT / ".env.example").read_text()
    missing = sorted(v for v in _vars_read_by_code() if v not in example)
    assert not missing, f".env.example is missing: {missing}"
```

- [ ] **Step 2: Run to verify failure**

Run: `cd worker && .venv/bin/python -m pytest tests/test_env_example.py -q`
Expected: FAIL — lists ~18 missing vars

- [ ] **Step 3: Rewrite `.env.example`**

Replace the sections from `# --- Models (optional, worker) ---` to the end of the file with (keep everything above `# --- Models` unchanged):

```bash
# --- Models (optional, worker) ------------------------------------------------
# Single source for both the direct-API client and the Claude Code CLI runner
# (reva/config.py). Defaults shown; full reviews use DEFAULT, /deep-review and
# audits use DEEP, the finding verifier uses VERIFY.
# REVA_DEFAULT_MODEL=claude-sonnet-5
# REVA_DEEP_MODEL=claude-opus-4-8
# REVA_VERIFY_MODEL=claude-haiku-4-5

# --- CodeGraph engine layer (optional, worker) --------------------------------
# Pre-indexed code knowledge graph exposed to repo-aware reviews/audits via MCP
# (cheaper, more cross-file-aware). Default off; pinned + validated before use.
# REVA_CODEGRAPH_ENABLED=false
# REVA_CODEGRAPH_VERSION=0.9.8          # keep in sync with the worker Dockerfile pin
# REVA_CODEGRAPH_INDEX_TIMEOUT=180      # seconds to bound the index step

# --- Second-pass self-critique (optional, worker) -----------------------------
# Re-verify blocking-threshold findings against the cited code before posting;
# drops confident false positives. Default ON. Per-repo
# `.claude-review.yml verify_findings: true|false` overrides this global.
# REVA_VERIFY_FINDINGS=true

# --- Operational alerts (optional, scheduler) ---------------------------------
# Thresholds for Google Chat alerts; require GOOGLE_CHAT_WEBHOOK_URL set.
# REVA_QUEUE_DEPTH_ALERT=50
# REVA_FAILED_JOBS_ALERT=10
# REVA_REPO_CACHE_DISK_PCT_ALERT=90

# --- Scheduler cadences (optional) ---------------------------------------------
# REVA_POLL_INTERVAL_SECONDS=30                 # pending-review debounce poller
# REVA_EVICTION_INTERVAL_SECONDS=86400          # repo-cache eviction job cadence
# REVA_RETENTION_PURGE_INTERVAL_SECONDS=86400   # retention purge cadence
# REVA_MEMORY_DISTILL_INTERVAL_SECONDS=86400    # learned-memory distill cadence
# REVA_MEMORY_DISTILL_MIN_DISMISSALS=3          # dismissals before a repo distills
# REVA_REPORT_WEEKDAY=0                         # weekly report: 0=Monday .. 6=Sunday
# REVA_REPORT_HOUR_UTC=8                        # weekly report send hour (UTC)

# --- Data retention (optional) --------------------------------------------------
# REVA_TICKET_TEXT_RETENTION_DAYS=30    # scrub raw customer ticket text after N days
# REVA_SPEND_RETENTION_DAYS=400         # delete claude_spend ledger rows after N days

# --- Reliability / hardening (optional) ---------------------------------------
# Reap review_runs stuck in 'running' past this many seconds (worker killed
# mid-review). Default = 2x the review job timeout (4200).
# REVA_STALE_RUNNING_SECONDS=4200
# Per-client request cap on /api/v1 over a rolling minute (0 = off). Per API
# process; complements nginx's rate limit.
# REVA_API_RATE_LIMIT_PER_MINUTE=120
# Database backup target + retention (used by scripts/backup.sh).
# REVA_BACKUP_DIR=./backups
# REVA_BACKUP_RETENTION_DAYS=14

# --- Paths & plumbing (rarely changed; container defaults are correct) --------
# REVA_QUEUE_NAME=reviews
# REVA_REPO_CACHE_DIR=/repos
# REVA_REPO_CACHE_TTL_DAYS=30
# REVA_MIGRATIONS_DIR=/app/db/migrations
# REVA_PROMPTS_DIR=/app/prompts
# REVA_SKILLS_DIR=/app/prompts/skills
# REVA_SCHEDULER_HEARTBEAT_PATH=/tmp/reva-scheduler-heartbeat

# --- Logging (optional) --------------------------------------------------------
# REVA_LOG_LEVEL=INFO
# REVA_LOG_FORMAT=json          # json | console

# --- Misc (optional) ------------------------------------------------------------
# REVA_DEBOUNCE_SECONDS=600             # webhook -> review debounce window
# REVA_DEFAULT_REVIEW_MODE=diff
# REVA_AUTO_AUDIT_REPOS=true            # audit repos on installation/added (prod)
# REVA_REQUIRE_API_KEY=true             # fail closed without REVA_API_KEY (prod)
```

Also fix the stale default in the existing `# REVA_DEFAULT_MODEL=claude-sonnet-4-6` line — it is replaced by the block above. Note: `REVA_SPEND_RETENTION_DAYS` lands in Task 11; documenting it here first is fine (the drift test only checks code→example, not example→code).

- [ ] **Step 4: Wire `REVA_VERIFY_MODEL` through both compose files**

In `docker-compose.yml` worker env, after `REVA_DEEP_MODEL: …` add:

```yaml
      REVA_VERIFY_MODEL: ${REVA_VERIFY_MODEL:-claude-haiku-4-5}
```

Same line in `docker-compose.prod.yml` worker env after its `REVA_DEEP_MODEL` line.

- [ ] **Step 5: Run to verify pass**

Run: `cd worker && .venv/bin/python -m pytest tests/test_env_example.py -q && docker compose -f docker-compose.yml config -q && docker compose -f docker-compose.prod.yml config -q`
Expected: PASS, both compose files parse

- [ ] **Step 6: Commit**

```bash
git add .env.example docker-compose.yml docker-compose.prod.yml worker/tests/test_env_example.py
git commit -m "chore(config): document every REVA_* tunable + drift test; wire REVA_VERIFY_MODEL"
```

---

### Task 11: C11 — claude_spend retention

**Files:**
- Modify: `reva/db/writers.py` (new purge function, next to `purge_old_github_events` ~line 1307)
- Modify: `scheduler/scheduler/settings.py` (new field), `scheduler/scheduler/main.py:61-81,169-174` (retention pass)
- Test: `worker/tests/test_spend_retention.py` (new), scheduler suite must stay green

**Interfaces:**
- Produces: `writers.purge_old_claude_spend(db: Database, older_than_days: int) -> int`; `scheduler Settings.spend_retention_days: int = 400` (env `REVA_SPEND_RETENTION_DAYS`).

- [ ] **Step 1: Write the failing test**

Create `worker/tests/test_spend_retention.py`:

```python
"""C11: claude_spend rows are deleted past the retention window (the budget
gate needs 24h; dashboards keep >1 year with the 400-day default)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from reva.db import Base, Database, create_engine_from_url, writers
from reva.db.models import ClaudeSpend


@pytest.fixture()
def db() -> Database:
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def _spend_row(db: Database, days_old: int) -> None:
    with db.session() as s:
        s.add(ClaudeSpend(
            kind="review", cost_usd=1.0,
            created_at=datetime.now(timezone.utc) - timedelta(days=days_old),
        ))


def test_purges_only_rows_past_window(db):
    _spend_row(db, days_old=500)
    _spend_row(db, days_old=10)
    deleted = writers.purge_old_claude_spend(db, older_than_days=400)
    assert deleted == 1
    with db.session() as s:
        assert s.query(ClaudeSpend).count() == 1


def test_idempotent(db):
    _spend_row(db, days_old=500)
    assert writers.purge_old_claude_spend(db, 400) == 1
    assert writers.purge_old_claude_spend(db, 400) == 0
```

- [ ] **Step 2: Run to verify failure**

Run: `cd worker && .venv/bin/python -m pytest tests/test_spend_retention.py -q`
Expected: FAIL — `AttributeError: module 'reva.db.writers' has no attribute 'purge_old_claude_spend'`

- [ ] **Step 3: Add the writer**

In `reva/db/writers.py`, after `purge_old_github_events`:

```python
def purge_old_claude_spend(db: Database, older_than_days: int) -> int:
    """Delete claude_spend ledger rows older than `older_than_days` (C11).

    The rolling budget cap reads only the trailing 24h; cost dashboards read
    weeks. Past the window the rows are pure unbounded growth. Returns the
    number of rows deleted."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    with db.session() as s:
        result = s.execute(delete(ClaudeSpend).where(ClaudeSpend.created_at < cutoff))
        return result.rowcount
```

(`delete` and `ClaudeSpend` are already imported in writers.py — verify with `grep -n "^from reva.db.models import\|from sqlalchemy import" reva/db/writers.py` and extend those imports if `ClaudeSpend` is missing.)

- [ ] **Step 4: Wire scheduler settings + retention pass**

`scheduler/scheduler/settings.py` — add after `retention_purge_interval_seconds`:

```python
    # C11: delete claude_spend ledger rows older than this (budget cap needs
    # 24h; dashboards keep >1 year).
    spend_retention_days: int = 400
```

and in `from_env`, after the `retention_purge_interval_seconds=` entry:

```python
            spend_retention_days=int(os.environ.get("REVA_SPEND_RETENTION_DAYS", "400")),
```

`scheduler/scheduler/main.py` — extend `maybe_purge_ticket_text` with a defaulted parameter (existing callers/tests keep working) and one more purge:

```python
def maybe_purge_ticket_text(db, now, last_purge, interval_s, retention_days,
                            spend_retention_days: int = 400):
```

and before `return now` inside it:

```python
    # C11: the spend ledger is append-only and unbounded — same daily cadence.
    purged_spend = writers.purge_old_claude_spend(db, spend_retention_days)
    if purged_spend:
        logger.info("claude_spend_purged", rows=purged_spend,
                    retention_days=spend_retention_days)
```

and at the call site in `main()`:

```python
            last_purge = maybe_purge_ticket_text(
                db, now, last_purge, settings.retention_purge_interval_seconds,
                settings.ticket_text_retention_days, settings.spend_retention_days,
            )
```

- [ ] **Step 5: Run to verify pass**

Run: `cd worker && .venv/bin/python -m pytest tests/test_spend_retention.py -q && cd ../scheduler && .venv/bin/python -m pytest tests/ -q`
Expected: PASS (both)

- [ ] **Step 6: Commit**

```bash
git add reva/db/writers.py scheduler/scheduler/settings.py scheduler/scheduler/main.py worker/tests/test_spend_retention.py
git commit -m "feat(retention): purge claude_spend past REVA_SPEND_RETENTION_DAYS (400d)"
```

---

### Task 12: C12 — stray files + CF-Access operator note

**Files:**
- Delete: `uv.lock`
- Inspect (then delete OR surface): `reva-prod-fixes.patch`, `reva-tui-cf-access.patch`
- Modify: `docs/setup-production.md` (operator checklist)

**Interfaces:** none.

- [ ] **Step 1: Delete the stale uv.lock**

Run: `rm /home/joseph/Projects/Cloudunify/cu_reva/uv.lock`
(uv migration was explicitly deferred as its own project — decision in the spec.)

- [ ] **Step 2: Check each patch against main**

Run for each of `reva-prod-fixes.patch` and `reva-tui-cf-access.patch`:

```bash
git apply --check reva-prod-fixes.patch; echo "exit: $?"
git apply --check reva-tui-cf-access.patch; echo "exit: $?"
```

Then read each patch. Decision rule:
- If `git apply --check` fails because the changes are **already present on
  main** (context lines match current code) → the patch is superseded: delete it.
- If a patch applies cleanly or contains changes **not** on main:
  **STOP. Do not apply it. Report the patch content summary to Joseph and
  wait for his decision.** (Spec: "never auto-apply", especially the
  CF-Access TUI patch.)

- [ ] **Step 3: Add the CF-Access step to the operator docs**

In `docs/setup-production.md`, find the operator/setup checklist for the
Cloudflare tunnel (grep for "cloudflared" or "Cloudflare") and add a numbered
step:

```markdown
1. **Gate the docs surface with Cloudflare Access.** Create a Cloudflare
   Access application for `https://$REVA_DOMAIN` covering the paths `/docs`
   and `/repo-docs` (the consultant docs SPA + its data API — the only
   human-facing browser surface). Leave `/webhooks` (GitHub cannot SSO),
   `/api`, and `/health` ungated. Until this application exists, the docs
   site is reachable by anyone who can reach the tunnel hostname.
```

- [ ] **Step 4: Verify and commit**

Run: `git status --short` — expect deletions of resolved files only, plus the docs change; no `.patch` file may be silently applied.

```bash
git add -A docs/setup-production.md
git rm --cached uv.lock 2>/dev/null; true   # only if it was ever staged
git commit -m "chore: remove stale uv.lock/superseded patches; document CF-Access gating step"
```

(If a patch was surfaced instead of deleted, commit only what was resolved and flag the open patch in the task report.)

---

### Task 13: D1 — quota columns + spend-sum writer

**Files:**
- Create: `db/migrations/026_odoo_instance_quotas.sql` (verify 026 is free)
- Modify: `reva/db/models.py:549-571` (`OdooInstance`), `reva/db/writers.py` (`update_odoo_instance` allowed set ~line 1916, `get_odoo_instance` dict ~line 1887, new `sum_instance_cost_since`)
- Test: `worker/tests/test_instance_quota_writers.py` (new)

**Interfaces:**
- Produces: `OdooInstance.daily_budget_usd: float | None`, `OdooInstance.rate_limit_per_minute: int | None`;
  `writers.sum_instance_cost_since(db: Database, odoo_instance_id: int, since: datetime) -> float`;
  `writers.update_odoo_instance` accepts the two new field names;
  `writers.get_odoo_instance` dict includes both new keys.

- [ ] **Step 1: Write the failing tests**

Create `worker/tests/test_instance_quota_writers.py`:

```python
"""D1: per-instance quota columns + the 24h spend sum they gate on."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from reva.db import Base, Database, create_engine_from_url, writers
from reva.db.models import TicketAnalysis, TicketIssueRun


@pytest.fixture()
def db() -> Database:
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def _instance(db: Database) -> int:
    return writers.create_odoo_instance(
        db, name="acme", key_hash="h", key_prefix="reva_odoo_x",
        callback_url="", callback_api_key_enc="",
    )


def _analysis_row(db, instance_id, cost, days_old=0):
    with db.session() as s:
        s.add(TicketAnalysis(
            odoo_instance_id=instance_id, ticket_id=1, model_name="m",
            field_name="f", input_text="t", status="completed",
            estimated_cost_usd=cost,
            created_at=datetime.now(timezone.utc) - timedelta(days=days_old),
        ))


def _issue_row(db, instance_id, cost):
    with db.session() as s:
        s.add(TicketIssueRun(
            odoo_instance_id=instance_id, ticket_id=1, model_name="m",
            github_url="https://github.com/a/b", name="n", description="d",
            analysis_html="<p/>", priority="normal", ticket_url="u",
            status="created", estimated_cost_usd=cost,
        ))


def test_quota_fields_default_null_and_update(db):
    iid = _instance(db)
    row = writers.get_odoo_instance(db, iid)
    assert row["daily_budget_usd"] is None
    assert row["rate_limit_per_minute"] is None

    assert writers.update_odoo_instance(
        db, iid, daily_budget_usd=10.5, rate_limit_per_minute=30
    )
    row = writers.get_odoo_instance(db, iid)
    assert row["daily_budget_usd"] == pytest.approx(10.5)
    assert row["rate_limit_per_minute"] == 30

    # Explicit clear back to unlimited.
    assert writers.update_odoo_instance(db, iid, daily_budget_usd=None)
    assert writers.get_odoo_instance(db, iid)["daily_budget_usd"] is None


def test_sum_spans_both_run_tables_and_window(db):
    iid = _instance(db)
    other = writers.create_odoo_instance(
        db, name="other", key_hash="h2", key_prefix="reva_odoo_y",
        callback_url="", callback_api_key_enc="",
    )
    _analysis_row(db, iid, cost=1.25)
    _issue_row(db, iid, cost=0.75)
    _analysis_row(db, iid, cost=99.0, days_old=2)   # outside 24h window
    _analysis_row(db, other, cost=5.0)              # different instance

    since = datetime.now(timezone.utc) - timedelta(days=1)
    assert writers.sum_instance_cost_since(db, iid, since) == pytest.approx(2.0)
    assert writers.sum_instance_cost_since(db, other, since) == pytest.approx(5.0)


def test_sum_empty_is_zero(db):
    iid = _instance(db)
    since = datetime.now(timezone.utc) - timedelta(days=1)
    assert writers.sum_instance_cost_since(db, iid, since) == 0.0
```

- [ ] **Step 2: Run to verify failure**

Run: `cd worker && .venv/bin/python -m pytest tests/test_instance_quota_writers.py -q`
Expected: FAIL — `KeyError: 'daily_budget_usd'`

- [ ] **Step 3: Create the migration**

`db/migrations/026_odoo_instance_quotas.sql`:

```sql
-- Per-instance quotas (D13, config-perf-hardening spec): NULL = unlimited,
-- so existing instances behave exactly as before until an operator sets a
-- cap. daily_budget_usd gates the rolling-24h spend summed over the
-- instance-scoped run tables; rate_limit_per_minute caps create-route
-- requests. Mirrors reva/db/models.py::OdooInstance.
ALTER TABLE odoo_instances ADD COLUMN IF NOT EXISTS daily_budget_usd NUMERIC(12, 2);
ALTER TABLE odoo_instances ADD COLUMN IF NOT EXISTS rate_limit_per_minute INTEGER;
```

- [ ] **Step 4: Extend the ORM model**

In `reva/db/models.py::OdooInstance`, after `active`:

```python
    # Per-instance quotas (migration 026): NULL = unlimited.
    daily_budget_usd: Mapped[float | None] = mapped_column(Numeric(12, 2))
    rate_limit_per_minute: Mapped[int | None] = mapped_column(Integer)
```

- [ ] **Step 5: Extend the writers**

In `reva/db/writers.py::update_odoo_instance`, change the allowed set:

```python
    allowed = {"name", "callback_url", "callback_api_key_enc", "active",
               "daily_budget_usd", "rate_limit_per_minute"}
```

In `get_odoo_instance`'s returned dict, after `"active": row.active,` add:

```python
            "daily_budget_usd": (
                float(row.daily_budget_usd) if row.daily_budget_usd is not None else None
            ),
            "rate_limit_per_minute": row.rate_limit_per_minute,
```

New function next to `get_odoo_instance` (imports `TicketAnalysis`, `TicketIssueRun`, `func`, `select` all already present in writers.py):

```python
def sum_instance_cost_since(db: Database, odoo_instance_id: int, since: datetime) -> float:
    """Rolling spend (USD) for one Odoo instance across its run tables (D13).

    Extension point: when the timesheet-review and website-analysis tables
    land (both carry odoo_instance_id + estimated_cost_usd), add them to this
    sum — it is the single source the per-instance budget gates read."""
    total = 0.0
    with db.session() as s:
        for model in (TicketAnalysis, TicketIssueRun):
            value = s.execute(
                select(func.coalesce(func.sum(model.estimated_cost_usd), 0)).where(
                    model.odoo_instance_id == odoo_instance_id,
                    model.created_at >= since,
                )
            ).scalar_one()
            total += float(value)
    return total
```

- [ ] **Step 6: Run to verify pass**

Run: `cd worker && .venv/bin/python -m pytest tests/test_instance_quota_writers.py tests/test_odoo_instance_writers.py tests/test_odoo_instance_model.py -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add db/migrations/026_odoo_instance_quotas.sql reva/db/models.py reva/db/writers.py worker/tests/test_instance_quota_writers.py
git commit -m "feat(db): per-instance quota columns + 24h instance spend sum"
```

---

### Task 14: D2 — API quota enforcement (429 + per-instance rate limit + PATCH)

**Files:**
- Modify: `api/app/ratelimit.py`, `api/app/dependencies.py:58-81`, `api/app/queries/odoo_instances.py`, `api/app/schemas/odoo_instances.py`, `api/app/routes/v1/odoo_instances.py:103-125`, `api/app/routes/v1/ticket_analyses.py` (submit), `api/app/routes/v1/ticket_issues.py` (submit)
- Test: `api/tests/test_instance_quotas.py` (new)

**Interfaces:**
- Consumes: Task 13's `writers.sum_instance_cost_since`, ORM columns, `update_odoo_instance` fields.
- Produces:
  - `app.ratelimit.enforce_instance_rate_limit(instance_id: int, limit: int | None) -> None` (raises 429)
  - `app.dependencies.ResolvedOdooInstance` gains `daily_budget_usd: float | None = None`, `rate_limit_per_minute: int | None = None`
  - `app.dependencies.assert_instance_within_budget(db: Database, instance: ResolvedOdooInstance) -> None` (raises 429)
  - `app.queries.odoo_instances.instance_limits(db, instance_id) -> tuple[float | None, int | None]`
  - `OdooInstanceUpdate` + `OdooInstanceSummary` carry the two new fields; PATCH persists them (explicit `null` clears).

- [ ] **Step 1: Write the failing tests**

Create `api/tests/test_instance_quotas.py`:

```python
"""D2: per-instance budget (429 at submit) + per-instance rate limit + PATCH."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from app.dependencies import get_db, get_settings
from app.main import app
from app.settings import Settings
from reva.db import Base, Database, create_engine_from_url, writers

BASE_PAYLOAD = {
    "ticket_id": 42,
    "model_name": "helpdesk.ticket",
    "field_name": "x_reva_analysis",
    "text": "The login page is broken.",
}


@dataclass
class FakeJob:
    id: str = "rq:job:fake-1"


@dataclass
class FakeQueue:
    enqueued: list[tuple] = field(default_factory=list)

    def enqueue(self, func_path, params, **kwargs):
        self.enqueued.append((func_path, params, kwargs))
        return FakeJob(id=f"rq:job:fake-{len(self.enqueued)}")


@pytest.fixture()
def client_db_queue(monkeypatch):
    from cryptography.fernet import Fernet

    from app import ratelimit
    ratelimit.reset()
    monkeypatch.setenv("REVA_SECRET_KEY", Fernet.generate_key().decode())
    engine = create_engine_from_url(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = Database(engine)
    settings = Settings(
        database_url="sqlite:///:memory:", github_app_id=1,
        github_webhook_secret="x", github_private_key="x",
        redis_url="redis://localhost:6379/0",
    )
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: settings
    queue = FakeQueue()
    prev_queue = getattr(app.state, "rq_queue", None)
    app.state.rq_queue = queue
    tc = TestClient(app)
    created = tc.post("/api/v1/odoo-instances", json={
        "name": "test", "callback_url": "", "callback_api_key": "",
    }).json()
    yield tc, db, queue, created["id"], {"Authorization": f"Bearer {created['api_key']}"}
    app.state.rq_queue = prev_queue
    app.dependency_overrides.clear()
    ratelimit.reset()


def _burn_budget(db: Database, instance_id: int, cost: float) -> None:
    from reva.db.models import TicketAnalysis
    with db.session() as s:
        s.add(TicketAnalysis(
            odoo_instance_id=instance_id, ticket_id=9, model_name="m",
            field_name="f", input_text="t", status="completed",
            estimated_cost_usd=cost,
        ))


def test_patch_sets_and_clears_quota(client_db_queue):
    client, _, _, iid, _ = client_db_queue
    r = client.patch(f"/api/v1/odoo-instances/{iid}",
                     json={"daily_budget_usd": 10, "rate_limit_per_minute": 30})
    assert r.status_code == 200
    inst = next(i for i in client.get("/api/v1/odoo-instances").json()["items"]
                if i["id"] == iid)
    assert inst["daily_budget_usd"] == 10
    assert inst["rate_limit_per_minute"] == 30

    # Explicit null clears back to unlimited.
    assert client.patch(f"/api/v1/odoo-instances/{iid}",
                        json={"daily_budget_usd": None}).status_code == 200
    inst = next(i for i in client.get("/api/v1/odoo-instances").json()["items"]
                if i["id"] == iid)
    assert inst["daily_budget_usd"] is None


def test_patch_rejects_negative_budget(client_db_queue):
    client, _, _, iid, _ = client_db_queue
    assert client.patch(f"/api/v1/odoo-instances/{iid}",
                        json={"daily_budget_usd": -1}).status_code == 422


def test_submit_429_when_over_budget(client_db_queue):
    client, db, queue, iid, headers = client_db_queue
    client.patch(f"/api/v1/odoo-instances/{iid}", json={"daily_budget_usd": 5})
    _burn_budget(db, iid, cost=6.0)
    r = client.post("/api/v1/ticket-analysis", json=BASE_PAYLOAD, headers=headers)
    assert r.status_code == 429
    assert "budget" in r.json()["detail"].lower()
    assert queue.enqueued == []


def test_submit_ok_under_budget(client_db_queue):
    client, db, queue, iid, headers = client_db_queue
    client.patch(f"/api/v1/odoo-instances/{iid}", json={"daily_budget_usd": 5})
    _burn_budget(db, iid, cost=1.0)
    r = client.post("/api/v1/ticket-analysis", json=BASE_PAYLOAD, headers=headers)
    assert r.status_code == 202
    assert len(queue.enqueued) == 1


def test_no_budget_means_unlimited(client_db_queue):
    client, db, queue, iid, headers = client_db_queue
    _burn_budget(db, iid, cost=1000.0)
    r = client.post("/api/v1/ticket-analysis", json=BASE_PAYLOAD, headers=headers)
    assert r.status_code == 202


def test_instance_rate_limit_429(client_db_queue):
    client, _, _, iid, headers = client_db_queue
    client.patch(f"/api/v1/odoo-instances/{iid}", json={"rate_limit_per_minute": 2})
    p1 = {**BASE_PAYLOAD, "ticket_id": 1}
    p2 = {**BASE_PAYLOAD, "ticket_id": 2}
    p3 = {**BASE_PAYLOAD, "ticket_id": 3}
    assert client.post("/api/v1/ticket-analysis", json=p1, headers=headers).status_code == 202
    assert client.post("/api/v1/ticket-analysis", json=p2, headers=headers).status_code == 202
    r = client.post("/api/v1/ticket-analysis", json=p3, headers=headers)
    assert r.status_code == 429
```

- [ ] **Step 2: Run to verify failure**

Run: `cd api && .venv/bin/python -m pytest tests/test_instance_quotas.py -q`
Expected: FAIL — PATCH ignores the fields (`no fields to update` 422) and submits return 202.

- [ ] **Step 3: ratelimit — shared window check + instance branch**

In `api/app/ratelimit.py`, add below `_sweep`:

```python
def _check_window(key: str, limit: int, detail: str) -> None:
    """Rolling-window check shared by the global and per-instance limiters."""
    now = time.monotonic()
    global _last_sweep
    with _lock:
        if now - _last_sweep > _SWEEP_INTERVAL:
            _sweep(now)
            _last_sweep = now
        window = _hits[key]
        cutoff = now - _WINDOW_SECONDS
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= limit:
            raise HTTPException(status_code=429, detail=detail)
        window.append(now)


def enforce_instance_rate_limit(instance_id: int, limit: int | None) -> None:
    """Per-Odoo-instance request cap (D13). None/0 = unlimited. Same
    in-memory best-effort-per-process semantics as the global limiter."""
    if not limit:
        return
    _check_window(f"instance:{instance_id}", limit, "Instance rate limit exceeded")
```

and rewrite the body of `rate_limit` to reuse it:

```python
def rate_limit(request: Request, settings: Settings = Depends(get_settings)) -> None:
    limit = settings.rate_limit_per_minute
    if not limit:
        return
    _check_window(_client_key(request), limit, "Rate limit exceeded")
```

- [ ] **Step 4: queries + dependencies**

`api/app/queries/odoo_instances.py` — add:

```python
def instance_limits(db: Database, instance_id: int) -> tuple[float | None, int | None]:
    """(daily_budget_usd, rate_limit_per_minute) for one instance (D13)."""
    with db.session() as s:
        row = s.execute(
            select(OdooInstance.daily_budget_usd, OdooInstance.rate_limit_per_minute)
            .where(OdooInstance.id == instance_id)
        ).first()
        if row is None:
            return None, None
        budget = float(row[0]) if row[0] is not None else None
        return budget, row[1]
```

and add the two fields to `list_odoo_instances`' dict (after `"active": r.active,`):

```python
                "daily_budget_usd": (
                    float(r.daily_budget_usd) if r.daily_budget_usd is not None else None
                ),
                "rate_limit_per_minute": r.rate_limit_per_minute,
```

`api/app/dependencies.py` — extend the dataclass and dependency:

```python
@dataclass(frozen=True)
class ResolvedOdooInstance:
    id: int
    name: str
    # Per-instance quotas (D13); None = unlimited.
    daily_budget_usd: float | None = None
    rate_limit_per_minute: int | None = None
```

In `require_odoo_instance`, replace the final `return`:

```python
    budget, rpm = q.instance_limits(db, resolved[0])
    # Per-instance rate limit fires here — before any row/job is created.
    from app.ratelimit import enforce_instance_rate_limit  # local: avoid a cycle
    enforce_instance_rate_limit(resolved[0], rpm)
    return ResolvedOdooInstance(
        id=resolved[0], name=resolved[1],
        daily_budget_usd=budget, rate_limit_per_minute=rpm,
    )
```

and add the budget helper at the bottom of `dependencies.py`:

```python
def assert_instance_within_budget(db: Database, instance: ResolvedOdooInstance) -> None:
    """429 when the instance's rolling-24h spend has reached its cap (D13).

    Submit-time gate — the worker re-checks before the paid call, so queued
    backlog can't blow past the cap either. Call this first in every
    instance-gated create route (tickets, issues, and future timesheet /
    website-analysis routes)."""
    if instance.daily_budget_usd is None:
        return
    from datetime import datetime, timedelta, timezone

    from reva.db import writers

    spent = writers.sum_instance_cost_since(
        db, instance.id, datetime.now(timezone.utc) - timedelta(days=1)
    )
    if spent >= instance.daily_budget_usd:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Odoo instance daily budget reached "
                f"(≈${spent:.2f} of ${instance.daily_budget_usd:.2f} in 24h); "
                f"try again after spend rolls off or raise the cap."
            ),
        )
```

- [ ] **Step 5: wire the create routes**

`api/app/routes/v1/ticket_analyses.py::submit_ticket_analysis` — first
statement of the function body (before the attachment check), plus extend the
`from app.dependencies import …` line with `assert_instance_within_budget`:

```python
    assert_instance_within_budget(db, instance)
```

Same first-statement call in `api/app/routes/v1/ticket_issues.py`'s
instance-gated create handler (locate: `grep -n "create_router.post" api/app/routes/v1/ticket_issues.py`).

- [ ] **Step 6: schemas + PATCH**

`api/app/schemas/odoo_instances.py`:

```python
from pydantic import BaseModel, Field
```

`OdooInstanceUpdate` — add:

```python
    # None = not provided; explicit null clears the cap (checked via
    # model_fields_set in the route).
    daily_budget_usd: float | None = Field(default=None, ge=0)
    rate_limit_per_minute: int | None = Field(default=None, ge=1)
```

`OdooInstanceSummary` — add:

```python
    daily_budget_usd: float | None = None
    rate_limit_per_minute: int | None = None
```

`api/app/routes/v1/odoo_instances.py::update_instance` — after the `active` field handling add:

```python
    # Quotas: distinguish "not sent" from explicit null (= clear to unlimited).
    if "daily_budget_usd" in body.model_fields_set:
        fields["daily_budget_usd"] = body.daily_budget_usd
    if "rate_limit_per_minute" in body.model_fields_set:
        fields["rate_limit_per_minute"] = body.rate_limit_per_minute
```

- [ ] **Step 7: Run to verify pass**

Run: `cd api && .venv/bin/python -m pytest tests/test_instance_quotas.py tests/test_v1_odoo_instances.py tests/test_odoo_instance_auth.py tests/test_v1_ticket_analyses.py tests/test_v1_ticket_issues.py tests/test_ratelimit.py -q`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add api/app/ratelimit.py api/app/dependencies.py api/app/queries/odoo_instances.py api/app/schemas/odoo_instances.py api/app/routes/v1/odoo_instances.py api/app/routes/v1/ticket_analyses.py api/app/routes/v1/ticket_issues.py api/tests/test_instance_quotas.py
git commit -m "feat(api): per-instance budget 429 + rate limit + quota PATCH"
```

---

### Task 15: D3 — worker-side instance budget gate

**Files:**
- Modify: `worker/worker/runner.py` (new `instance_budget_exceeded` next to `budget_exceeded` ~line 303), `worker/worker/ticket_runner.py:47-61`, `worker/worker/ticket_issue_runner.py:402-403`
- Test: `worker/tests/test_ticket_runner.py` (append), `worker/tests/test_ticket_issue_runner.py` (append)

**Interfaces:**
- Consumes: `writers.sum_instance_cost_since`, `writers.get_odoo_instance` (Task 13); existing `_send_failed_callback` (issue runner), `writers.record_ticket_analysis_failed`, `writers.record_ticket_issue_run_failed`.
- Produces: `worker.runner.instance_budget_exceeded(ctx: WorkerContext, odoo_instance_id: int) -> float | None`.

- [ ] **Step 1: Write the failing tests**

Append to `worker/tests/test_ticket_runner.py`:

```python
def test_instance_budget_gate_declines_before_paid_call(ctx_and_fakes, monkeypatch):
    """D13: an over-budget instance's queued job fails fast — no paid call."""
    s = ctx_and_fakes
    monkeypatch.setattr(
        "worker.ticket_runner.instance_budget_exceeded", lambda ctx, iid: 12.5
    )
    params = _make_params(s["db"])

    with pytest.raises(PermanentError):
        run_ticket_analysis(params)

    row = writers.get_ticket_analysis(s["db"], params["analysis_id"])
    assert row["status"] == "failed"
    assert "budget" in row["error_message"].lower()
    assert s["analyzer"].call_count == 0
```

Append to `worker/tests/test_ticket_issue_runner.py` (its `ctx_and_fakes`
fixture provides `{"ctx", "db", "planner", "github", "odoo"}`; `_make_params`
creates the run row; `FakeGitHub.find_issues_with_marker` returns `[]` by
default, so execution reaches the planner — where the gate sits):

```python
def test_instance_budget_gate_declines_planning(ctx_and_fakes, monkeypatch):
    """D13: over-budget instance → run failed + failed callback, no paid plan
    call. The reconcile path (issues already on GitHub) is deliberately not
    gated — it makes no paid call."""
    s = ctx_and_fakes
    monkeypatch.setattr(
        "worker.ticket_issue_runner.instance_budget_exceeded", lambda ctx, iid: 12.5
    )
    params = _make_params(s["db"])

    with pytest.raises(PermanentError):
        run_ticket_issues(params)

    row = writers.get_ticket_issue_run(s["db"], params["run_id"])
    assert row["status"] == "failed"
    assert "budget" in row["error_message"].lower()
    assert s["planner"].call_count == 0            # no paid call
    assert s["odoo"].calls, "failed callback must have been sent"
    assert s["odoo"].calls[0]["status"] == "failed"
```

- [ ] **Step 2: Run to verify failure**

Run: `cd worker && .venv/bin/python -m pytest tests/test_ticket_runner.py::test_instance_budget_gate_declines_before_paid_call -q`
Expected: FAIL — `AttributeError` (no `instance_budget_exceeded` import in ticket_runner)

- [ ] **Step 3: Add the gate function**

In `worker/worker/runner.py`, directly below `budget_exceeded`:

```python
def instance_budget_exceeded(ctx: WorkerContext, odoo_instance_id: int) -> float | None:
    """Rolling 24h spend (USD) for one Odoo instance if its cap is reached,
    else None (D13). NULL cap = unlimited. Mirrors budget_exceeded; the API
    also gates at submit time, so this only catches already-queued backlog."""
    inst = writers.get_odoo_instance(ctx.db, odoo_instance_id)
    if inst is None or inst.get("daily_budget_usd") is None:
        return None
    spent = writers.sum_instance_cost_since(
        ctx.db, odoo_instance_id, datetime.now(timezone.utc) - timedelta(days=1)
    )
    return spent if spent >= float(inst["daily_budget_usd"]) else None
```

- [ ] **Step 4: Gate the ticket runner**

In `worker/worker/ticket_runner.py`: extend the import to
`from worker.runner import build_odoo_client, get_context, instance_budget_exceeded`,
then inside `run_ticket_analysis`, in the `else:` branch (fresh analysis path,
directly before the `try:` around `analyze_with_response`):

```python
        # D13: per-instance budget — fail fast before the paid call. Odoo's
        # poll/requeue surface shows the recorded error.
        spent = instance_budget_exceeded(ctx, params.odoo_instance_id)
        if spent is not None:
            error = (
                f"Odoo instance daily budget reached (≈${spent:.2f} in 24h); "
                f"analysis declined."
            )
            log.warning("ticket_analysis_instance_over_budget", spent_usd=round(spent, 2))
            writers.record_ticket_analysis_failed(ctx.db, params.analysis_id, error)
            raise PermanentError(error)
```

- [ ] **Step 5: Gate the issue runner**

In `worker/worker/ticket_issue_runner.py`: extend its `from worker.runner
import …` line with `instance_budget_exceeded`, then directly before
`response, plan = ctx.ticket_issue_planner.plan_with_response(params)`:

```python
            # D13: per-instance budget — fail fast before the paid plan call.
            spent = instance_budget_exceeded(ctx, params.odoo_instance_id)
            if spent is not None:
                error = (
                    f"Odoo instance daily budget reached (≈${spent:.2f} in 24h); "
                    f"issue planning declined."
                )
                log.warning("ticket_issues_instance_over_budget",
                            spent_usd=round(spent, 2))
                writers.record_ticket_issue_run_failed(ctx.db, params.run_id, error)
                _send_failed_callback(ctx, params, error, log)
                raise PermanentError(error)
```

(The reconcile path — issues already found on GitHub — is deliberately NOT
gated: it makes no paid call.)

- [ ] **Step 6: Run to verify pass**

Run: `cd worker && .venv/bin/python -m pytest tests/test_ticket_runner.py tests/test_ticket_issue_runner.py tests/test_runner.py -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add worker/worker/runner.py worker/worker/ticket_runner.py worker/worker/ticket_issue_runner.py worker/tests/test_ticket_runner.py worker/tests/test_ticket_issue_runner.py
git commit -m "feat(worker): per-instance budget gate before paid ticket/issue calls"
```

---

### Task 16: D4 — TUI budget columns

**Files:**
- Modify: `tui/internal/api/types.go:267-275` (`OdooInstanceSummary`), `tui/internal/api/mock.go` (the `OdooInstances` mock), `tui/internal/ui/odoo.go:214-253` (view columns)
- Test: `tui/internal/ui/odoo_test.go` (append)

**Interfaces:**
- Consumes: the API's new `daily_budget_usd` / `rate_limit_per_minute` summary fields (Task 14).

- [ ] **Step 1: Write the failing test**

Append to `tui/internal/ui/odoo_test.go` (match the file's existing
construction pattern for the `Odoo` model — locate with `grep -n "func Test" tui/internal/ui/odoo_test.go`; the essential assertions):

```go
func TestOdooViewShowsBudgetColumn(t *testing.T) {
	o := newOdoo(&api.MockClient{})
	b := 10.0
	page := &api.OdooInstancePage{
		Items: []api.OdooInstanceSummary{
			{ID: 1, Name: "capped", KeyPrefix: "reva_odoo_x", Active: true,
				DailyBudgetUSD: &b,
				Cost: api.OdooInstanceCost{Last24h: api.WindowCost{
					Analysis: api.TaskCost{CostUSD: 3.2}}}},
			{ID: 2, Name: "unlimited", KeyPrefix: "reva_odoo_y", Active: true},
		},
		Total: 2,
	}
	o, _ = o.update(odooLoadedMsg{data: page})
	o.width, o.height = 140, 30
	out := o.view(140, 30)
	if !strings.Contains(out, "3.20/10") {
		t.Fatalf("expected budget cell '3.20/10' in view:\n%s", out)
	}
	if !strings.Contains(out, "Budget") {
		t.Fatalf("expected Budget column header:\n%s", out)
	}
}
```

(If the odoo loaded message type has a different name, mirror the one
`odoo.go`'s `update` handles — `grep -n "case odoo" tui/internal/ui/odoo.go`.)

- [ ] **Step 2: Run to verify failure**

Run: `cd tui && go test ./internal/ui/ -run TestOdooViewShowsBudgetColumn`
Expected: FAIL — unknown field `DailyBudgetUSD`

- [ ] **Step 3: Extend the Go types + mock**

`tui/internal/api/types.go` — in `OdooInstanceSummary`, after `CreatedAt`:

```go
	// Per-instance quotas (nil = unlimited).
	DailyBudgetUSD     *float64 `json:"daily_budget_usd"`
	RateLimitPerMinute *int     `json:"rate_limit_per_minute"`
```

`tui/internal/api/mock.go` — in the `OdooInstances()` mock, give one demo
instance a budget so demo mode exercises the column, e.g. add to an existing
item literal:

```go
	// demo: a capped instance so the Budget column renders
	// (add inside one existing OdooInstanceSummary literal)
	DailyBudgetUSD: f64Ptr(10),
```

(reuse the file's existing `f64Ptr` helper; if that mock function lacks one,
declare `f64Ptr := func(f float64) *float64 { return &f }` at its top.)

- [ ] **Step 4: Render the column**

In `tui/internal/ui/odoo.go::view`, replace the column layout + row rendering
(lines 214–252) with:

```go
	colName, colPrefix, colHost, colA, colI, colW, colB := 24, 16, 26, 10, 10, 9, 12
	hdr := lipgloss.NewStyle().Bold(true).Foreground(colorMuted).Render(
		fmt.Sprintf("   %-*s  %-*s  %-*s  %*s  %*s  %*s  %*s  %*s",
			colName, "Name", colPrefix, "Key", colHost, "Callback",
			colA, "Life A$", colI, "Life I$", colW, "24h$", colW, "30d$",
			colB, "Budget"))

	visibleRows := h - 6
	if visibleRows < 1 {
		visibleRows = 1
	}
	end := o.offset + visibleRows
	if end > len(o.items) {
		end = len(o.items)
	}
	rows := []string{hdr}
	for i := o.offset; i < end; i++ {
		it := o.items[i]
		host := it.CallbackURL
		if host == "" {
			host = "—"
		}
		life := it.Cost.Lifetime
		d24 := it.Cost.Last24h.Analysis.CostUSD + it.Cost.Last24h.Issues.CostUSD
		d30 := it.Cost.Last30d.Analysis.CostUSD + it.Cost.Last30d.Issues.CostUSD
		budget := "—"
		overBudget := false
		if it.DailyBudgetUSD != nil {
			budget = fmt.Sprintf("%.2f/%.0f", d24, *it.DailyBudgetUSD)
			overBudget = *it.DailyBudgetUSD > 0 && d24 >= 0.9*(*it.DailyBudgetUSD)
		}
		active := "+"
		if !it.Active {
			active = "x"
		}
		line := fmt.Sprintf("  %s  %-*s  %-*s  %-*s  %*.2f  %*.2f  %*.2f  %*.2f  %*s",
			active,
			colName, truncate(it.Name, colName),
			colPrefix, truncate(it.KeyPrefix, colPrefix),
			colHost, truncate(host, colHost),
			colA, life.Analysis.CostUSD, colI, life.Issues.CostUSD,
			colW, d24, colW, d30, colB, budget)
		if i == o.cursor {
			line = styleSelected.Width(w - 2).Render(line)
		} else if overBudget {
			// ≥90% of the daily cap — flag the whole row.
			line = styleStatusFailed.Render(line)
		}
		rows = append(rows, line)
	}
```

- [ ] **Step 5: Build, vet, test**

Run: `cd tui && go build ./... && go vet ./... && go test ./...`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add tui/internal/api/types.go tui/internal/api/mock.go tui/internal/ui/odoo.go tui/internal/ui/odoo_test.go
git commit -m "feat(tui): per-instance budget column on the Odoo tab"
```

---

### Task 17: Final verification (whole batch DoD)

**Files:** none new.

- [ ] **Step 1: Full test gate**

```bash
make test
ruff check reva worker/worker api/app scheduler/scheduler
cd tui && go build ./... && go vet ./... && go test ./... && cd ..
docker compose -f docker-compose.yml config -q
docker compose -f docker-compose.prod.yml config -q
```
Expected: everything green.

- [ ] **Step 2: Optional Postgres pass**

If Docker is available: `make test-integration` (exercises the new migrations' raw SQL + the partial-unique/index constructs on real Postgres). Otherwise: first staging boot, per repo convention.

- [ ] **Step 3: Report**

State honestly in the final report:
- which items carry **staging live-gates** still owed (B5 partial-clone review, B6 cache measurement, B9 strict-mode acceptance),
- whether any C12 patch was surfaced instead of deleted,
- that the migration numbers used (025/026) must be re-checked if the timesheet or metasoul plans shipped first.
