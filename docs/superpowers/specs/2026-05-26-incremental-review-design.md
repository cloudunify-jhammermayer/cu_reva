# Incremental PR Review & Comment Resolution

**Date:** 2026-05-26
**Status:** Approved

## Problem

When a developer pushes a new commit to address REVA's findings, REVA re-reviews the full `base..head` diff and re-reports all previously flagged issues as new comments. Resolved issues are never marked resolved. This creates noise and discourages developers from acting on reviews.

## Goal

On every commit after the first review:
1. Only report findings introduced by the new commits (`last_reviewed_sha..new_head`).
2. For old findings whose files were touched, verify whether the issue is still present; resolve the GitHub thread if it is fixed.

## Scope

- **In scope:** `synchronize` events on PRs that already have a completed review.
- **Out of scope:** Reopened PRs (treated as first review), deep-review mode (uses same logic), comment replies, ticket analysis.

---

## Architecture

Three new capabilities bolt onto the existing pipeline. One field is added to `ReviewResult`: `delta_base_sha: str | None = None` — the reviewer sets this when taking the delta path; the runner checks it to decide whether to call `_verify_and_resolve_findings()`. No other type changes to `Finding`, `ClaudeResponse`, or the existing full-review path.

### 1. Delta Detection (Reviewer)

`Reviewer.execute()` adds a branch after the stale-SHA check:

```
last_review = repos.get_last_completed_review(pull_request_id)
if last_review:
    diff = github.get_compare_diff(token, owner, repo,
                                   last_review.head_sha, params.head_sha)
    skill = "reva-delta-review"
else:
    diff = github.get_pull_request_diff(...)     # unchanged
    skill = "reva-diff-review" / "reva-full-review"
```

If the delta diff is empty the reviewer returns `status="stale"` immediately.

### 2. Finding Verification (Runner)

After `_post_completed()` succeeds on a delta review, the runner calls `_verify_and_resolve_findings()`:

```
threads = github.get_review_threads(token, owner, repo, pr_number)
# threads: dict[int, str]  →  github_comment_id → thread_node_id

old_findings = db.get_open_findings_for_pr(pull_request_id)
# touched_files parsed from the delta diff string via a new
# diff_utils.extract_file_paths(diff: str) → set[str] helper
touched_files = diff_utils.extract_file_paths(result.diff)

for finding in old_findings:
    if finding.file_path not in touched_files:
        continue
    content = github.get_file_content(token, owner, repo,
                                      finding.file_path, new_head)
    if content is None:
        continue   # file deleted — skip; GitHub marks comment outdated automatically
    if verifier.is_resolved(finding, content):
        thread_id = threads.get(finding.github_comment_id)
        if thread_id:
            github.resolve_review_thread(token, thread_id)
```

All errors in the verification/resolution loop are caught and logged; they never block the review result.

### 3. Thread Resolution (GitHub Client)

One new REST method and two new GraphQL methods on `GitHubClient`:

| Method | Transport | Purpose |
|--------|-----------|---------|
| `get_compare_diff(token, owner, repo, base, head) → str` | REST GET `/compare/{base}...{head}` | Delta unified diff |
| `get_review_threads(token, owner, repo, pr_number) → dict[int, str]` | GraphQL query | `{databaseId → threadNodeId}` for all unresolved threads |
| `resolve_review_thread(token, thread_node_id) → None` | GraphQL mutation `resolveReviewThread` | Mark thread resolved |

GraphQL calls POST to `{base_url}/graphql` using the existing `httpx.Client`.

---

## New Components

### `reva/finding_verifier.py` — `FindingVerifier`

```python
@dataclass
class StoredFinding:
    file_path: str
    line_start: int | None
    title: str
    body: str
    severity: str
    category: str

class FindingVerifier:
    def __init__(self, claude: ClaudeClient) -> None: ...
    def is_resolved(self, finding: StoredFinding, file_content: str) -> bool: ...
```

Uses the Messages API (not CLI subprocess). Forces `tool_choice` to a single tool:
```json
{"name": "verify_finding", "input_schema": {"resolved": "bool", "reason": "str"}}
```

**System prompt:** ~5 sentences. You are a code reviewer checking whether a previously flagged issue has been fixed. Given the finding and the current file content, determine if the issue is still present at or near the original location. Be conservative — only mark resolved if you are confident the issue no longer exists.

**User prompt:** finding title, severity, body, original line number, full current file content.

Returns `True` if resolved, `False` otherwise. Raises on API error (caller catches).

### `prompts/skills/reva-delta-review.md`

Variant of `reva-diff-review.md`. Key addition in the preamble:

> This diff represents only the commits added since the last review of this PR, not the full PR diff. Report only findings visible within the changed lines. Do not report issues that may exist in unchanged parts of the file.

Output schema is identical to the full review — same `ReviewResult` shape. No type changes needed.

---

## DB Changes

**No migration required.**

- `review_findings.github_comment_id` — already populated; used to look up thread node IDs at resolution time via `get_review_threads`.
- `review_runs.head_sha` — already stored; used as `last_reviewed_sha` for delta base.

One new query function added to the worker's DB layer:

```python
def get_last_completed_review(db, pull_request_id: int) -> dict | None:
    """Returns {id, head_sha} of the most recent completed review_run, or None."""
```

And one for the runner:

```python
def get_open_findings_for_pr(db, pull_request_id: int) -> list[StoredFinding]:
    """Open findings (github_comment_id set) from the most recent completed review_run."""
```

---

## Protocol Changes

`RepoLookup` protocol (in `reviewer.py`) gains one method:

```python
def get_last_completed_review(self, pull_request_id: int) -> dict | None:
    """Returns {id, head_sha} or None if no completed review exists."""
```

The concrete `DatabaseRepoLookup` in `reva/db/repo_lookup.py` implements it.

---

## Error Handling

| Scenario | Behaviour |
|----------|-----------|
| Delta diff is empty | Return `status="stale"`, skip all verification |
| GitHub Compare API fails | Bubble up as `TransientError` (RQ retries) |
| `get_file_content` returns None (deleted file) | Skip verification for that finding |
| `is_resolved()` raises | Log warning, skip resolution for that finding |
| `resolve_review_thread` fails | Log warning, continue — review result is already posted |
| `get_review_threads` fails | Log warning, skip all resolution — new findings were already posted |

---

## Testing

- `test_github_client.py` — 3 new tests: `get_compare_diff`, `get_review_threads`, `resolve_review_thread`
- `test_finding_verifier.py` — new file: resolved/not-resolved/API-error cases
- `test_diff_utils.py` — `extract_file_paths` parses file paths from unified diff
- `test_reviewer.py` — delta path taken when prior review exists; full path when none; `delta_base_sha` set correctly
- `test_runner.py` — `_verify_and_resolve_findings` called after delta post; errors swallowed

All tests use mocks/fakes — no live network calls.

---

## Cost

Per re-review: one `get_compare_diff` call + one `get_review_threads` call + N `is_resolved` calls (N = open findings in touched files, typically 3–8). Each verification call is small (file content + short prompt). Total token cost is roughly equivalent to one ticket analysis.
