# Follow-Up Check Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a developer pushes to a PR that already has a completed REVA review with inline findings, run a targeted follow-up check instead of a full re-scan: ask Claude whether each finding was addressed, resolve fixed threads via GitHub GraphQL, and reply to open ones.

**Architecture:** Routing happens at webhook time — `_handle_pull_request` checks for a prior completed run with inline findings; if found it enqueues `run_follow_up` directly into RQ (no debounce) and skips the full-review path. The follow-up task calls a new `ClaudeClient.check_findings()` method with a dedicated tool schema, then uses three new GitHub GraphQL methods to resolve threads.

**Tech Stack:** Python 3.14, SQLAlchemy 2.0, httpx, FastAPI, RQ, GitHub GraphQL API (same installation token as REST)

---

## File Map

| File | Change |
|---|---|
| `db/migrations/006_follow_up_check.sql` | **CREATE** — adds `resolved_at`, `resolved_sha` to `review_findings` + partial index |
| `reva/db/models.py` | **MODIFY** — add 2 columns + index to `ReviewFinding` |
| `reva/db/writers.py` | **MODIFY** — add 3 writer functions |
| `reva/types.py` | **MODIFY** — add `FollowUpParams` |
| `reva/review_tool.py` | **MODIFY** — add `FOLLOW_UP_TOOL_NAME`, `build_follow_up_tool_schema()`, `tool_choice_force_follow_up()` |
| `reva/claude_client.py` | **MODIFY** — add `check_findings()` method |
| `reva/github_client.py` | **MODIFY** — add `_graphql()`, `get_pr_review_threads()`, `resolve_review_thread()` |
| `worker/worker/runner.py` | **MODIFY** — add `run_follow_up()` RQ task |
| `api/app/routes/webhooks.py` | **MODIFY** — routing logic in `_handle_pull_request` |
| `worker/tests/test_db.py` | **MODIFY** — 5 tests for new writer functions |
| `worker/tests/test_claude_client.py` | **MODIFY** — 2 tests for `check_findings` |
| `worker/tests/test_github_client.py` | **MODIFY** — 3 tests for GraphQL methods |
| `worker/tests/test_runner.py` | **MODIFY** — `FakeClaude` + new `FakeGitHub` methods + 5 tests for `run_follow_up` |
| `api/tests/test_webhooks.py` | **MODIFY** — `FakeQueue` + 2 tests for routing |

---

## Task 1: DB Migration + Model

**Files:**
- Create: `db/migrations/006_follow_up_check.sql`
- Modify: `reva/db/models.py`

- [ ] **Step 1: Create migration file**

```sql
-- db/migrations/006_follow_up_check.sql
ALTER TABLE review_findings ADD COLUMN resolved_at  TIMESTAMPTZ;
ALTER TABLE review_findings ADD COLUMN resolved_sha TEXT;

CREATE INDEX idx_findings_resolved
    ON review_findings (review_run_id)
    WHERE resolved_at IS NULL;
```

- [ ] **Step 2: Add columns to `ReviewFinding` in `reva/db/models.py`**

Add these two lines to the `ReviewFinding` class after `posted_to_github`:

```python
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
```

Also add this index to `ReviewFinding.__table_args__`:

```python
        Index(
            "idx_findings_resolved",
            "review_run_id",
            postgresql_where=text("resolved_at IS NULL"),
        ),
```

The full `__table_args__` for `ReviewFinding` becomes:

```python
    __table_args__ = (
        Index("idx_findings_run", "review_run_id"),
        Index("idx_findings_severity", "severity"),
        Index("idx_findings_category", "category"),
        Index("idx_findings_file", "file_path"),
        Index(
            "idx_findings_resolved",
            "review_run_id",
            postgresql_where=text("resolved_at IS NULL"),
        ),
    )
```

- [ ] **Step 3: Verify migration file and model are consistent**

Run:
```bash
cd worker && python3.14 -m pytest tests/test_db.py -v 2>&1 | tail -5
```
Expected: all existing DB tests still pass (PASS).

- [ ] **Step 4: Commit**

```bash
git add db/migrations/006_follow_up_check.sql reva/db/models.py
git commit -m "feat: add resolved_at/resolved_sha columns to review_findings"
```

---

## Task 2: DB Writer Functions

**Files:**
- Modify: `reva/db/writers.py`
- Test: `worker/tests/test_db.py`

- [ ] **Step 1: Write failing tests in `worker/tests/test_db.py`**

Add these imports to the top of `test_db.py`:

```python
from reva.db.models import ReviewFinding, ReviewRun
```

Add this helper after the existing `seeded` fixture (around line 60):

```python
def _seed_completed_run(db: Database, seeded: dict, *, with_inline: bool = True) -> tuple[int, int | None]:
    """Insert a completed ReviewRun. If with_inline, also insert a ReviewFinding with github_comment_id set.
    Returns (run_id, finding_id or None)."""
    from reva.types import JobParams, ReviewResult, Finding
    params = JobParams(
        repository_id=seeded["repo_id"],
        pull_request_id=seeded["pr_id"],
        head_sha="aabbccdd",
        installation_id=500,
        review_mode="diff",
        trigger_event="opened",
    )
    run_id = writers.record_review_completed(
        db, params,
        ReviewResult(
            status="completed",
            summary="ok",
            risk_level="low",
            findings=[Finding(
                severity="major", category="bug",
                file="x.py", line_start=10, line_end=10,
                title="Issue", body="desc", confidence=0.9,
            )] if with_inline else [],
        ),
    )
    if not with_inline:
        return run_id, None
    with db.session() as s:
        f = s.execute(
            __import__("sqlalchemy").select(ReviewFinding)
            .where(ReviewFinding.review_run_id == run_id)
        ).scalar_one()
        f.github_comment_id = 999
        f.posted_to_github = True
        fid = f.id
    return run_id, fid
```

Now add the five new tests:

