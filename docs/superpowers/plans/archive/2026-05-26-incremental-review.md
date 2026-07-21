# Incremental PR Review & Comment Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On pushes to a reviewed PR, only review the new commits (delta), verify whether old findings are fixed, and resolve their GitHub threads if so.

**Architecture:** Delta detection in `Reviewer.execute()` checks for a prior completed review and uses `get_compare_diff` instead of the full PR diff. After posting delta findings, `_verify_and_resolve_findings()` in the runner asks Claude to verify each old finding in touched files and calls `resolve_review_thread` (GraphQL) for confirmed fixes.

**Tech Stack:** Python, SQLAlchemy, httpx, Anthropic Messages API, GitHub REST + GraphQL API, RQ

---

## File Map

| File | Change |
|------|--------|
| `reva/diff_utils.py` | Add `extract_file_paths(diff) → set[str]` |
| `reva/db/repo_lookup.py` | Add `get_last_completed_review()` + `DatabaseRepoLookup.get_last_completed_review()` |
| `reva/db/writers.py` | Add `get_open_findings_for_pr()` |
| `reva/github_client.py` | Add `get_compare_diff()`, `get_review_threads()`, `resolve_review_thread()` |
| `reva/types.py` | Add `delta_base_sha: str \| None = None` to `ReviewResult` |
| `reva/finding_verifier.py` | **New** — `StoredFinding` dataclass + `FindingVerifier` class |
| `prompts/skills/reva-delta-review.md` | **New** — skill template for delta reviews |
| `worker/reviewer.py` | Add `get_last_completed_review` to `RepoLookup` protocol; add delta branch in `execute()` |
| `worker/worker/runner.py` | Add `verifier` to `WorkerContext`; add `_verify_and_resolve_findings()`; call it from `_post_result_to_github` |
| `worker/tests/test_diff_utils.py` | Add `extract_file_paths` tests |
| `worker/tests/test_db.py` | Add `get_last_completed_review` + `get_open_findings_for_pr` tests |
| `worker/tests/test_github_client.py` | Add 3 new method tests |
| `worker/tests/test_finding_verifier.py` | **New** — resolved / not-resolved / error cases |
| `worker/tests/test_reviewer.py` | Add delta path tests |
| `worker/tests/test_runner.py` | Add `_verify_and_resolve_findings` tests |

---

## Task 1: `diff_utils.extract_file_paths`

**Files:**
- Modify: `reva/diff_utils.py`
- Test: `worker/tests/test_diff_utils.py`

`iter_diff_files` already yields file paths from `+++ b/<path>` headers. `extract_file_paths` just wraps it into a set.

- [ ] **Write the failing test** — add to `worker/tests/test_diff_utils.py`:

```python
from reva.diff_utils import extract_file_paths

def test_extract_file_paths_returns_set_of_touched_files():
    diff = (
        "diff --git a/custom_addons/a.py b/custom_addons/a.py\n"
        "+++ b/custom_addons/a.py\n"
        "+added\n"
        "diff --git a/custom_addons/b.py b/custom_addons/b.py\n"
        "+++ b/custom_addons/b.py\n"
        "+added\n"
    )
    assert extract_file_paths(diff) == {"custom_addons/a.py", "custom_addons/b.py"}


def test_extract_file_paths_empty_diff_returns_empty_set():
    assert extract_file_paths("") == set()
```

- [ ] **Run tests to verify they fail**

```bash
worker/.venv/bin/pytest worker/tests/test_diff_utils.py::test_extract_file_paths_returns_set_of_touched_files worker/tests/test_diff_utils.py::test_extract_file_paths_empty_diff_returns_empty_set -v
```
Expected: `ImportError: cannot import name 'extract_file_paths'`

- [ ] **Implement** — add to `reva/diff_utils.py` after `iter_diff_files`:

```python
def extract_file_paths(diff: str) -> set[str]:
    """Return the set of file paths touched by the diff."""
    return set(iter_diff_files(diff))
```

- [ ] **Run tests to verify they pass**

```bash
worker/.venv/bin/pytest worker/tests/test_diff_utils.py::test_extract_file_paths_returns_set_of_touched_files worker/tests/test_diff_utils.py::test_extract_file_paths_empty_diff_returns_empty_set -v
```
Expected: 2 passed

- [ ] **Commit**

```bash
git add reva/diff_utils.py worker/tests/test_diff_utils.py
git commit -m "feat: add extract_file_paths to diff_utils"
```

---

## Task 2: DB query functions

**Files:**
- Modify: `reva/db/repo_lookup.py`
- Modify: `reva/db/writers.py`
- Test: `worker/tests/test_db.py`

- [ ] **Write the failing tests** — add to `worker/tests/test_db.py`:

```python
from reva.db.repo_lookup import get_last_completed_review
from reva.db.writers import get_open_findings_for_pr

# --- get_last_completed_review ---

def test_get_last_completed_review_returns_none_when_no_reviews(db_session):
    repo_id, pr_id = _seed_repo_and_pr(db_session)
    assert get_last_completed_review(db_session, pr_id) is None


def test_get_last_completed_review_returns_most_recent_completed(db_session):
    repo_id, pr_id = _seed_repo_and_pr(db_session)
    run_id = _seed_review_run(db_session, pr_id, repo_id, head_sha="aaa", status="completed")
    _seed_review_run(db_session, pr_id, repo_id, head_sha="bbb", status="completed")
    result = get_last_completed_review(db_session, pr_id)
    assert result is not None
    assert result["head_sha"] == "bbb"
    assert "id" in result


def test_get_last_completed_review_ignores_failed_runs(db_session):
    repo_id, pr_id = _seed_repo_and_pr(db_session)
    _seed_review_run(db_session, pr_id, repo_id, head_sha="aaa", status="failed")
    assert get_last_completed_review(db_session, pr_id) is None


# --- get_open_findings_for_pr ---

def test_get_open_findings_for_pr_returns_findings_with_comment_ids(db_session):
    repo_id, pr_id = _seed_repo_and_pr(db_session)
    run_id = _seed_review_run(db_session, pr_id, repo_id, head_sha="abc", status="completed")
    _seed_finding(db_session, run_id, file_path="custom_addons/a.py", github_comment_id=999)
    _seed_finding(db_session, run_id, file_path="custom_addons/b.py", github_comment_id=None)

    findings = get_open_findings_for_pr(db_session, pr_id)
    assert len(findings) == 1
    assert findings[0]["file_path"] == "custom_addons/a.py"
    assert findings[0]["github_comment_id"] == 999


def test_get_open_findings_for_pr_uses_most_recent_run(db_session):
    repo_id, pr_id = _seed_repo_and_pr(db_session)
    old_run = _seed_review_run(db_session, pr_id, repo_id, head_sha="old", status="completed")
    new_run = _seed_review_run(db_session, pr_id, repo_id, head_sha="new", status="completed")
    _seed_finding(db_session, old_run, file_path="custom_addons/old.py", github_comment_id=111)
    _seed_finding(db_session, new_run, file_path="custom_addons/new.py", github_comment_id=222)

    findings = get_open_findings_for_pr(db_session, pr_id)
    assert len(findings) == 1
    assert findings[0]["file_path"] == "custom_addons/new.py"
```

You also need these seed helpers in `test_db.py` (add near the existing seed helpers):

```python
from datetime import datetime, timezone
from reva.db.models import ReviewFinding, ReviewRun

def _seed_review_run(db, pr_id, repo_id, *, head_sha="abc123", status="completed"):
    with db.session() as s:
        run = ReviewRun(
            repository_id=repo_id,
            pull_request_id=pr_id,
            head_sha=head_sha,
            status=status,
            trigger_event="synchronize",
            review_mode="diff",
            completed_at=datetime.now(timezone.utc),
        )
        s.add(run)
        s.flush()
        return run.id


def _seed_finding(db, run_id, *, file_path="custom_addons/foo.py",
                  github_comment_id=None):
    with db.session() as s:
        f = ReviewFinding(
            review_run_id=run_id,
            severity="minor",
            category="bug",
            file_path=file_path,
            line_start=10,
            title="Test finding",
            body="Test body",
            confidence=0.8,
            github_comment_id=github_comment_id,
        )
        s.add(f)
        s.flush()
        return f.id
```

- [ ] **Run tests to verify they fail**

```bash
worker/.venv/bin/pytest worker/tests/test_db.py::test_get_last_completed_review_returns_none_when_no_reviews worker/tests/test_db.py::test_get_open_findings_for_pr_returns_findings_with_comment_ids -v
```
Expected: `ImportError: cannot import name 'get_last_completed_review'`

- [ ] **Implement `get_last_completed_review`** — add to `reva/db/repo_lookup.py`:

```python
from reva.db.models import PullRequest, Repository, ReviewRun

def get_last_completed_review(db: Database, pull_request_id: int) -> dict | None:
    """Return {id, head_sha} of the most recent completed review_run, or None."""
    with db.session() as s:
        row = s.execute(
            select(ReviewRun.id, ReviewRun.head_sha)
            .where(ReviewRun.pull_request_id == pull_request_id)
            .where(ReviewRun.status == "completed")
            .order_by(ReviewRun.completed_at.desc())
            .limit(1)
        ).first()
    if not row:
        return None
    return {"id": row[0], "head_sha": row[1]}
```

- [ ] **Implement `get_open_findings_for_pr`** — add to `reva/db/writers.py` after `get_findings_for_run`:

```python
def get_open_findings_for_pr(db: Database, pull_request_id: int) -> list[dict]:
    """Return findings with a github_comment_id from the most recent completed review."""
    with db.session() as s:
        subq = (
            select(ReviewRun.id)
            .where(ReviewRun.pull_request_id == pull_request_id)
            .where(ReviewRun.status == "completed")
            .order_by(ReviewRun.completed_at.desc())
            .limit(1)
            .scalar_subquery()
        )
        rows = s.execute(
            select(
                ReviewFinding.id,
                ReviewFinding.file_path,
                ReviewFinding.line_start,
                ReviewFinding.title,
                ReviewFinding.body,
                ReviewFinding.severity,
                ReviewFinding.category,
                ReviewFinding.github_comment_id,
            )
            .where(ReviewFinding.review_run_id == subq)
            .where(ReviewFinding.github_comment_id.is_not(None))
        ).all()
    return [
        {
            "id": r[0],
            "file_path": r[1],
            "line_start": r[2],
            "title": r[3],
            "body": r[4],
            "severity": r[5],
            "category": r[6],
            "github_comment_id": r[7],
        }
        for r in rows
    ]
```

