# REVA Production Validation Handoff - 2026-07-06

## Current State

Production server: `reva_service@157.90.243.66`

Core services were rebuilt/restarted during validation:

- `api`
- `worker`
- `nginx` restarted once after API recreation to refresh upstream DNS

Current expected service state:

```bash
cd ~/cu_reva
docker compose -f docker-compose.prod.yml ps
```

Expected:

- `api` healthy
- `worker-1` healthy
- `worker-2` healthy
- `scheduler` healthy
- `nginx` healthy
- `postgres` healthy
- `redis` healthy

## Deployed Fixes Included In This Commit

These fixes were deployed manually to the server and are included in the local commit created after this handoff:

- Worker healthcheck fix: RQ worker now registers with container hostname.
- `core_sync.sh` now loads `.env` and supports no-fetch sync from local mirrors.
- Claude strict tool schema fix:
  - every nested object gets `additionalProperties: false`
  - unsupported `minItems` / `maxItems` are stripped from Claude tool schemas
- Issue creation quality fix:
  - placeholder issue title/body is replaced with ticket-derived content
  - single-issue plans no longer create a duplicate parent issue
  - issue body is capped around 900 chars
  - prompt tells Claude to keep issue bodies concise

## Known Existing Test Data

Ticket `220` has previous test artifacts:

- failed analysis `#16`: old `additionalProperties` Claude schema failure
- completed analysis `#17`
- failed issue run `#15`: old `maxItems` Claude schema failure
- completed issue runs `#16` and `#17`
- GitHub issues:
  - `#71` parent/epic from old behavior
  - `#72` child issue

Because REVA dedupes unchanged ticket content, re-clicking Create Issues on the same unchanged ticket will adopt the old plan and will not create a new issue. Use a new ticket or materially change the ticket description to test the latest behavior.

## Tests To Run Tomorrow

### 1. Basic Health

```bash
cd ~/cu_reva
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs --since=30m api worker scheduler nginx \
  | grep -Ei "error|exception|traceback|critical|failed|fatal"
```

Expected: all services healthy, no new unexpected errors.

### 2. TUI Smoke

```bash
cd ~/cu_reva/tui
./run-tui.sh
```

Expected:

- dashboard loads
- ticket analyses load
- ticket issue runs load
- Odoo instances load
- ops events load

Do not run `./reva-tui` directly unless `REVA_API_URL` and `REVA_API_KEY` are exported.

### 3. Ticket Analysis

From Odoo, submit a new ticket analysis.

Expected:

- REVA run becomes `completed`
- no Claude `additionalProperties` error
- Odoo receives `/tickets/write-field`
- analysis text appears in the Odoo field

Check:

```bash
curl -sS -H "Authorization: Bearer $(cat ~/cu_reva/secrets/reva_api_key)" \
  "http://localhost/api/v1/ticket-analyses?limit=5" | python3 -m json.tool
```

### 4. Create Issues - Sparse Ticket

Use a new test ticket or change the existing ticket text enough to avoid dedup.

Example sparse content:

```text
Enable creating an invoice directly for a partner/customer without starting from a sale order.
```

Expected:

- exactly one GitHub issue for a single coherent request
- no separate parent/epic issue
- issue body is concise
- issue body is not `placeholder`
- Odoo receives `/tickets/issues-created`

Check:

```bash
curl -sS -H "Authorization: Bearer $(cat ~/cu_reva/secrets/reva_api_key)" \
  "http://localhost/api/v1/ticket-issue-runs?limit=5" | python3 -m json.tool
```

### 5. Create Issues - Multi-Issue Ticket

Use a richer ticket with 2 or more independent work items.

Expected:

- one parent/epic issue is created
- child issues are created and attached to the parent
- Odoo callback contains only child issues, not the parent
- titles are prefixed with ticket ID and type

### 6. Create Issues - Same Ticket Reclick

Click Create Issues again on unchanged ticket content.

Expected:

- no duplicate GitHub issues
- run completes quickly
- log shows plan adoption/reconciliation
- Odoo receives current issue union again

### 7. Callback URL Verification

Odoo instance callback URL should be the base:

```text
https://<odoo-domain>/api/reva
```

REVA appends:

- `/tickets/write-field`
- `/tickets/reset-status`
- `/tickets/issues-created`
- `/tickets/issue-state`
- `/tickets/ready`
- `/tickets/change-note`
- `/hr/timesheet-results`

### 8. Core Knowledge

```bash
ls /srv/reva-core/19.0
ls /srv/reva-core/19.0/catalog | head
docker compose -f ~/cu_reva/docker-compose.prod.yml exec -T worker env \
  | grep -E "REVA_CORE_KNOWLEDGE_ENABLED|REVA_CORE_VERSIONS|REVA_CORE_KNOWLEDGE_DIR"
```

Expected:

- `/srv/reva-core/19.0/{odoo,enterprise,documentation,catalog}` exists
- worker env has `REVA_CORE_KNOWLEDGE_ENABLED=true`
- worker env has `REVA_CORE_VERSIONS=19.0`

Note: latest ticket test logged `ticket_core_knowledge_unavailable`; investigate separately if core knowledge should be used for ticket analysis immediately.

### 9. Timesheet Review

Submit a small timesheet wording review from Odoo.

Expected:

- no Claude schema errors
- callback goes to `/hr/timesheet-results`
- Odoo receives clear rewritten/ok/needs-human results

### 10. Logs After Manual Tests

Run after each test batch:

```bash
cd ~/cu_reva
docker compose -f docker-compose.prod.yml logs --since=15m api worker scheduler nginx \
  | grep -Ei "Claude 400|additionalProperties|maxItems|minItems|error|exception|traceback|critical|failed|fatal"
```

Expected: no fresh schema errors.

## Local Follow-Up

Before further deploys, inspect local changes:

```bash
git status --short
git diff --stat
```

Important files changed by this validation/fix pass:

- `reva/tool_schema.py`
- `reva/ticket_tool.py`
- `reva/ticket_issue_tool.py`
- `reva/timesheet_tool.py`
- `reva/review_memory_tool.py`
- `worker/worker/main.py`
- `worker/worker/ticket_issue_runner.py`
- `worker/tests/test_strict_tools.py`
- `worker/tests/test_ticket_issue_runner.py`
- `prompts/ticket_issues.md`
- `scripts/core_sync.sh`