```python
def test_get_latest_run_returns_none_when_no_runs(db: Database, seeded: dict):
    result = writers.get_latest_run_with_inline_findings(db, seeded["pr_id"])
    assert result is None


def test_get_latest_run_returns_none_when_no_inline_findings(db: Database, seeded: dict):
    _seed_completed_run(db, seeded, with_inline=False)
    result = writers.get_latest_run_with_inline_findings(db, seeded["pr_id"])
    assert result is None


def test_get_latest_run_returns_run_id_and_installation_id(db: Database, seeded: dict):
    run_id, _ = _seed_completed_run(db, seeded, with_inline=True)
    result = writers.get_latest_run_with_inline_findings(db, seeded["pr_id"])
    assert result is not None
    assert result["run_id"] == run_id
    assert result["installation_id"] == 500


def test_get_inline_findings_for_run_excludes_resolved(db: Database, seeded: dict):
    run_id, fid = _seed_completed_run(db, seeded, with_inline=True)
    # Before resolving: 1 result.
    rows = writers.get_inline_findings_for_run(db, run_id)
    assert len(rows) == 1
    assert rows[0]["github_comment_id"] == 999

    # After resolving: 0 results.
    writers.mark_findings_resolved(db, [fid], "newsha")
    rows = writers.get_inline_findings_for_run(db, run_id)
    assert rows == []


def test_mark_findings_resolved_sets_sha(db: Database, seeded: dict):
    run_id, fid = _seed_completed_run(db, seeded, with_inline=True)
    writers.mark_findings_resolved(db, [fid], "resolved_sha_abc")
    with db.session() as s:
        f = s.get(ReviewFinding, fid)
        assert f.resolved_at is not None
        assert f.resolved_sha == "resolved_sha_abc"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd worker && python3.14 -m pytest tests/test_db.py -k "get_latest_run or get_inline_findings or mark_findings" -v 2>&1 | tail -15
```
Expected: `AttributeError: module 'reva.db.writers' has no attribute 'get_latest_run_with_inline_findings'`

- [ ] **Step 3: Implement the three writer functions in `reva/db/writers.py`**

Add these imports at the top of the import block in `writers.py` (alongside existing `select`, `delete`):

```python
from sqlalchemy import select, delete  # already there — no change needed
```

Add these three functions at the end of `reva/db/writers.py`, before `# --- internals`:

```python
# --- follow-up check helpers --------------------------------------------------


def get_latest_run_with_inline_findings(
    db: Database, pull_request_id: int
) -> dict | None:
    """Return {run_id, installation_id} for the latest completed review_run for
    this PR that has at least one finding with github_comment_id set, or None."""
    has_inline = (
        select(ReviewFinding.review_run_id)
        .where(ReviewFinding.github_comment_id.isnot(None))
        .distinct()
        .scalar_subquery()
    )
    with db.session() as s:
        row = s.execute(
            select(
                ReviewRun.id,
                Repository.installation_id,
            )
            .join(Repository, ReviewRun.repository_id == Repository.id)
            .where(
                ReviewRun.pull_request_id == pull_request_id,
                ReviewRun.status == "completed",
                ReviewRun.id.in_(has_inline),
            )
            .order_by(ReviewRun.id.desc())
            .limit(1)
        ).first()
    if row is None:
        return None
    return {"run_id": row[0], "installation_id": row[1]}


def get_inline_findings_for_run(db: Database, run_id: int) -> list[dict]:
    """Return findings for a run that have a github_comment_id and are not yet resolved."""
    with db.session() as s:
        rows = s.execute(
            select(
                ReviewFinding.id,
                ReviewFinding.severity,
                ReviewFinding.title,
                ReviewFinding.body,
                ReviewFinding.file_path,
                ReviewFinding.line_start,
                ReviewFinding.suggestion,
                ReviewFinding.github_comment_id,
            )
            .where(
                ReviewFinding.review_run_id == run_id,
                ReviewFinding.github_comment_id.isnot(None),
                ReviewFinding.resolved_at.is_(None),
            )
        ).all()
    return [
        {
            "id": r[0],
            "severity": r[1],
            "title": r[2],
            "body": r[3],
            "file_path": r[4],
            "line_start": r[5],
            "suggestion": r[6],
            "github_comment_id": r[7],
        }
        for r in rows
    ]


def mark_findings_resolved(db: Database, finding_ids: list[int], sha: str) -> None:
    """Batch-set resolved_at and resolved_sha for a list of finding IDs."""
    if not finding_ids:
        return
    now = datetime.now(timezone.utc)
    with db.session() as s:
        for fid in finding_ids:
            finding = s.get(ReviewFinding, fid)
            if finding is not None:
                finding.resolved_at = now
                finding.resolved_sha = sha
```

