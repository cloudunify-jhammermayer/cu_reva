# Follow-Up Check on Re-Push

**Date:** 2026-05-22  
**Status:** Approved  

## Problem

When a developer pushes a fix to an open PR that REVA has already reviewed, the existing system queues a full new review scan. This is noisy (new findings churn), expensive (full Claude call), and doesn't close previously-reported issues. Instead, REVA should do a targeted follow-up: check whether the existing inline findings have been addressed, resolve fixed threads via GitHub's GraphQL API, and reply to threads that remain open.

## Behaviour Summary

| Trigger | Condition | Action |
|---|---|---|
| `synchronize` (push) | PR has no prior completed review with inline findings | Full review (existing behaviour, unchanged) |
| `synchronize` (push) | PR has a prior completed review with ≥1 inline finding | Follow-up check only — no full scan |
| `opened`, `reopened`, `ready_for_review` | Any | Full review (unchanged) |
| `/review`, `/deep-review` comment | Any | Full review (unchanged) |
| Manual requeue from TUI | Any | Full review (unchanged) |

## Architecture

### Approach

Routing at webhook time (Approach A): `_handle_pull_request` in `webhooks.py` reads the DB once to check for a prior run with inline findings. If found, it enqueues `run_follow_up` directly into RQ and skips `upsert_pending_review`. If not found, the existing full-review path is taken unchanged.

### Data Flow

```
Developer pushes to existing PR
        │
        ▼
GitHub sends `synchronize` webhook
        │
        ▼
_handle_pull_request (webhooks.py)
  └─ get_latest_run_with_inline_findings(db, pull_request_id)
        │
        ├── None → upsert_pending_review (existing path, full review)
        │
        └── found → rq_queue.enqueue("worker.runner.run_follow_up", params)
                        │
                        ▼
                run_follow_up (worker/runner.py)
                  1. get_inline_findings_for_run(prior_run_id)
                  2. github.get_pull_request_diff()
                  3. claude.check_findings(findings, diff)
                  4. For each finding:
                     - fixed  → reply "✅ Addressed" + resolve thread (GraphQL)
                     - open   → reply "⚠️ Still open after {sha[:7]}"
                  5. mark_findings_resolved(fixed_ids, head_sha)
```

## Components

### 1. Webhook Handler (`api/app/routes/webhooks.py`)

`_handle_pull_request` gains a DB read on `synchronize`:

```python
if action == "synchronize":
    prior = writers.get_latest_run_with_inline_findings(db, pr_id)
    if prior:
        rq_queue.enqueue("worker.runner.run_follow_up", FollowUpParams(...))
        return  # skip upsert_pending_review
```

The `rq_queue` handle must be passed into `_handle_pull_request` (it is already available on `request.app.state`).

### 2. `FollowUpParams` (`reva/types.py`)

```python
class FollowUpParams(BaseModel):
    repository_id: int
    pull_request_id: int
    head_sha: str
    installation_id: int
    prior_run_id: int
```

### 3. `run_follow_up` task (`worker/worker/runner.py`)

RQ task entry point. Steps:

1. Fetch inline findings: `get_inline_findings_for_run(db, prior_run_id)` — findings where `github_comment_id IS NOT NULL AND resolved_at IS NULL`
2. If empty (all findings unmapped or all already resolved): post a single `create_issue_comment` — *"All findings from the previous review were general (no inline comments). Use `/review` for a fresh scan."* — and return
3. Get token, owner, name, pr_number
4. Fetch new diff via `github.get_pull_request_diff()`
5. Call `claude.check_findings(findings, diff)` → list of `{finding_id, verdict, reasoning}`
6. Fetch thread map: `github.get_pr_review_threads()` → `{comment_db_id: thread_node_id}`
7. Skip any finding whose thread is already `isResolved=True`
8. For each verdict:
   - `fixed`: reply `"✅ Addressed — {reasoning}"`, call `resolve_review_thread(thread_id)` (best-effort)
   - `open`: reply `"⚠️ Still open after {sha[:7]} — {reasoning}"`
   - Claude omitted finding: treat as `open` (conservative)
9. `mark_findings_resolved(db, fixed_ids, head_sha)`

Error handling: `TransientError` bubbles to RQ (max 3 retries). GraphQL resolve failure per thread → log warning, continue. No Google Chat notification for follow-up errors.

### 4. `ClaudeClient.check_findings()` (`reva/claude_client.py`)

New method. Uses tool_use with a new `submit_follow_up_verdicts` schema.

**Tool schema** (added to `reva/review_tool.py`):

```json
{
  "name": "submit_follow_up_verdicts",
  "description": "Report whether each finding has been addressed in the new diff.",
  "input_schema": {
    "type": "object",
    "required": ["verdicts"],
    "properties": {
      "verdicts": {
        "type": "array",
        "items": {
          "type": "object",
          "required": ["finding_id", "verdict", "reasoning"],
          "properties": {
            "finding_id": {"type": "integer"},
            "verdict": {"type": "string", "enum": ["fixed", "open"]},
            "reasoning": {"type": "string", "maxLength": 200}
          }
        }
      }
    }
  }
}
```