Also add the necessary import at the top of `writers.py` if not present: `from reva.db.models import ReviewFinding, ReviewRun` (check existing imports first).

- [ ] **Run tests to verify they pass**

```bash
worker/.venv/bin/pytest worker/tests/test_db.py -k "last_completed_review or open_findings" -v
```
Expected: 5 passed

- [ ] **Commit**

```bash
git add reva/db/repo_lookup.py reva/db/writers.py worker/tests/test_db.py
git commit -m "feat: add get_last_completed_review and get_open_findings_for_pr DB queries"
```

---

## Task 3: `RepoLookup` protocol + `DatabaseRepoLookup`

**Files:**
- Modify: `worker/reviewer.py`
- Modify: `reva/db/repo_lookup.py`
- Test: `worker/tests/test_reviewer.py` (update `FakeRepos`)

- [ ] **Add the method to the protocol** — in `worker/reviewer.py`, update the `RepoLookup` Protocol:

```python
class RepoLookup(Protocol):
    def get_owner_name(self, repository_id: int) -> tuple[str, str]: ...
    def get_pr_basic(self, pull_request_id: int) -> dict: ...

    def get_last_completed_review(self, pull_request_id: int) -> dict | None:
        """Returns {id, head_sha} or None if no completed review exists."""
        ...
```

- [ ] **Add the method to `DatabaseRepoLookup`** — in `reva/db/repo_lookup.py`:

```python
class DatabaseRepoLookup:
    # ... existing methods ...

    def get_last_completed_review(self, pull_request_id: int) -> dict | None:
        return get_last_completed_review(self._db, pull_request_id)
```

- [ ] **Update `FakeRepos` in `worker/tests/test_reviewer.py`** — add the method to the existing `FakeRepos` dataclass:

```python
@dataclass
class FakeRepos:
    owner: str = "acme"
    name: str = "widgets"
    pr: dict = field(...)
    last_completed_review: dict | None = None  # add this field

    def get_owner_name(self, repository_id: int) -> tuple[str, str]:
        return self.owner, self.name

    def get_pr_basic(self, pull_request_id: int) -> dict:
        return self.pr

    def get_last_completed_review(self, pull_request_id: int) -> dict | None:
        return self.last_completed_review
```

- [ ] **Run the full reviewer test suite to confirm nothing broke**

```bash
worker/.venv/bin/pytest worker/tests/test_reviewer.py -v
```
Expected: all existing tests pass

- [ ] **Commit**

```bash
git add worker/reviewer.py reva/db/repo_lookup.py worker/tests/test_reviewer.py
git commit -m "feat: add get_last_completed_review to RepoLookup protocol and DatabaseRepoLookup"
```

---

## Task 4: `GitHubClient` additions

**Files:**
- Modify: `reva/github_client.py`
- Test: `worker/tests/test_github_client.py`

- [ ] **Write the failing tests** — add to `worker/tests/test_github_client.py`:

```python
def test_get_compare_diff_returns_diff_text(rsa_key_pair):
    private_pem, _ = rsa_key_pair

    def handler(request: httpx.Request) -> httpx.Response:
        assert "/compare/abc123...def456" in str(request.url)
        assert "diff" in request.headers.get("accept", "")
        return httpx.Response(200, text="diff --git a/foo.py b/foo.py\n+added")

    client = _make_client(handler, private_pem)
    result = client.get_compare_diff("tok", "acme", "widgets", "abc123", "def456")
    assert result.startswith("diff --git")


def test_get_review_threads_returns_database_id_to_node_id_map(rsa_key_pair):
    private_pem, _ = rsa_key_pair

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/graphql"
        return httpx.Response(200, json={
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [
                                {
                                    "id": "THREAD_NODE_1",
                                    "isResolved": False,
                                    "comments": {"nodes": [{"databaseId": 12345}]},
                                },
                                {
                                    "id": "THREAD_NODE_2",
                                    "isResolved": True,
                                    "comments": {"nodes": [{"databaseId": 99999}]},
                                },
                            ]
                        }
                    }
                }
            }
        })

    client = _make_client(handler, private_pem)
    result = client.get_review_threads("tok", "acme", "widgets", 42)
    # Only unresolved threads returned
    assert result == {12345: "THREAD_NODE_1"}


def test_resolve_review_thread_posts_graphql_mutation(rsa_key_pair):
    private_pem, _ = rsa_key_pair
    called = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/graphql"
        body = request.read()
        called.append(body)
        return httpx.Response(200, json={
            "data": {"resolveReviewThread": {"thread": {"isResolved": True}}}
        })

    client = _make_client(handler, private_pem)
    client.resolve_review_thread("tok", "THREAD_NODE_1")
    assert len(called) == 1
```