Also add `Repository` to the existing import block in `writers.py` (it's already imported — verify):

```python
from reva.db.models import (
    GithubEvent,
    PendingReview,
    PullRequest,
    Repository,      # already present
    ReviewFinding,
    ReviewRun,
)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd worker && python3.14 -m pytest tests/test_db.py -k "get_latest_run or get_inline_findings or mark_findings" -v 2>&1 | tail -15
```
Expected: 5 PASSED.

- [ ] **Step 5: Run full DB test suite to check for regressions**

```bash
cd worker && python3.14 -m pytest tests/test_db.py -v 2>&1 | tail -5
```
Expected: all PASSED.

- [ ] **Step 6: Commit**

```bash
git add reva/db/writers.py worker/tests/test_db.py
git commit -m "feat: add follow-up DB writers (get_latest_run, get_inline_findings, mark_resolved)"
```

---

## Task 3: FollowUpParams Type

**Files:**
- Modify: `reva/types.py`

- [ ] **Step 1: Add `FollowUpParams` to `reva/types.py`**

Add this class after `JobParams` (around line 147):

```python
class FollowUpParams(BaseModel):
    """Inputs handed to runner.run_follow_up (the targeted re-push check)."""

    repository_id: int
    pull_request_id: int
    head_sha: str
    installation_id: int
    prior_run_id: int
```

- [ ] **Step 2: Verify import works**

```bash
cd worker && python3.14 -c "from reva.types import FollowUpParams; print(FollowUpParams.__fields__.keys())"
```
Expected: `dict_keys(['repository_id', 'pull_request_id', 'head_sha', 'installation_id', 'prior_run_id'])`

- [ ] **Step 3: Commit**

```bash
git add reva/types.py
git commit -m "feat: add FollowUpParams type"
```

---

## Task 4: Follow-Up Tool Schema

**Files:**
- Modify: `reva/review_tool.py`

- [ ] **Step 1: Add schema to `reva/review_tool.py`**

Add after the existing `tool_choice_force_submit` function:

```python
FOLLOW_UP_TOOL_NAME = "submit_follow_up_verdicts"

_FOLLOW_UP_DESCRIPTION = (
    "Report whether each finding has been addressed in the new diff. "
    "You MUST call this tool exactly once with a verdict for every finding ID you were given."
)


def build_follow_up_tool_schema() -> dict:
    """Return the Anthropic tool definition for submit_follow_up_verdicts."""
    return {
        "name": FOLLOW_UP_TOOL_NAME,
        "description": _FOLLOW_UP_DESCRIPTION,
        "input_schema": {
            "type": "object",
            "required": ["verdicts"],
            "additionalProperties": False,
            "properties": {
                "verdicts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["finding_id", "verdict", "reasoning"],
                        "additionalProperties": False,
                        "properties": {
                            "finding_id": {"type": "integer"},
                            "verdict": {
                                "type": "string",
                                "enum": ["fixed", "open"],
                            },
                            "reasoning": {
                                "type": "string",
                                "maxLength": 200,
                            },
                        },
                    },
                }
            },
        },
    }


def tool_choice_force_follow_up() -> dict:
    """Tool-choice value that forces Claude to call submit_follow_up_verdicts."""
    return {"type": "tool", "name": FOLLOW_UP_TOOL_NAME}
```

- [ ] **Step 2: Verify import works**

```bash
cd worker && python3.14 -c "from reva.review_tool import build_follow_up_tool_schema, tool_choice_force_follow_up; print('ok')"
```
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add reva/review_tool.py
git commit -m "feat: add submit_follow_up_verdicts tool schema"
```

---

## Task 5: ClaudeClient.check_findings()

**Files:**
- Modify: `reva/claude_client.py`
- Test: `worker/tests/test_claude_client.py`

- [ ] **Step 1: Write failing tests in `worker/tests/test_claude_client.py`**

Add these two tests at the end of the file:

```python
def test_check_findings_returns_verdicts():
    verdicts_payload = [
        {"finding_id": 1, "verdict": "fixed", "reasoning": "Null check added."},
        {"finding_id": 2, "verdict": "open", "reasoning": "Still missing."},
    ]

    def handler(req):
        return httpx.Response(
            200,
            json={
                "id": "msg_fu",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "stop_reason": "tool_use",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_fu",
                        "name": "submit_follow_up_verdicts",
                        "input": {"verdicts": verdicts_payload},
                    }
                ],
                "usage": {
                    "input_tokens": 300,
                    "output_tokens": 80,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
            },
        )

    transport = httpx.MockTransport(handler)
    client = ClaudeClient(api_key="test", client=httpx.Client(transport=transport))

    findings = [
        {"id": 1, "severity": "major", "title": "T1", "body": "B1",
         "file_path": "x.py", "line_start": 5, "suggestion": None},
        {"id": 2, "severity": "minor", "title": "T2", "body": "B2",
         "file_path": None, "line_start": None, "suggestion": None},
    ]

    result = client.check_findings(findings, "diff content here")

    assert len(result) == 2
    assert result[0]["finding_id"] == 1
    assert result[0]["verdict"] == "fixed"
    assert result[1]["finding_id"] == 2
    assert result[1]["verdict"] == "open"


def test_check_findings_raises_permanent_error_when_tool_missing():
    def handler(req):
        return httpx.Response(
            200,
            json={
                "id": "msg_bad",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "I cannot help with that."}],
                "usage": {"input_tokens": 10, "output_tokens": 10,
                          "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            },
        )

    transport = httpx.MockTransport(handler)
    client = ClaudeClient(api_key="test", client=httpx.Client(transport=transport))

    with pytest.raises(PermanentError, match="submit_follow_up_verdicts"):
        client.check_findings([{"id": 1, "severity": "major", "title": "T",
                                 "body": "B", "file_path": None, "line_start": None,
                                 "suggestion": None}], "diff")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd worker && python3.14 -m pytest tests/test_claude_client.py -k "check_findings" -v 2>&1 | tail -10