**System prompt:** Short, uncached — REVA identity + "evaluate whether each finding below has been addressed in the new diff."

**User prompt:** Finding list (id, severity, title, body, file, line) + filtered diff.

**Model:** `claude-sonnet-4-6` (default). No prompt caching — context is inherently per-push.

**Signature:**
```python
def check_findings(
    self,
    findings: list[dict],
    diff: str,
    model: str | None = None,
) -> list[dict]:  # [{finding_id, verdict, reasoning}]
```

### 5. GraphQL on `GitHubClient` (`reva/github_client.py`)

Three new methods:

**`_graphql(token, query, variables) -> dict`**  
POST to `https://api.github.com/graphql`. Same `Bearer` token as REST. Raises `TransientError` on timeout/transport, `PermanentError` on GraphQL-level errors (`response["errors"]` non-empty).

**`get_pr_review_threads(token, owner, repo, pr_number) -> dict[int, tuple[str, bool]]`**  
Returns `{comment_database_id: (thread_node_id, is_resolved)}`.

Query:
```graphql
query GetThreads($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          comments(first: 1) {
            nodes { databaseId }
          }
        }
      }
    }
  }
}
```

**`resolve_review_thread(token, thread_id: str) -> None`**  
Mutation `markPullRequestReviewThreadAsResolved`. Called best-effort inside `run_follow_up`.

**Auth:** No new scopes required. Installation token with `pull_requests: write` (already granted) covers both the query and the mutation.

### 6. DB Changes

**Migration `006_follow_up_check.sql`:**

```sql
ALTER TABLE review_findings ADD COLUMN resolved_at  TIMESTAMPTZ;
ALTER TABLE review_findings ADD COLUMN resolved_sha TEXT;

CREATE INDEX idx_findings_resolved
    ON review_findings (review_run_id)
    WHERE resolved_at IS NULL;
```

**`ReviewFinding` model update:** Add `resolved_at: Mapped[datetime | None]` and `resolved_sha: Mapped[str | None]`.

**New/updated writer functions (`reva/db/writers.py`):**

| Function | Signature | Purpose |
|---|---|---|
| `get_latest_run_with_inline_findings` | `(db, pull_request_id) -> dict \| None` | Webhook routing: returns `{run_id, head_sha, installation_id}` or None |
| `get_inline_findings_for_run` | `(db, run_id) -> list[dict]` | Follow-up job: findings with `github_comment_id IS NOT NULL AND resolved_at IS NULL` |
| `mark_findings_resolved` | `(db, finding_ids, sha) -> None` | Batch-set `resolved_at = now()`, `resolved_sha = sha` |

## Loop Safety

| Layer | Mechanism |
|---|---|
| REVA reply → no follow-up-of-follow-up | `_handle_review_comment` already guards `sender.type == "Bot"` |
| Follow-up job doesn't push code | No `synchronize` event triggered by replies |
| GraphQL resolve | Does not fire webhook events |

## Idempotency on Retry

- `resolved_at` already set → skip that finding
- Thread already `isResolved` → skip reply and resolve
- Duplicate "still open" reply on retry accepted (rare, harmless)

## Edge Cases

| Situation | Behaviour |
|---|---|
| All prior inline findings already manually resolved | `get_inline_findings_for_run` returns empty → return silently |
| Diff fetch fails | `TransientError` → RQ retries |
| GraphQL resolve fails for one thread | Log warning, continue (reply already posted) |
| Claude returns unknown `finding_id` | Skip silently (log warning) |
| Claude omits a finding | Treat as `open` |
| PR has only unmapped findings | Post one issue comment; return |
| Follow-up runs concurrently with a `/review` | Independent; full review creates a new run and its own findings |

## What Is Not Changing

- Full `run_review` code path — untouched
- `run_comment_reply` — untouched  
- Scheduler / poller — untouched  
- TUI — no new endpoint needed (resolved findings visible via existing findings tab once `resolved_at` is surfaced)

## Files Changed

| File | Change |
|---|---|
| `reva/types.py` | Add `FollowUpParams` |
| `reva/review_tool.py` | Add `submit_follow_up_verdicts` schema |
| `reva/claude_client.py` | Add `check_findings()` |
| `reva/github_client.py` | Add `_graphql()`, `get_pr_review_threads()`, `resolve_review_thread()` |
| `reva/db/models.py` | Add `resolved_at`, `resolved_sha` to `ReviewFinding` |
| `reva/db/writers.py` | Add 3 writer functions |
| `db/migrations/006_follow_up_check.sql` | New migration |
| `api/app/routes/webhooks.py` | Routing logic in `_handle_pull_request` |
| `worker/worker/runner.py` | Add `run_follow_up` task |
| `worker/tests/test_runner.py` | Tests for `run_follow_up` |
| `worker/tests/test_github_client.py` | Tests for GraphQL methods |