- [ ] **Run tests to verify they fail**

```bash
worker/.venv/bin/pytest worker/tests/test_github_client.py::test_get_compare_diff_returns_diff_text worker/tests/test_github_client.py::test_get_review_threads_returns_database_id_to_node_id_map worker/tests/test_github_client.py::test_resolve_review_thread_posts_graphql_mutation -v
```
Expected: `AttributeError: 'GitHubClient' object has no attribute 'get_compare_diff'`

- [ ] **Implement** — add to `reva/github_client.py` in the Reads section:

```python
def get_compare_diff(
    self, token: str, owner: str, repo: str, base_sha: str, head_sha: str
) -> str:
    """Return the unified diff between two SHAs."""
    response = self._get(
        token,
        f"/repos/{owner}/{repo}/compare/{base_sha}...{head_sha}",
        extra_headers={"Accept": "application/vnd.github.v3.diff"},
    )
    return response.text
```

Add to the Writes section:

```python
def get_review_threads(
    self, token: str, owner: str, repo: str, pr_number: int
) -> dict[int, str]:
    """Return {github_comment_database_id → thread_node_id} for unresolved threads."""
    query = """
    query GetPRThreads($owner: String!, $repo: String!, $prNumber: Int!) {
      repository(owner: $owner, name: $repo) {
        pullRequest(number: $prNumber) {
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
    response = self._post(
        token,
        "/graphql",
        {"query": query, "variables": {"owner": owner, "repo": repo, "prNumber": pr_number}},
    )
    threads = (
        response.json()
        .get("data", {})
        .get("repository", {})
        .get("pullRequest", {})
        .get("reviewThreads", {})
        .get("nodes", [])
    )
    return {
        node["comments"]["nodes"][0]["databaseId"]: node["id"]
        for node in threads
        if not node.get("isResolved") and node.get("comments", {}).get("nodes")
    }

def resolve_review_thread(self, token: str, thread_node_id: str) -> None:
    """Resolve a pull request review thread via GraphQL."""
    mutation = """
    mutation ResolveThread($threadId: ID!) {
      resolveReviewThread(input: {threadId: $threadId}) {
        thread { isResolved }
      }
    }
    """
    self._post(token, "/graphql", {"query": mutation, "variables": {"threadId": thread_node_id}})
```

- [ ] **Run tests to verify they pass**

```bash
worker/.venv/bin/pytest worker/tests/test_github_client.py::test_get_compare_diff_returns_diff_text worker/tests/test_github_client.py::test_get_review_threads_returns_database_id_to_node_id_map worker/tests/test_github_client.py::test_resolve_review_thread_posts_graphql_mutation -v
```
Expected: 3 passed

- [ ] **Commit**

```bash
git add reva/github_client.py worker/tests/test_github_client.py
git commit -m "feat: add get_compare_diff, get_review_threads, resolve_review_thread to GitHubClient"
```

---

## Task 5: `ReviewResult.delta_base_sha`

**Files:**
- Modify: `reva/types.py`

No test needed — this is a field addition with a default. The reviewer tests in Task 8 will exercise it.

- [ ] **Add the field** — in `reva/types.py`, add to `ReviewResult` after `error_class`:

```python
class ReviewResult(BaseModel):
    # ... existing fields ...
    error_class: Literal["transient", "permanent"] | None = None
    delta_base_sha: str | None = None   # set when this was a delta review
```

- [ ] **Run full test suite to confirm nothing broke**

```bash
worker/.venv/bin/pytest worker/tests/ -q
```
Expected: all pass

- [ ] **Commit**

```bash
git add reva/types.py
git commit -m "feat: add delta_base_sha field to ReviewResult"
```

---

## Task 6: `FindingVerifier`

**Files:**
- Create: `reva/finding_verifier.py`
- Create: `worker/tests/test_finding_verifier.py`

- [ ] **Write the failing tests** — create `worker/tests/test_finding_verifier.py`:

```python
"""Tests for FindingVerifier.is_resolved."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from reva.finding_verifier import FindingVerifier, StoredFinding
from reva.types import ClaudeResponse


def _make_verifier(resolved: bool = True, raise_exc: Exception | None = None) -> FindingVerifier:
    claude = MagicMock()
    if raise_exc:
        claude.review.side_effect = raise_exc
    else:
        claude.review.return_value = ClaudeResponse(
            model="claude-sonnet-4-6",
            stop_reason="tool_use",
            tool_use_input={"resolved": resolved, "reason": "test reason"},
        )
    return FindingVerifier(claude)


def _finding() -> StoredFinding:
    return StoredFinding(
        file_path="custom_addons/foo.py",
        line_start=42,
        title="Missing null check",
        body="The `user` variable may be None here.",
        severity="major",
        category="bug",
    )


def test_is_resolved_returns_true_when_claude_says_resolved():
    verifier = _make_verifier(resolved=True)
    assert verifier.is_resolved(_finding(), "def foo():\n    pass\n") is True


def test_is_resolved_returns_false_when_claude_says_not_resolved():
    verifier = _make_verifier(resolved=False)
    assert verifier.is_resolved(_finding(), "def foo():\n    x = user.name\n") is False


def test_is_resolved_raises_on_api_error():
    from reva.errors import TransientError
    verifier = _make_verifier(raise_exc=TransientError("rate limited"))
    with pytest.raises(TransientError):
        verifier.is_resolved(_finding(), "content")
```