```
Expected: `AttributeError: 'ClaudeClient' object has no attribute 'check_findings'`

- [ ] **Step 3: Implement `check_findings` in `reva/claude_client.py`**

Add this import at the top of `claude_client.py` (alongside the existing `REVIEW_TOOL_NAME` import):

```python
from reva.review_tool import (
    REVIEW_TOOL_NAME,
    FOLLOW_UP_TOOL_NAME,
    build_follow_up_tool_schema,
    tool_choice_force_follow_up,
)
```

Add the `check_findings` method to `ClaudeClient`, after the `chat` method and before `close`:

```python
    def check_findings(
        self,
        findings: list[dict],
        diff: str,
        model: str | None = None,
    ) -> list[dict]:
        """Ask Claude to classify each finding as 'fixed' or 'open' against a new diff.

        Returns a list of {finding_id, verdict, reasoning} dicts.
        Raises TransientError / PermanentError with the same semantics as review().
        """
        system = (
            "You are REVA, an automated code review assistant. "
            "You are checking whether previously-identified issues have been addressed "
            "in a new diff. For each finding, determine if the issue is 'fixed' or 'open'. "
            "Be concise in your reasoning (one sentence max). "
            "You MUST call submit_follow_up_verdicts exactly once with a verdict for every finding."
        )

        findings_text = "\n\n".join(
            "Finding ID {id} [{sev}] {title}\nFile: {file}{line}\n{body}".format(
                id=f["id"],
                sev=f["severity"].upper(),
                title=f["title"],
                file=f.get("file_path") or "(general)",
                line=f" line {f['line_start']}" if f.get("line_start") else "",
                body=f["body"],
            )
            for f in findings
        )

        user_prompt = (
            f"## Findings to check ({len(findings)} total)\n\n"
            f"{findings_text}\n\n"
            f"## New diff\n\n```diff\n{diff}\n```"
        )

        body = {
            "model": model or self.default_model,
            "max_tokens": 2048,
            "system": system,
            "messages": [{"role": "user", "content": user_prompt}],
            "tools": [build_follow_up_tool_schema()],
            "tool_choice": tool_choice_force_follow_up(),
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

        try:
            response = self._client.post(self.BASE_URL, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise TransientError(f"Claude request timed out: {exc}") from exc
        except httpx.TransportError as exc:
            raise TransientError(f"Claude transport error: {exc}") from exc

        if response.status_code != 200:
            raise _map_status_to_error(
                response.status_code,
                response.headers.get("retry-after"),
                response.text,
            )

        content = response.json().get("content") or []
        for block in content:
            if block.get("type") == "tool_use" and block.get("name") == FOLLOW_UP_TOOL_NAME:
                return block.get("input", {}).get("verdicts", [])

        raise PermanentError(
            f"Claude response missing tool_use[{FOLLOW_UP_TOOL_NAME}] block "
            f"(stop_reason={response.json().get('stop_reason')!r})"
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd worker && python3.14 -m pytest tests/test_claude_client.py -v 2>&1 | tail -10
```
Expected: all PASSED.

- [ ] **Step 5: Commit**

```bash
git add reva/claude_client.py reva/review_tool.py worker/tests/test_claude_client.py
git commit -m "feat: add ClaudeClient.check_findings() with follow-up tool schema"
```

---

## Task 6: GitHubClient GraphQL Methods

**Files:**
- Modify: `reva/github_client.py`
- Test: `worker/tests/test_github_client.py`

- [ ] **Step 1: Write failing tests in `worker/tests/test_github_client.py`**

Add `import json` to the existing imports at the top of `test_github_client.py`.

Add these three tests at the end of the file:

```python
def test_get_pr_review_threads_returns_thread_map(rsa_key_pair):
    private_pem, _ = rsa_key_pair

    def handler(req):
        return httpx.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "nodes": [
                                    {
                                        "id": "PRRT_node1",
                                        "isResolved": False,
                                        "comments": {"nodes": [{"databaseId": 999}]},
                                    },
                                    {
                                        "id": "PRRT_node2",
                                        "isResolved": True,
                                        "comments": {"nodes": [{"databaseId": 1234}]},
                                    },
                                ]
                            }
                        }
                    }
                }
            },
        )

    client = _make_client(handler, private_pem)
    result = client.get_pr_review_threads("tok", "acme", "widgets", 42)

    assert result == {
        999: ("PRRT_node1", False),
        1234: ("PRRT_node2", True),
    }


def test_resolve_review_thread_sends_mutation(rsa_key_pair):
    private_pem, _ = rsa_key_pair
    captured: dict = {}

    def handler(req):
        captured["body"] = json.loads(req.content)
        return httpx.Response(
            200,
            json={
                "data": {
                    "markPullRequestReviewThreadAsResolved": {
                        "thread": {"id": "PRRT_node1", "isResolved": True}
                    }
                }
            },
        )

    client = _make_client(handler, private_pem)
    client.resolve_review_thread("tok", "PRRT_node1")

    assert captured["body"]["variables"] == {"threadId": "PRRT_node1"}
    assert "markPullRequestReviewThreadAsResolved" in captured["body"]["query"]


def test_graphql_raises_permanent_error_on_errors_field(rsa_key_pair):
    private_pem, _ = rsa_key_pair

    def handler(req):
        return httpx.Response(200, json={"errors": [{"message": "Resource not accessible"}]})

    client = _make_client(handler, private_pem)
    with pytest.raises(PermanentError, match="GraphQL errors"):
        client.get_pr_review_threads("tok", "acme", "widgets", 42)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd worker && python3.14 -m pytest tests/test_github_client.py -k "graphql or review_threads or resolve_review" -v 2>&1 | tail -10
```
Expected: `AttributeError: 'GitHubClient' object has no attribute 'get_pr_review_threads'`

- [ ] **Step 3: Implement GraphQL methods in `reva/github_client.py`**

Add the following constants and methods to `GitHubClient`. Place the constants before the class, after `PAGE_SIZE = 100`:

```python
_GET_THREADS_QUERY = """
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
"""

