# Headless Claude — Gap Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the four production gaps in the headless Claude Code integration: pass `custom_instructions` through to Claude, fix the PR comment command mapping, add `--dangerously-skip-permissions` so the CLI runs non-interactively in Docker, and add a repo-cache prune job.

**Architecture:** All four fixes are surgical — one or two files each, no new abstractions. The prune job follows the existing `run_weekly_report` pattern: a plain RQ task function in `runner.py` exposed via an admin API endpoint. The other three fixes are one-liners or small additions to existing files.

**Tech Stack:** Python 3.12, FastAPI, RQ, pytest, Claude Code CLI subprocess

---

## File Map

**Modified files:**
- `worker/worker/reviewer.py` — add `custom_instructions` to `skill_params`
- `prompts/skills/reva-diff-review.md` — acknowledge `custom_instructions` parameter
- `prompts/skills/reva-full-review.md` — acknowledge `custom_instructions` parameter
- `worker/tests/test_reviewer.py` — test that `custom_instructions` reaches `runner.last_params`
- `api/app/routes/webhooks.py` — add `/full-review`→`full`, change `/deep-review`→`deep`
- `api/tests/test_webhooks.py` — add tests for `/full-review` and `/deep-review`
- `reva/claude_code_runner.py` — add `--dangerously-skip-permissions`; add `ttl_days` param; add `prune_old_repos()`
- `worker/worker/runner.py` — pass `ttl_days` to runner; add `run_prune_repo_cache()`
- `api/app/routes/v1/admin.py` — add `POST /admin/prune-repo-cache`
- `worker/tests/test_claude_code_runner.py` — assert `--dangerously-skip-permissions` present; add prune tests

---

## Task 1: Pass `custom_instructions` to Claude

**Files:**
- Modify: `worker/worker/reviewer.py` (around line 206)
- Modify: `prompts/skills/reva-diff-review.md`
- Modify: `prompts/skills/reva-full-review.md`
- Test: `worker/tests/test_reviewer.py`

- [ ] **Step 1: Write failing test**

In `worker/tests/test_reviewer.py`, add after `test_custom_instructions_appended_as_block`:

```python
def test_custom_instructions_passed_to_skill_params():
    github = FakeGitHub(
        file_contents={
            ".claude-review.yml": (
                "custom_instructions: |\n"
                "  Be strict about currency_id validation.\n"
            )
        }
    )
    runner = FakeRunner(response=_claude_response_with_findings([]))
    reviewer, _, _, runner_out, _ = _make_reviewer(github=github, runner=runner)
    reviewer.execute(_params())
    assert runner_out.last_params is not None
    assert "currency_id" in runner_out.last_params.get("custom_instructions", "")
```

- [ ] **Step 2: Run test — expect failure**

```bash
cd worker && .venv/bin/pytest tests/test_reviewer.py::test_custom_instructions_passed_to_skill_params -v
```

Expected: `FAILED` — `AssertionError` because `custom_instructions` key is missing from `last_params`.

- [ ] **Step 3: Add `custom_instructions` to `skill_params` in `reviewer.py`**

In `worker/worker/reviewer.py`, find the `skill_params` dict (around line 206). Change:

```python
        skill_params = {
            "pr_title": pr_basic.get("title", ""),
            "pr_body": pr_basic.get("body") or pr_detail.get("body") or "",
            "diff": diff,
            "changed_files": "\n".join(f"- {f}" for f in changed_files),
            "base_branch": pr_basic["base_branch"],
            "head_branch": pr_basic["head_branch"],
        }
```

To:

```python
        skill_params = {
            "pr_title": pr_basic.get("title", ""),
            "pr_body": pr_basic.get("body") or pr_detail.get("body") or "",
            "diff": diff,
            "changed_files": "\n".join(f"- {f}" for f in changed_files),
            "base_branch": pr_basic["base_branch"],
            "head_branch": pr_basic["head_branch"],
            "custom_instructions": repo_config.custom_instructions or "",
        }
```

- [ ] **Step 4: Run test — expect pass**

```bash
cd worker && .venv/bin/pytest tests/test_reviewer.py::test_custom_instructions_passed_to_skill_params -v
```

Expected: `PASSED`.

- [ ] **Step 5: Update `prompts/skills/reva-diff-review.md`**

Append this section at the end of the file (after the Rules list):

```markdown

If the `custom_instructions` parameter is non-empty, treat it as additional
repo-specific review guidance that overrides or supplements the standard process
above. Apply it when deciding what to flag and how to prioritize findings.
```

- [ ] **Step 6: Update `prompts/skills/reva-full-review.md`**

Same addition at the end of the file:

```markdown

If the `custom_instructions` parameter is non-empty, treat it as additional
repo-specific review guidance that overrides or supplements the standard process
above. Apply it when deciding what to flag and how to prioritize findings.
```