- [ ] **Run tests to verify they fail**

```bash
worker/.venv/bin/pytest worker/tests/test_finding_verifier.py -v
```
Expected: `ModuleNotFoundError: No module named 'reva.finding_verifier'`

- [ ] **Implement** — create `reva/finding_verifier.py`:

```python
"""Claude-based verification that a prior finding is still present in the current code."""

from __future__ import annotations

from dataclasses import dataclass

from reva.claude_client import ClaudeClient
from reva.errors import PermanentError
from reva.types import ContentBlock


@dataclass
class StoredFinding:
    file_path: str
    line_start: int | None
    title: str
    body: str
    severity: str
    category: str


_SYSTEM_PROMPT = """\
You are a code reviewer checking whether a previously reported issue has been fixed.
You will be given a finding from a prior code review and the current content of the file.
Determine whether the issue described in the finding is still present at or near the original location.
Be conservative: only mark resolved if you are confident the issue no longer exists.
If the file has been significantly restructured and you cannot locate the original code, mark it as unresolved.\
"""

_VERIFY_TOOL = {
    "name": "verify_finding",
    "description": "Report whether the finding is still present in the current file.",
    "input_schema": {
        "type": "object",
        "properties": {
            "resolved": {
                "type": "boolean",
                "description": "True if the issue is fixed, False if still present.",
            },
            "reason": {
                "type": "string",
                "description": "One sentence explaining the decision.",
            },
        },
        "required": ["resolved", "reason"],
    },
}

_TOOL_CHOICE = {"type": "tool", "name": "verify_finding"}


class FindingVerifier:
    def __init__(self, claude: ClaudeClient) -> None:
        self._claude = claude

    def is_resolved(self, finding: StoredFinding, file_content: str) -> bool:
        """Return True if the finding is no longer present in file_content.

        Raises TransientError / PermanentError on API failure (caller catches).
        """
        line_info = f" line {finding.line_start}" if finding.line_start else ""
        user_prompt = (
            f"## Finding\n"
            f"**Title:** {finding.title}\n"
            f"**Severity:** {finding.severity}\n"
            f"**Category:** {finding.category}\n"
            f"**Original location:** {finding.file_path}{line_info}\n"
            f"**Description:** {finding.body}\n\n"
            f"## Current file content\n```\n{file_content}\n```\n\n"
            f"Is this issue still present in the current file?"
        )
        system_blocks: list[ContentBlock] = [{"type": "text", "text": _SYSTEM_PROMPT}]
        response = self._claude.review(
            system_blocks=system_blocks,
            user_prompt=user_prompt,
            tools=[_VERIFY_TOOL],
            tool_choice=_TOOL_CHOICE,
            max_tokens=512,
        )
        if response.tool_use_input is None:
            raise PermanentError("FindingVerifier: Claude did not call verify_finding")
        return bool(response.tool_use_input.get("resolved", False))
```

- [ ] **Run tests to verify they pass**

```bash
worker/.venv/bin/pytest worker/tests/test_finding_verifier.py -v
```
Expected: 3 passed

- [ ] **Commit**

```bash
git add reva/finding_verifier.py worker/tests/test_finding_verifier.py
git commit -m "feat: add FindingVerifier for checking whether prior findings are resolved"
```

---

## Task 7: `reva-delta-review.md` skill

**Files:**
- Create: `prompts/skills/reva-delta-review.md`

Read `prompts/skills/reva-diff-review.md` first to understand the structure, then create the delta variant.

- [ ] **Create `prompts/skills/reva-delta-review.md`** — copy the full content of `reva-diff-review.md` and replace the opening section ("Your task") with:

```markdown
## Your task

Review the **incremental changes** described in the Task Parameters below. The diff represents only the commits added to this pull request **since the last review** — not the full PR diff.

Your goal is to find genuine bugs, security vulnerabilities, performance problems, and maintainability issues introduced by these new commits.

**Important constraints:**
- Report only findings visible within the changed lines in the diff.
- Do not report issues that exist in unchanged parts of the file.
- Do not re-report issues that may have been flagged in earlier reviews of this PR.
- Use the Read tool to examine the complete context of changed functions, but only flag issues traceable to the new changes.
```

Keep the rest of the file (review process, output format, severity guide, etc.) identical to `reva-diff-review.md`.

- [ ] **Commit**

```bash
git add prompts/skills/reva-delta-review.md
git commit -m "feat: add reva-delta-review skill template for incremental PR reviews"
```

---