_RESOLVE_THREAD_MUTATION = """
mutation ResolveThread($threadId: ID!) {
  markPullRequestReviewThreadAsResolved(
    input: {pullRequestReviewThreadId: $threadId}
  ) {
    thread { id isResolved }
  }
}
"""
```

Add these three methods to `GitHubClient`, after `reply_to_review_comment` and before `_get`:

```python
    # --- GraphQL ----------------------------------------------------------------

    def _graphql(self, token: str, query: str, variables: dict) -> dict:
        """Execute a GraphQL query or mutation. Returns the `data` dict.

        Uses the same installation token as REST. Raises TransientError on
        network failure, PermanentError on GraphQL-level errors.
        """
        url = f"{self.base_url}/graphql"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            response = self._client.post(
                url,
                headers=headers,
                json={"query": query, "variables": variables},
            )
        except httpx.TimeoutException as exc:
            raise TransientError(f"GitHub GraphQL timeout: {exc}") from exc
        except httpx.TransportError as exc:
            raise TransientError(f"GitHub GraphQL transport error: {exc}") from exc

        if response.status_code >= 300:
            raise map_github_status(response, action="graphql")

        payload = response.json()
        errors = payload.get("errors")
        if errors:
            raise PermanentError(f"GitHub GraphQL errors: {errors}")
        return payload.get("data", {})

    def get_pr_review_threads(
        self, token: str, owner: str, repo: str, pr_number: int
    ) -> dict[int, tuple[str, bool]]:
        """Return {comment_database_id: (thread_node_id, is_resolved)} for all threads on a PR."""
        data = self._graphql(
            token,
            _GET_THREADS_QUERY,
            {"owner": owner, "repo": repo, "number": pr_number},
        )
        nodes = (
            data.get("repository", {})
            .get("pullRequest", {})
            .get("reviewThreads", {})
            .get("nodes", [])
        )
        result: dict[int, tuple[str, bool]] = {}
        for t in nodes:
            comments = t.get("comments", {}).get("nodes", [])
            if comments and comments[0].get("databaseId"):
                db_id = comments[0]["databaseId"]
                result[db_id] = (t["id"], t.get("isResolved", False))
        return result

    def resolve_review_thread(self, token: str, thread_id: str) -> None:
        """Resolve a PR review thread by its GraphQL Node ID."""
        self._graphql(token, _RESOLVE_THREAD_MUTATION, {"threadId": thread_id})
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd worker && python3.14 -m pytest tests/test_github_client.py -v 2>&1 | tail -10
```
Expected: all PASSED.

- [ ] **Step 5: Commit**

```bash
git add reva/github_client.py worker/tests/test_github_client.py
git commit -m "feat: add GitHubClient GraphQL support (get_pr_review_threads, resolve_review_thread)"
```

---

## Task 7: run_follow_up Task

**Files:**
- Modify: `worker/worker/runner.py`
- Test: `worker/tests/test_runner.py`

- [ ] **Step 1: Add `FakeClaude` and new `FakeGitHub` methods to `worker/tests/test_runner.py`**

Add `FakeClaude` after `FakeGitHub`:

```python
@dataclass
class FakeClaude:
    verdicts: list[dict] = field(default_factory=list)

    def check_findings(
        self, findings: list[dict], diff: str, model: str | None = None
    ) -> list[dict]:
        return self.verdicts
```

Add these fields and methods to the existing `FakeGitHub` dataclass (add fields after `diff_fetch_count: int = 0`):

```python
    review_threads: dict = field(default_factory=dict)
    resolved_threads: list[str] = field(default_factory=list)
    replies: list[dict] = field(default_factory=list)
```

Add these methods to `FakeGitHub` (after `create_issue_comment`):

```python
    def get_pr_review_threads(self, token, owner, repo, pr_number):
        return self.review_threads

    def resolve_review_thread(self, token, thread_id):
        self.resolved_threads.append(thread_id)

    def reply_to_review_comment(self, token, owner, repo, pr_number, comment_id, body):
        self.replies.append({"comment_id": comment_id, "body": body})
        cid = self.next_comment_id
        self.next_comment_id += 1
        return cid
```

- [ ] **Step 2: Add `ctx_and_fakes_for_follow_up` fixture to `worker/tests/test_runner.py`**

Add this import at the top of the test file (alongside existing imports):

```python
from sqlalchemy import select as sa_select
from reva.db.models import ReviewFinding as ReviewFindingModel
from reva.types import FollowUpParams
from worker.runner import run_follow_up
```

Add this fixture after `ctx_and_fakes`:

```python
@pytest.fixture()
def ctx_and_fakes_for_follow_up():
    """ctx_and_fakes extended with a seeded completed run, inline finding, and FakeClaude."""
    engine = create_engine_from_url("sqlite:///:memory:")
    from reva.db import Base
    Base.metadata.create_all(engine)
    db = Database(engine)

    repo_id = writers.upsert_repository(
        db,
        github_repository_id=1,
        owner="acme",
        name="widgets",
        default_branch="main",
        installation_id=500,
    )
    pr_id = writers.upsert_pull_request(
        db,
        repository_id=repo_id,
        github_pr_id=9001,
        pr_number=42,
        title="Add foo",
        author_login="alice",
        base_branch="main",
        head_branch="feat/foo",
        head_sha="deadbeef",
        state="open",
        draft=False,
    )

    # Seed a completed review_run with one inline finding (github_comment_id=999).
    prior_params = JobParams(
        repository_id=repo_id,
        pull_request_id=pr_id,
        head_sha="deadbeef",
        installation_id=500,
        review_mode="diff",
        trigger_event="opened",
    )
    run_id = writers.record_review_completed(
        db,
        prior_params,
        _completed_result(findings=[_f("major", file="x.py", line_start=12)]),
    )
    with db.session() as s:
        f = s.execute(
            sa_select(ReviewFindingModel).where(ReviewFindingModel.review_run_id == run_id)
        ).scalar_one()
        f.github_comment_id = 999
        f.posted_to_github = True
        finding_id = f.id

    claude = FakeClaude()
    github = FakeGitHub()
    context = WorkerContext(
        db=db,
        claude=claude,  # type: ignore[arg-type]
        github=github,  # type: ignore[arg-type]
        reviewer=None,  # type: ignore[arg-type]
    )
    set_context(context)
    return {
        "ctx": context,
        "db": db,
        "github": github,
        "claude": claude,
        "repo_id": repo_id,
        "pr_id": pr_id,
        "run_id": run_id,
        "finding_id": finding_id,
    }


def _follow_up_params(s: dict, **overrides) -> dict:
    base = {
        "repository_id": s["repo_id"],
        "pull_request_id": s["pr_id"],
        "head_sha": "newsha001",
        "installation_id": 500,
        "prior_run_id": s["run_id"],
    }
    base.update(overrides)
    return base