- [ ] **Step 7: Run full test suite**

```bash
cd worker && .venv/bin/pytest tests/ -q
```

Expected: all tests pass (160 now, +1 new).

- [ ] **Step 8: Commit**

```bash
git add worker/worker/reviewer.py prompts/skills/reva-diff-review.md prompts/skills/reva-full-review.md worker/tests/test_reviewer.py
git commit -m "feat: pass custom_instructions from .claude-review.yml to Claude Code skill"
```

---

## Task 2: Fix PR comment command mapping

**Files:**
- Modify: `api/app/routes/webhooks.py` (line 186–189)
- Test: `api/tests/test_webhooks.py`

- [ ] **Step 1: Write failing tests**

In `api/tests/test_webhooks.py`, add a `_comment_payload` helper and two new tests after the existing PR tests.

First, add the helper (at module level, near `_pr_payload`):

```python
def _comment_payload(command: str, pr_number: int = 42) -> dict:
    """Build an issue_comment webhook payload for a PR comment with `command`."""
    return {
        "action": "created",
        "installation": {"id": 99},
        "repository": {
            "id": 1001,
            "name": "widgets",
            "full_name": "acme/widgets",
            "default_branch": "main",
            "owner": {"login": "acme"},
        },
        "issue": {
            "number": pr_number,
            "pull_request": {"url": "https://api.github.com/repos/acme/widgets/pulls/42"},
        },
        "comment": {"body": command},
        "sender": {"login": "alice"},
    }
```

Then add these two tests:

```python
def test_full_review_comment_queues_full_mode(client_and_db):
    client, db = client_and_db
    # Create the PR in the DB first via a PR opened event.
    _post(client, _pr_payload("opened", sha="abc123"))
    # Now post the /full-review comment.
    resp = _post(client, _comment_payload("/full-review"), event="issue_comment", delivery="d2")
    assert resp.status_code == 202
    with db.session() as s:
        pending = s.query(PendingReview).one()
        assert pending.review_mode == "full"


def test_deep_review_comment_queues_deep_mode(client_and_db):
    client, db = client_and_db
    _post(client, _pr_payload("opened", sha="abc123"))
    resp = _post(client, _comment_payload("/deep-review"), event="issue_comment", delivery="d2")
    assert resp.status_code == 202
    with db.session() as s:
        pending = s.query(PendingReview).one()
        assert pending.review_mode == "deep"
```

- [ ] **Step 2: Run tests — expect failure**

```bash
cd /path/to/project && python -m pytest api/tests/test_webhooks.py::test_full_review_comment_queues_full_mode api/tests/test_webhooks.py::test_deep_review_comment_queues_deep_mode -v
```

Run from the project root where `api/` lives. Expected: both `FAILED` — the comment handler returns early (no PR row) or maps to wrong mode.

To find the right test runner:
```bash
ls api/  # look for pytest.ini or pyproject.toml
```

Run as:
```bash
cd api && ../.venv/bin/pytest tests/test_webhooks.py::test_full_review_comment_queues_full_mode -v 2>&1 | head -20
```

If there's no `api/.venv`, run with the worker venv: `cd worker && .venv/bin/pytest ../api/tests/test_webhooks.py -v`

- [ ] **Step 3: Fix the command mapping in `webhooks.py`**

In `api/app/routes/webhooks.py`, change:

```python
_COMMENT_COMMANDS: dict[str, str] = {
    "/review": "diff",
    "/deep-review": "full",
}
```

To:

```python
_COMMENT_COMMANDS: dict[str, str] = {
    "/review": "diff",
    "/full-review": "full",
    "/deep-review": "deep",
}
```

- [ ] **Step 4: Run the new tests — expect pass**

```bash
cd worker && .venv/bin/pytest ../api/tests/test_webhooks.py::test_full_review_comment_queues_full_mode ../api/tests/test_webhooks.py::test_deep_review_comment_queues_deep_mode -v
```

Expected: both `PASSED`.

- [ ] **Step 5: Run all webhook tests**

```bash
cd worker && .venv/bin/pytest ../api/tests/ -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add api/app/routes/webhooks.py api/tests/test_webhooks.py
git commit -m "fix: /full-review triggers full mode, /deep-review triggers deep (Opus) mode"
```

---

## Task 3: Add `--dangerously-skip-permissions` for non-interactive CLI

**Files:**
- Modify: `reva/claude_code_runner.py` (the subprocess args list, around line 104)
- Test: `worker/tests/test_claude_code_runner.py`

- [ ] **Step 1: Write failing test**

In `worker/tests/test_claude_code_runner.py`, add after the existing `review` tests:

```python
def test_review_includes_dangerously_skip_permissions(runner_with_skill, tmp_path):
    repo_path = str(tmp_path / "repo")
    os.makedirs(repo_path)

    def fake_run(args, **kwargs):
        task_str = args[-1]
        output_path = [
            line.split(": ", 1)[1]
            for line in task_str.splitlines()
            if line.startswith("output_path:")
        ][0]
        with open(output_path, "w") as f:
            json.dump({"summary": "ok", "findings": []}, f)
        return _ok()

    with patch("subprocess.run", side_effect=fake_run) as mock_run:
        runner_with_skill.review(repo_path=repo_path, skill="reva-diff-review", params={})

    call_args = mock_run.call_args.args[0]
    assert "--dangerously-skip-permissions" in call_args
```

- [ ] **Step 2: Run test — expect failure**

```bash
cd worker && .venv/bin/pytest tests/test_claude_code_runner.py::test_review_includes_dangerously_skip_permissions -v
```

Expected: `FAILED` — `AssertionError` because the flag is not present.

- [ ] **Step 3: Add the flag to the subprocess call in `claude_code_runner.py`**

In `reva/claude_code_runner.py`, find the `subprocess.run` call (around line 103). Change:

```python
            proc = subprocess.run(
                [
                    _CLAUDE_BIN, "--print",
                    "--output-format", "json",
                    "--model", model or self.default_model,
                    "--allowedTools", "Read,Bash,Grep,Write",
                    task,
                ],
```

To:

```python
            proc = subprocess.run(
                [
                    _CLAUDE_BIN, "--print",
                    "--dangerously-skip-permissions",
                    "--output-format", "json",
                    "--model", model or self.default_model,
                    "--allowedTools", "Read,Bash,Grep,Write",
                    task,
                ],
```

- [ ] **Step 4: Run test — expect pass**

```bash
cd worker && .venv/bin/pytest tests/test_claude_code_runner.py::test_review_includes_dangerously_skip_permissions -v
```

Expected: `PASSED`.

- [ ] **Step 5: Run full test suite**

```bash
cd worker && .venv/bin/pytest tests/ -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add reva/claude_code_runner.py worker/tests/test_claude_code_runner.py
git commit -m "fix: add --dangerously-skip-permissions so Claude Code CLI runs non-interactively in Docker"
```

---

## Task 4: Repo cache prune job

**Files:**
- Modify: `reva/claude_code_runner.py` — add `ttl_days` to `__init__`; add `prune_old_repos()`
- Modify: `worker/worker/runner.py` — pass `ttl_days`; add `run_prune_repo_cache()`
- Modify: `api/app/routes/v1/admin.py` — add `POST /admin/prune-repo-cache`
- Test: `worker/tests/test_claude_code_runner.py` — add two prune tests

- [ ] **Step 1: Write failing prune tests**

In `worker/tests/test_claude_code_runner.py`, add at the end of the file:

```python
# ---- prune_old_repos ----

def test_prune_removes_stale_repo(tmp_path):
    import shutil
    import time

    runner = ClaudeCodeRunner(
        repo_cache_dir=str(tmp_path / "repos"),
        api_key="k",
        skills_dir=str(tmp_path / "skills"),
        ttl_days=30,
    )
    repo_dir = tmp_path / "repos" / "acme" / "old-repo"
    repo_dir.mkdir(parents=True)
    # Backdate mtime to 31 days ago.
    old_mtime = time.time() - (31 * 86400)
    os.utime(repo_dir, (old_mtime, old_mtime))

    removed = runner.prune_old_repos()

    assert str(repo_dir) in removed
    assert not repo_dir.exists()


def test_prune_keeps_recent_repo(tmp_path):
    runner = ClaudeCodeRunner(
        repo_cache_dir=str(tmp_path / "repos"),
        api_key="k",
        skills_dir=str(tmp_path / "skills"),
        ttl_days=30,
    )
    repo_dir = tmp_path / "repos" / "acme" / "new-repo"
    repo_dir.mkdir(parents=True)
    # mtime is now — well within the TTL.

    removed = runner.prune_old_repos()

    assert removed == []
    assert repo_dir.exists()
```

- [ ] **Step 2: Run tests — expect failure**

```bash
cd worker && .venv/bin/pytest tests/test_claude_code_runner.py::test_prune_removes_stale_repo tests/test_claude_code_runner.py::test_prune_keeps_recent_repo -v
```

Expected: `FAILED` — `TypeError` because `ClaudeCodeRunner.__init__` does not accept `ttl_days`.

- [ ] **Step 3: Add `ttl_days` and `prune_old_repos()` to `claude_code_runner.py`**

In `reva/claude_code_runner.py`, change `__init__` signature:

```python
    def __init__(
        self,
        repo_cache_dir: str,
        api_key: str,
        skills_dir: str,
        default_model: str = DEFAULT_MODEL,
        deep_model: str = DEEP_MODEL,
        ttl_days: int = 30,
    ) -> None:
        self.repo_cache_dir = repo_cache_dir
        self.api_key = api_key
        self.skills_dir = skills_dir
        self.default_model = default_model
        self.deep_model = deep_model
        self.ttl_days = ttl_days
```

Then add `prune_old_repos()` as a public method, after `review()` and before the `# helpers` section:

```python
    def prune_old_repos(self) -> list[str]:
        """Remove repo dirs not accessed within self.ttl_days. Returns removed paths."""
        import shutil
        import time

        base = Path(self.repo_cache_dir)
        if not base.exists():
            return []

        now = time.time()
        cutoff = now - (self.ttl_days * 86400)
        removed: list[str] = []

        for owner_dir in base.iterdir():
            if not owner_dir.is_dir():
                continue
            for repo_dir in owner_dir.iterdir():
                if not repo_dir.is_dir():
                    continue
                if repo_dir.stat().st_mtime < cutoff:
                    shutil.rmtree(repo_dir)
                    removed.append(str(repo_dir))
            try:
                owner_dir.rmdir()  # succeeds only if empty
            except OSError:
                pass

        return removed
```

- [ ] **Step 4: Run prune tests — expect pass**

```bash
cd worker && .venv/bin/pytest tests/test_claude_code_runner.py::test_prune_removes_stale_repo tests/test_claude_code_runner.py::test_prune_keeps_recent_repo -v
```

Expected: both `PASSED`.

- [ ] **Step 5: Pass `ttl_days` from settings in `runner.py`**

In `worker/worker/runner.py`, find the `ClaudeCodeRunner(...)` instantiation in `build_worker_context()`. Change:

```python
    runner = ClaudeCodeRunner(
        repo_cache_dir=settings.repo_cache_dir,
        api_key=settings.anthropic_api_key,
        skills_dir=settings.skills_dir,
    )
```

To:

```python
    runner = ClaudeCodeRunner(
        repo_cache_dir=settings.repo_cache_dir,
        api_key=settings.anthropic_api_key,
        skills_dir=settings.skills_dir,
        ttl_days=settings.repo_cache_ttl_days,
    )
```

Then add `run_prune_repo_cache` at the bottom of `runner.py`, after `run_weekly_report`:

```python
def run_prune_repo_cache(params: dict | None = None) -> dict:
    """RQ task: remove stale repos from the cache volume.

    Uses ClaudeCodeRunner.ttl_days (from REVA_REPO_CACHE_TTL_DAYS, default 30).
    """
    ctx = get_context()
    removed = ctx.runner.prune_old_repos()
    logger.info("repo_cache_pruned", count=len(removed), paths=removed)
    return {"count": len(removed), "removed": removed}
```

- [ ] **Step 6: Add the admin endpoint to `admin.py`**

In `api/app/routes/v1/admin.py`, append:

```python

@router.post("/prune-repo-cache")
async def trigger_prune_repo_cache(request: Request) -> dict:
    """Enqueue a repo cache prune job.

    Removes repos not accessed within REVA_REPO_CACHE_TTL_DAYS (default 30).
    """
    rq_queue = request.app.state.rq_queue
    job = rq_queue.enqueue("worker.runner.run_prune_repo_cache")
    return {"status": "queued", "job_id": job.id}
```

- [ ] **Step 7: Run full test suite**

```bash
cd worker && .venv/bin/pytest tests/ -q
```

Expected: all tests pass (161 now, +2 prune tests).

- [ ] **Step 8: Commit**

```bash
git add reva/claude_code_runner.py worker/worker/runner.py api/app/routes/v1/admin.py worker/tests/test_claude_code_runner.py
git commit -m "feat: add repo cache prune job (ClaudeCodeRunner.prune_old_repos + POST /admin/prune-repo-cache)"
```

---

## Self-review

**Spec coverage:**
- ✅ Task 1: `custom_instructions` passed via `skill_params`; skill templates acknowledge it
- ✅ Task 2: `/full-review`→`full`, `/deep-review`→`deep`; both tested
- ✅ Task 3: `--dangerously-skip-permissions` in subprocess args; tested
- ✅ Task 4: `prune_old_repos()` removes stale dirs; admin endpoint enqueues job; ttl_days from settings

**Placeholder scan:** None found.

**Type consistency:**
- `prune_old_repos()` returns `list[str]` — consistent across `runner.py` usage and test assertions
- `ttl_days: int` — consistent between `__init__` param, `self.ttl_days`, and test fixture
- `ClaudeCodeRunner` constructor gains `ttl_days` as optional param with default 30 — backward compatible with existing test fixtures that don't pass it