## Task 8: Delta detection in `Reviewer.execute()`

**Files:**
- Modify: `worker/reviewer.py`
- Test: `worker/tests/test_reviewer.py`

- [ ] **Write the failing tests** — add to `worker/tests/test_reviewer.py`:

```python
def test_delta_review_used_when_prior_review_exists():
    """When a completed review exists, get_compare_diff is called and reva-delta-review skill used."""
    github = FakeGitHub()
    github.compare_diff = _DEFAULT_DIFF  # diff returned by compare endpoint

    repos = FakeRepos(last_completed_review={"id": 1, "head_sha": "prevsha"})
    runner = FakeRunner(response=_claude_response_with_findings([]))
    reviewer = _make_reviewer(github=github, repos=repos, runner=runner)
    params = JobParams(
        repository_id=1, pull_request_id=1, head_sha="newsha",
        installation_id=99, trigger_event="synchronize",
    )

    result = reviewer.execute(params)

    assert result.status == "completed"
    assert result.delta_base_sha == "prevsha"
    assert runner.last_skill == "reva-delta-review"
    assert github.compare_diff_calls == 1


def test_full_review_used_when_no_prior_review():
    """Without a prior review, get_pull_request_diff is called and reva-diff-review skill used."""
    github = FakeGitHub()
    repos = FakeRepos(last_completed_review=None)
    runner = FakeRunner(response=_claude_response_with_findings([]))
    reviewer = _make_reviewer(github=github, repos=repos, runner=runner)
    params = JobParams(
        repository_id=1, pull_request_id=1, head_sha="sha1",
        installation_id=99, trigger_event="synchronize",
    )

    result = reviewer.execute(params)

    assert result.status == "completed"
    assert result.delta_base_sha is None
    assert runner.last_skill in ("reva-diff-review", "reva-full-review")
    assert github.diff_calls == 1


def test_delta_empty_returns_stale():
    """If the compare diff is empty, return stale without calling Claude."""
    github = FakeGitHub()
    github.compare_diff = ""  # empty delta

    repos = FakeRepos(last_completed_review={"id": 1, "head_sha": "prevsha"})
    runner = FakeRunner(response=_claude_response_with_findings([]))
    reviewer = _make_reviewer(github=github, repos=repos, runner=runner)
    params = JobParams(
        repository_id=1, pull_request_id=1, head_sha="newsha",
        installation_id=99, trigger_event="synchronize",
    )

    result = reviewer.execute(params)

    assert result.status == "stale"
    assert runner.last_skill is None  # Claude never called
```

You also need to update `FakeGitHub` to support `get_compare_diff`:

```python
@dataclass
class FakeGitHub:
    diff: str = _DEFAULT_DIFF
    compare_diff: str = _DEFAULT_DIFF  # add this
    files: list[dict] = field(default_factory=lambda: [{"filename": "custom_addons/app.py"}])
    head_sha: str = "deadbeef"
    file_contents: dict[str, str | None] = field(default_factory=dict)
    diff_calls: int = 0
    token_calls: int = 0
    compare_diff_calls: int = 0  # add this

    # ... existing methods ...

    def get_compare_diff(self, token, owner, repo, base_sha, head_sha) -> str:
        self.compare_diff_calls += 1
        return self.compare_diff
```

- [ ] **Run tests to verify they fail**

```bash
worker/.venv/bin/pytest worker/tests/test_reviewer.py::test_delta_review_used_when_prior_review_exists worker/tests/test_reviewer.py::test_full_review_used_when_no_prior_review worker/tests/test_reviewer.py::test_delta_empty_returns_stale -v
```
Expected: FAIL — `FakeGitHub has no attribute get_compare_diff` or assertion errors

- [ ] **Implement the delta branch** — in `worker/reviewer.py`, inside `execute()` after the stale-SHA check (step 5, before fetching the diff), add:

```python
# 4b. Delta detection: if a prior completed review exists, use the compare diff.
last_review = self.repos.get_last_completed_review(params.pull_request_id)
if last_review:
    raw_diff = self.github.get_compare_diff(
        token, owner, name, last_review["head_sha"], params.head_sha
    )
    diff = filter_diff(raw_diff)
    if not diff.strip():
        return ReviewResult(
            status="stale",
            summary="No reviewable changes since last review.",
            risk_level="low",
        )
    skill = "reva-delta-review"
    delta_base_sha: str | None = last_review["head_sha"]
else:
    raw_diff = self.github.get_pull_request_diff(token, owner, name, pr_number)
    diff = filter_diff(raw_diff)
    skill = "reva-full-review" if params.review_mode == "full" else "reva-diff-review"
    delta_base_sha = None
```

Remove the existing `raw_diff = self.github.get_pull_request_diff(...)` and `diff = filter_diff(raw_diff)` lines that follow immediately after the stale check (they are replaced by the block above).

At step 10 in `execute()`, replace `skill = "reva-full-review" if ...` with just `skill` (already set above).

At the end, in the `ReviewResult(...)` construction, add:

```python
return ReviewResult(
    # ... existing fields ...
    delta_base_sha=delta_base_sha,
)
```