```

- [ ] **Step 3: Write failing tests for `run_follow_up`**

Add these five tests at the end of `worker/tests/test_runner.py`:

```python
def test_follow_up_fixed_finding_resolves_and_marks(ctx_and_fakes_for_follow_up):
    s = ctx_and_fakes_for_follow_up

    s["claude"].verdicts = [
        {"finding_id": s["finding_id"], "verdict": "fixed", "reasoning": "Null check added."}
    ]
    s["github"].review_threads = {999: ("PRRT_node123", False)}

    result = run_follow_up(_follow_up_params(s))

    assert result == {"status": "done", "fixed": 1, "total": 1}
    assert len(s["github"].replies) == 1
    assert "✅" in s["github"].replies[0]["body"]
    assert s["github"].replies[0]["comment_id"] == 999
    assert s["github"].resolved_threads == ["PRRT_node123"]

    with s["db"].session() as session:
        f = session.get(ReviewFindingModel, s["finding_id"])
        assert f.resolved_at is not None
        assert f.resolved_sha == "newsha001"


def test_follow_up_open_finding_posts_warning_reply(ctx_and_fakes_for_follow_up):
    s = ctx_and_fakes_for_follow_up

    s["claude"].verdicts = [
        {"finding_id": s["finding_id"], "verdict": "open", "reasoning": "Still no validation."}
    ]
    s["github"].review_threads = {999: ("PRRT_node123", False)}

    run_follow_up(_follow_up_params(s))

    assert len(s["github"].replies) == 1
    assert "⚠️" in s["github"].replies[0]["body"]
    assert "newsha00" in s["github"].replies[0]["body"]
    assert s["github"].resolved_threads == []

    with s["db"].session() as session:
        f = session.get(ReviewFindingModel, s["finding_id"])
        assert f.resolved_at is None


def test_follow_up_skips_already_resolved_thread(ctx_and_fakes_for_follow_up):
    s = ctx_and_fakes_for_follow_up

    # Thread already resolved by developer (isResolved=True).
    s["github"].review_threads = {999: ("PRRT_node123", True)}

    run_follow_up(_follow_up_params(s))

    assert s["github"].replies == []
    assert s["github"].resolved_threads == []

    # Finding still marked resolved in DB (already handled).
    with s["db"].session() as session:
        f = session.get(ReviewFindingModel, s["finding_id"])
        assert f.resolved_at is not None


def test_follow_up_omitted_verdict_treated_as_open(ctx_and_fakes_for_follow_up):
    s = ctx_and_fakes_for_follow_up

    # Claude returns no verdicts.
    s["claude"].verdicts = []
    s["github"].review_threads = {999: ("PRRT_node123", False)}

    run_follow_up(_follow_up_params(s))

    # Conservative: treated as open → warning reply, no resolve.
    assert len(s["github"].replies) == 1
    assert "⚠️" in s["github"].replies[0]["body"]
    assert s["github"].resolved_threads == []


def test_follow_up_no_inline_findings_posts_guidance_comment(ctx_and_fakes):
    s = ctx_and_fakes
    # Seed a completed run with no inline findings (no github_comment_id).
    prior_params = JobParams(
        repository_id=s["repo_id"],
        pull_request_id=s["pr_id"],
        head_sha="deadbeef",
        installation_id=500,
        review_mode="diff",
        trigger_event="opened",
    )
    run_id = writers.record_review_completed(s["db"], prior_params, _completed_result())

    result = run_follow_up({
        "repository_id": s["repo_id"],
        "pull_request_id": s["pr_id"],
        "head_sha": "newsha001",
        "installation_id": 500,
        "prior_run_id": run_id,
    })

    assert result == {"status": "no_inline_findings"}
    assert len(s["github"].created_issue_comments) == 1
    assert "/review" in s["github"].created_issue_comments[0]["body"]
```

- [ ] **Step 4: Run tests to verify they fail**

```bash
cd worker && python3.14 -m pytest tests/test_runner.py -k "follow_up" -v 2>&1 | tail -10
```
Expected: `ImportError` or `AttributeError: module 'worker.runner' has no attribute 'run_follow_up'`

- [ ] **Step 5: Implement `run_follow_up` in `worker/worker/runner.py`**

Add `FollowUpParams` to the existing types import at the top of `runner.py`:

```python
from reva.types import JobParams, ReviewResult, FollowUpParams
```

Add `run_follow_up` at the end of `runner.py`, after `run_weekly_report`:

```python
def run_follow_up(job_params: dict) -> dict:
    """RQ task: targeted follow-up check on a PR's previous inline findings.

    For each unresolved inline finding from prior_run_id, asks Claude whether
    it was addressed in the new push, then replies to each thread and resolves
    fixed ones via GraphQL.
    """
    ctx = get_context()
    params = FollowUpParams.model_validate(job_params)

    log = logger.bind(
        pull_request_id=params.pull_request_id,
        head_sha=params.head_sha[:8],
        prior_run_id=params.prior_run_id,
    )
    log.info("follow_up_start")

    findings = writers.get_inline_findings_for_run(ctx.db, params.prior_run_id)

    owner, name = repo_lookup.get_owner_name(ctx.db, params.repository_id)
    pr_basic = repo_lookup.get_pr_basic(ctx.db, params.pull_request_id)
    pr_number = pr_basic["pr_number"]
    token = ctx.github.get_installation_token(params.installation_id)

    if not findings:
        ctx.github.create_issue_comment(
            token=token,
            owner=owner,
            repo=name,
            pr_number=pr_number,
            body=(
                "All findings from the previous review were general (not inline comments). "
                "Use `/review` for a fresh scan of this push.\n\n*REVA*"
            ),
        )
        log.info("follow_up_no_inline_findings")
        return {"status": "no_inline_findings"}

    diff = ctx.github.get_pull_request_diff(token, owner, name, pr_number)
    verdicts = ctx.claude.check_findings(findings, diff)
    verdict_map: dict[int, dict] = {v["finding_id"]: v for v in verdicts}

    thread_map = ctx.github.get_pr_review_threads(token, owner, name, pr_number)

    fixed_ids: list[int] = []

    for f in findings:
        fid = f["id"]
        comment_id = f["github_comment_id"]
        thread_info = thread_map.get(comment_id)

        if thread_info is None:
            log.warning("follow_up_thread_not_found", finding_id=fid, comment_id=comment_id)
            continue

        thread_node_id, is_resolved = thread_info

        if is_resolved:
            fixed_ids.append(fid)
            continue

        verdict = verdict_map.get(fid) or {
            "finding_id": fid,
            "verdict": "open",
            "reasoning": "Not evaluated by Claude.",
        }
        reasoning = verdict.get("reasoning", "")

        if verdict["verdict"] == "fixed":
            ctx.github.reply_to_review_comment(
                token=token,
                owner=owner,
                repo=name,
                pr_number=pr_number,
                comment_id=comment_id,
                body=f"✅ **Addressed** — {reasoning}\n\n*REVA*",
            )
            try:
                ctx.github.resolve_review_thread(token, thread_node_id)
            except Exception:
                log.warning("follow_up_resolve_failed", finding_id=fid, exc_info=True)
            fixed_ids.append(fid)
        else:
            ctx.github.reply_to_review_comment(
                token=token,
                owner=owner,
                repo=name,
                pr_number=pr_number,
                comment_id=comment_id,
                body=f"⚠️ **Still open** after `{params.head_sha[:7]}` — {reasoning}\n\n*REVA*",
            )

    if fixed_ids:
        writers.mark_findings_resolved(ctx.db, fixed_ids, params.head_sha)

    log.info("follow_up_done", fixed=len(fixed_ids), total=len(findings))
    return {"status": "done", "fixed": len(fixed_ids), "total": len(findings)}
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
cd worker && python3.14 -m pytest tests/test_runner.py -k "follow_up" -v 2>&1 | tail -15
```
Expected: 5 PASSED.

- [ ] **Step 7: Run full runner test suite to check for regressions**

```bash
cd worker && python3.14 -m pytest tests/test_runner.py -v 2>&1 | tail -10
```
Expected: all PASSED.

- [ ] **Step 8: Commit**

```bash
git add worker/worker/runner.py worker/tests/test_runner.py reva/types.py
git commit -m "feat: add run_follow_up RQ task"
```

---

## Task 8: Webhook Routing

**Files:**
- Modify: `api/app/routes/webhooks.py`
- Test: `api/tests/test_webhooks.py`

- [ ] **Step 1: Add `FakeQueue` and update fixture in `api/tests/test_webhooks.py`**

Add this class at the top of the test file, after the imports:

```python
class FakeQueue:
    def __init__(self):
        self.enqueued: list[tuple[str, dict]] = []

    def enqueue(self, func_name, params=None, **kwargs):
        self.enqueued.append((func_name, params))