Also add `get_compare_diff` to the `GitHubReader` protocol:

```python
class GitHubReader(Protocol):
    def get_installation_token(self, installation_id: int) -> str: ...
    def get_pull_request(self, token: str, owner: str, repo: str, pr_number: int) -> dict: ...
    def get_pull_request_diff(self, token: str, owner: str, repo: str, pr_number: int) -> str: ...
    def get_compare_diff(self, token: str, owner: str, repo: str, base_sha: str, head_sha: str) -> str: ...
    def get_changed_files(self, token: str, owner: str, repo: str, pr_number: int) -> list[dict]: ...
    def get_file_content(self, token: str, owner: str, repo: str, path: str, ref: str) -> str | None: ...
```

- [ ] **Run tests to verify they pass**

```bash
worker/.venv/bin/pytest worker/tests/test_reviewer.py -v
```
Expected: all pass including the 3 new tests

- [ ] **Commit**

```bash
git add worker/reviewer.py worker/tests/test_reviewer.py
git commit -m "feat: add delta review detection to Reviewer.execute()"
```

---

## Task 9: `_verify_and_resolve_findings` in runner

**Files:**
- Modify: `worker/worker/runner.py`
- Test: `worker/tests/test_runner.py`

- [ ] **Write the failing tests** — add to `worker/tests/test_runner.py`:

```python
from unittest.mock import MagicMock, patch
from reva.finding_verifier import FindingVerifier, StoredFinding

def test_verify_and_resolve_calls_resolve_for_fixed_finding():
    """When Claude says resolved and thread exists, resolve_review_thread is called."""
    from worker.worker.runner import _verify_and_resolve_findings

    # Build a minimal WorkerContext-like object
    ctx = MagicMock()
    ctx.github.get_review_threads.return_value = {12345: "THREAD_NODE_1"}
    ctx.github.get_file_content.return_value = "def foo(): pass"
    ctx.verifier.is_resolved.return_value = True

    params = MagicMock()
    params.pull_request_id = 1
    params.head_sha = "newsha"

    result = MagicMock()
    result.diff = (
        "diff --git a/custom_addons/foo.py b/custom_addons/foo.py\n"
        "+++ b/custom_addons/foo.py\n+fixed\n"
    )

    with patch("worker.worker.runner.writers") as mock_writers:
        mock_writers.get_open_findings_for_pr.return_value = [{
            "id": 1,
            "file_path": "custom_addons/foo.py",
            "line_start": 10,
            "title": "Missing null check",
            "body": "user may be None",
            "severity": "major",
            "category": "bug",
            "github_comment_id": 12345,
        }]
        _verify_and_resolve_findings(ctx, params, result, "tok", "acme", "widgets", 42)

    ctx.github.resolve_review_thread.assert_called_once_with("tok", "THREAD_NODE_1")


def test_verify_and_resolve_skips_unfixed_finding():
    from worker.worker.runner import _verify_and_resolve_findings

    ctx = MagicMock()
    ctx.github.get_review_threads.return_value = {12345: "THREAD_NODE_1"}
    ctx.github.get_file_content.return_value = "def foo(): x = user.name"
    ctx.verifier.is_resolved.return_value = False

    params = MagicMock()
    params.pull_request_id = 1
    params.head_sha = "newsha"

    result = MagicMock()
    result.diff = (
        "diff --git a/custom_addons/foo.py b/custom_addons/foo.py\n"
        "+++ b/custom_addons/foo.py\n+changed\n"
    )

    with patch("worker.worker.runner.writers") as mock_writers:
        mock_writers.get_open_findings_for_pr.return_value = [{
            "id": 1,
            "file_path": "custom_addons/foo.py",
            "line_start": 10,
            "title": "Missing null check",
            "body": "user may be None",
            "severity": "major",
            "category": "bug",
            "github_comment_id": 12345,
        }]
        _verify_and_resolve_findings(ctx, params, result, "tok", "acme", "widgets", 42)

    ctx.github.resolve_review_thread.assert_not_called()


def test_verify_and_resolve_swallows_verification_error():
    from worker.worker.runner import _verify_and_resolve_findings
    from reva.errors import TransientError

    ctx = MagicMock()
    ctx.github.get_review_threads.return_value = {12345: "THREAD_NODE_1"}
    ctx.github.get_file_content.return_value = "content"
    ctx.verifier.is_resolved.side_effect = TransientError("rate limited")

    params = MagicMock()
    params.pull_request_id = 1
    params.head_sha = "newsha"

    result = MagicMock()
    result.diff = (
        "diff --git a/custom_addons/foo.py b/custom_addons/foo.py\n"
        "+++ b/custom_addons/foo.py\n+changed\n"
    )

    with patch("worker.worker.runner.writers") as mock_writers:
        mock_writers.get_open_findings_for_pr.return_value = [{
            "id": 1,
            "file_path": "custom_addons/foo.py",
            "line_start": 10,
            "title": "t",
            "body": "b",
            "severity": "minor",
            "category": "bug",
            "github_comment_id": 12345,
        }]
        # Must not raise
        _verify_and_resolve_findings(ctx, params, result, "tok", "acme", "widgets", 42)
```