```

Update the existing `client_and_db` fixture to also set a fake queue (no change to the yield signature — existing tests are unaffected):

```python
@pytest.fixture()
def client_and_db():
    engine = create_engine_from_url(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = Database(engine)
    settings = Settings(
        database_url="sqlite:///:memory:",
        github_app_id=12345,
        github_webhook_secret=_SECRET,
        github_private_key="fake",
        redis_url="redis://localhost:6379/0",
        debounce_seconds=600,
    )
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.rq_queue = FakeQueue()   # ← add this line
    yield TestClient(app), db
    app.dependency_overrides.clear()
```

Add a second fixture for tests that need to inspect the queue:

```python
@pytest.fixture()
def client_db_queue():
    engine = create_engine_from_url(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = Database(engine)
    settings = Settings(
        database_url="sqlite:///:memory:",
        github_app_id=12345,
        github_webhook_secret=_SECRET,
        github_private_key="fake",
        redis_url="redis://localhost:6379/0",
        debounce_seconds=600,
    )
    fake_queue = FakeQueue()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.rq_queue = fake_queue
    yield TestClient(app), db, fake_queue
    app.dependency_overrides.clear()
```

- [ ] **Step 2: Write failing tests in `api/tests/test_webhooks.py`**

Add these imports to the top of `test_webhooks.py`:

```python
from reva.db.models import ReviewFinding, ReviewRun
```

Add these two tests at the end of the file:

```python
def test_pr_synchronize_with_no_prior_run_uses_full_review_path(client_db_queue):
    client, db, queue = client_db_queue
    # No prior completed run exists — synchronize should create a pending_review.
    _post(client, _pr_payload("synchronize", sha="sha2"), delivery="d1")

    with db.session() as s:
        assert s.query(PendingReview).count() == 1
        pending = s.query(PendingReview).one()
        assert pending.head_sha == "sha2"

    assert all(fn != "worker.runner.run_follow_up" for fn, _ in queue.enqueued)


def test_pr_synchronize_with_prior_run_enqueues_follow_up(client_db_queue):
    client, db, queue = client_db_queue

    # Seed: opened event creates repo + PR.
    _post(client, _pr_payload("opened", sha="sha1"), delivery="d1")

    # Manually seed a completed review_run with an inline finding.
    with db.session() as s:
        repo = s.query(Repository).one()
        pr = s.query(PullRequest).one()
        run = ReviewRun(
            repository_id=repo.id,
            pull_request_id=pr.id,
            head_sha="sha1",
            status="completed",
            trigger_event="opened",
            review_mode="diff",
        )
        s.add(run)
        s.flush()
        finding = ReviewFinding(
            review_run_id=run.id,
            severity="major",
            category="bug",
            title="Bad code",
            body="Needs fix",
            confidence=0.9,
            is_odoo_specific=False,
            github_comment_id=999,
            posted_to_github=True,
        )
        s.add(finding)
        prior_run_id = run.id

    queue.enqueued.clear()

    # Push a new commit.
    _post(client, _pr_payload("synchronize", sha="sha2"), delivery="d2")

    # pending_review row from "opened" should be unchanged (no upsert on follow-up path).
    with db.session() as s:
        pending = s.query(PendingReview).one()
        assert pending.head_sha == "sha1"  # NOT updated to sha2

    # Follow-up was enqueued instead of a full review.
    follow_up_calls = [
        (fn, p) for fn, p in queue.enqueued if fn == "worker.runner.run_follow_up"
    ]
    assert len(follow_up_calls) == 1
    _, params = follow_up_calls[0]
    assert params["head_sha"] == "sha2"
    assert params["prior_run_id"] == prior_run_id
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
cd api && python3.14 -m pytest tests/test_webhooks.py -k "follow_up or prior_run" -v 2>&1 | tail -10
```
Expected: FAILED (route still uses old signature / no routing logic yet)

- [ ] **Step 4: Implement routing in `api/app/routes/webhooks.py`**

Change the `_handle_pull_request` signature to accept `rq_queue`:

```python
def _handle_pull_request(db: Database, payload: dict, settings: Settings, rq_queue) -> None:
```

Update the call site in `receive_webhook` (the `pull_request` branch):

```python
    if x_github_event == "pull_request":
        _handle_pull_request(db, payload, settings, request.app.state.rq_queue)
```

Inside `_handle_pull_request`, add the follow-up check after `pr_id = writers.upsert_pull_request(...)` and before the `scheduled_at` line:

```python
    if action == "synchronize":
        prior = writers.get_latest_run_with_inline_findings(db, pr_id)
        if prior is not None:
            rq_queue.enqueue(
                "worker.runner.run_follow_up",
                {
                    "repository_id": repo_id,
                    "pull_request_id": pr_id,
                    "head_sha": pr_data["head"]["sha"],
                    "installation_id": installation_id,
                    "prior_run_id": prior["run_id"],
                },
            )
            logger.info(
                "follow_up_queued",
                repo=repo_data.get("full_name"),
                pr=pr_data["number"],
                prior_run_id=prior["run_id"],
                sha=pr_data["head"]["sha"][:8],
            )
            return

    scheduled_at = datetime.now(timezone.utc) + timedelta(seconds=settings.debounce_seconds)
    writers.upsert_pending_review(
        ...  # unchanged
    )
```

The complete updated `_handle_pull_request` function body:

```python
def _handle_pull_request(db: Database, payload: dict, settings: Settings, rq_queue) -> None:
    action = payload.get("action", "")
    if action not in _REVIEWABLE_ACTIONS:
        return

    pr_data = payload["pull_request"]
    if pr_data.get("draft", False) and action != "ready_for_review":
        return

    repo_data = payload["repository"]
    installation_id = payload["installation"]["id"]

    repo_id = writers.upsert_repository(
        db,
        github_repository_id=repo_data["id"],
        owner=repo_data["owner"]["login"],
        name=repo_data["name"],
        default_branch=repo_data.get("default_branch", "main"),
        installation_id=installation_id,
    )
    pr_id = writers.upsert_pull_request(
        db,
        repository_id=repo_id,
        github_pr_id=pr_data["id"],
        pr_number=pr_data["number"],
        title=pr_data["title"],
        author_login=(pr_data.get("user") or {}).get("login"),
        base_branch=pr_data["base"]["ref"],
        head_branch=pr_data["head"]["ref"],
        head_sha=pr_data["head"]["sha"],
        state=pr_data["state"],
        draft=pr_data.get("draft", False),
    )

    if action == "synchronize":
        prior = writers.get_latest_run_with_inline_findings(db, pr_id)
        if prior is not None:
            rq_queue.enqueue(
                "worker.runner.run_follow_up",
                {
                    "repository_id": repo_id,
                    "pull_request_id": pr_id,
                    "head_sha": pr_data["head"]["sha"],
                    "installation_id": installation_id,
                    "prior_run_id": prior["run_id"],
                },
            )
            logger.info(
                "follow_up_queued",
                repo=repo_data.get("full_name"),
                pr=pr_data["number"],
                prior_run_id=prior["run_id"],
                sha=pr_data["head"]["sha"][:8],
            )
            return

    scheduled_at = datetime.now(timezone.utc) + timedelta(seconds=settings.debounce_seconds)
    writers.upsert_pending_review(
        db,
        repository_id=repo_id,
        pull_request_id=pr_id,
        pr_number=pr_data["number"],
        head_sha=pr_data["head"]["sha"],
        installation_id=installation_id,
        trigger_event=action,
        review_mode=settings.default_review_mode,
        scheduled_at=scheduled_at,
    )

    logger.info(
        "pending_review_upserted",
        repo=repo_data.get("full_name"),
        pr=pr_data["number"],
        sha=pr_data["head"]["sha"][:8],
        scheduled_in_s=settings.debounce_seconds,
    )
```

- [ ] **Step 5: Run new tests to verify they pass**

```bash
cd api && python3.14 -m pytest tests/test_webhooks.py -k "follow_up or prior_run" -v 2>&1 | tail -10
```
Expected: 2 PASSED.

- [ ] **Step 6: Run full webhook test suite to check for regressions**

```bash
cd api && python3.14 -m pytest tests/test_webhooks.py -v 2>&1 | tail -10
```
Expected: all PASSED.

- [ ] **Step 7: Run all three test suites for final verification**

```bash
cd worker && python3.14 -m pytest tests/ -v 2>&1 | tail -5
cd ../api && python3.14 -m pytest tests/ -v 2>&1 | tail -5
cd ../scheduler && python3.14 -m pytest tests/ -v 2>&1 | tail -5
```
Expected: all PASSED across all three suites.

- [ ] **Step 8: Commit**

```bash
git add api/app/routes/webhooks.py api/tests/test_webhooks.py
git commit -m "feat: route synchronize events to follow-up check when prior inline findings exist"
```