- [ ] **Run tests to verify they fail**

```bash
worker/.venv/bin/pytest worker/tests/test_runner.py::test_verify_and_resolve_calls_resolve_for_fixed_finding worker/tests/test_runner.py::test_verify_and_resolve_skips_unfixed_finding worker/tests/test_runner.py::test_verify_and_resolve_swallows_verification_error -v
```
Expected: `ImportError: cannot import name '_verify_and_resolve_findings'`

- [ ] **Implement** — add to `worker/worker/runner.py`:

First, add the import at the top:
```python
from reva.finding_verifier import FindingVerifier, StoredFinding
from reva.diff_utils import parse_diff_hunks, extract_file_paths
```

Add `verifier: FindingVerifier` to `WorkerContext`:
```python
@dataclass(frozen=True)
class WorkerContext:
    db: Database
    claude: ClaudeClient
    runner: ClaudeCodeRunner
    github: GitHubClient
    reviewer: Reviewer
    auditor: Auditor
    ticket_analyzer: TicketAnalyzer
    odoo: OdooCallbackClient
    verifier: FindingVerifier
    google_chat_webhook_url: str = ""
```

Add the helper function (place it after `_backfill_comment_ids`):

```python
def _verify_and_resolve_findings(
    ctx: WorkerContext,
    params: JobParams,
    result: ReviewResult,
    token: str,
    owner: str,
    name: str,
    pr_number: int,
) -> None:
    """Best-effort: for old findings in touched files, verify if fixed and resolve threads."""
    try:
        threads = ctx.github.get_review_threads(token, owner, name, pr_number)
    except Exception:
        logger.warning("get_review_threads_failed", exc_info=True)
        return

    old_findings = writers.get_open_findings_for_pr(ctx.db, params.pull_request_id)
    touched_files = extract_file_paths(result.diff)

    for f in old_findings:
        if f["file_path"] not in touched_files:
            continue
        try:
            content = ctx.github.get_file_content(
                token, owner, name, f["file_path"], params.head_sha
            )
            if content is None:
                continue
            stored = StoredFinding(
                file_path=f["file_path"],
                line_start=f["line_start"],
                title=f["title"],
                body=f["body"],
                severity=f["severity"],
                category=f["category"],
            )
            if ctx.verifier.is_resolved(stored, content):
                thread_id = threads.get(f["github_comment_id"])
                if thread_id:
                    ctx.github.resolve_review_thread(token, thread_id)
                    logger.info(
                        "finding_resolved",
                        finding_id=f["id"],
                        file=f["file_path"],
                    )
        except Exception:
            logger.warning("finding_verification_failed", finding_id=f["id"], exc_info=True)
```

In `_post_result_to_github`, add the call after `_backfill_comment_ids`:

```python
if result.status == "completed":
    check_run_id, review_id = _post_completed(
        ctx, params, result, run_id, token, owner, name, pr_number
    )
    writers.attach_github_ids(ctx.db, run_id, check_run_id=check_run_id, review_id=review_id)
    _backfill_comment_ids(ctx, run_id, token, owner, name, pr_number, review_id)
    if result.delta_base_sha:
        _verify_and_resolve_findings(ctx, params, result, token, owner, name, pr_number)
```

- [ ] **Run tests to verify they pass**

```bash
worker/.venv/bin/pytest worker/tests/test_runner.py::test_verify_and_resolve_calls_resolve_for_fixed_finding worker/tests/test_runner.py::test_verify_and_resolve_skips_unfixed_finding worker/tests/test_runner.py::test_verify_and_resolve_swallows_verification_error -v
```
Expected: 3 passed

- [ ] **Commit**

```bash
git add worker/worker/runner.py worker/tests/test_runner.py
git commit -m "feat: add _verify_and_resolve_findings and hook into delta review post flow"
```

---

## Task 10: Wire `FindingVerifier` into `WorkerContext`

**Files:**
- Modify: `worker/worker/runner.py` (`build_worker_context`)

`WorkerContext` now requires a `verifier` field. The `build_worker_context` function that constructs the singleton needs to create one.

- [ ] **Update `build_worker_context`** — find the function in `worker/worker/runner.py` and add:

```python
from reva.finding_verifier import FindingVerifier

# Inside build_worker_context, after claude is constructed:
verifier = FindingVerifier(claude=claude)

# In the WorkerContext(...) call, add:
verifier=verifier,
```

- [ ] **Run the full worker test suite**

```bash
worker/.venv/bin/pytest worker/tests/ -q
```
Expected: all pass

- [ ] **Commit**

```bash
git add worker/worker/runner.py
git commit -m "feat: wire FindingVerifier into WorkerContext"
```

---

## Final Verification

- [ ] **Run the complete worker test suite**

```bash
worker/.venv/bin/pytest worker/tests/ -v
```
Expected: all pass, no warnings

- [ ] **Rebuild and deploy**

```bash
docker compose build worker && docker compose up -d worker
```
