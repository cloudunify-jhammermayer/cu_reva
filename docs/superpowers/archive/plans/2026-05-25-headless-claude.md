# Headless Claude Code Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace all PR review Claude API calls with a headless Claude Code CLI subprocess that clones repos locally and reads connected files, while keeping the direct Messages API for ticket analysis and comment replies.

**Architecture:** `ClaudeCodeRunner` wraps `claude --print` as a subprocess, writing structured review output to a temp JSON file which REVA reads back. A local repo cache (`/repos/{owner}/{name}/`) is cloned once per repo and updated with `git fetch` on each review. `Reviewer` swaps its `ClaudeClient` for `ClaudeCodeRunner`; a new `Auditor` class runs standalone repo audits. Three REVA skill template markdown files in `prompts/skills/` define what Claude does and how it structures its output.

**Tech Stack:** Python 3.12, subprocess, Claude Code CLI (`@anthropic-ai/claude-code`), pytest, Pydantic v2, FastAPI, RQ, PostgreSQL, Docker Compose

---

## Scope Note

The Go TUI `a` keybind (Repos tab → trigger audit) and the docs restructure (`doc/` → `docs/architecture/`) are **not** in this plan — they have no Python dependencies and should be separate tasks after this plan is complete.

---

## File Map

**New files:**
- `reva/claude_code_runner.py` — `ClaudeCodeRunner` class
- `worker/worker/auditor.py` — `Auditor` class (pure, no side effects)
- `worker/worker/audit_tasks.py` — RQ task entry point for audit
- `prompts/skills/reva-diff-review.md` — diff/deep review skill template
- `prompts/skills/reva-full-review.md` — full review skill template
- `prompts/skills/reva-repo-audit.md` — repo audit skill template
- `db/migrations/007_audit_runs.sql` — audit_runs table
- `worker/tests/test_claude_code_runner.py` — tests for `ClaudeCodeRunner`
- `worker/tests/test_auditor.py` — tests for `Auditor`

**Modified files:**
- `reva/types.py` — add `AuditJobParams`, `AuditResult`; add `"full"` to `ReviewMode`
- `worker/worker/settings.py` — add `repo_cache_dir`, `repo_cache_ttl_days`, `skills_dir`
- `worker/worker/reviewer.py` — replace `claude: ClaudeClient` with `runner: ClaudeCodeRunner`; update `execute()`
- `worker/tests/test_reviewer.py` — replace `FakeClaude` with `FakeRunner`
- `worker/worker/runner.py` — add `ClaudeCodeRunner` and `Auditor` to `WorkerContext`; update `build_worker_context()`
- `reva/db/repo_lookup.py` — add `get_repo_meta()` helper
- `api/app/routes/v1/repos.py` — add `POST /repos/{id}/audit` endpoint
- `api/app/routes/v1/__init__.py` — include audit router
- `worker/Dockerfile` — add git, nodejs, claude CLI, skill files, superpowers plugin
- `docker-compose.yml` — add `repo_cache` volume
- `docker-compose.prod.yml` — add `repo_cache` volume

---

## Task 1: Extend `reva/types.py`

**Files:**
- Modify: `reva/types.py`

- [ ] **Step 1: Add `"full"` to `ReviewMode`**

In `reva/types.py` line 30, change:
```python
ReviewMode = Literal["diff", "deep"]
```
to:
```python
ReviewMode = Literal["diff", "full", "deep"]
```

- [ ] **Step 2: Add `AuditJobParams` and `AuditResult` after `TicketJobParams`**

At the end of `reva/types.py` (before `ClaudeResponse`), add:
```python
class AuditJobParams(BaseModel):
    """Inputs handed to the repo audit RQ job."""

    repository_id: int
    installation_id: int
    requested_by: str | None = None


class AuditResult(BaseModel):
    """Outcome of a single repo audit run."""

    status: Literal["completed", "failed"]
    summary: str
    findings: list[Finding] = Field(default_factory=list)
    model: str = ""
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: int = 0
```

- [ ] **Step 3: Verify the import — `Literal` is already imported, `datetime` is already imported, `Finding` and `Field` are already present**

Run:
```bash
cd worker && .venv/bin/python -c "from reva.types import AuditJobParams, AuditResult, ReviewMode; print(ReviewMode)"
```
Expected: `typing.Literal['diff', 'full', 'deep']`

- [ ] **Step 4: Commit**
```bash
git add reva/types.py
git commit -m "feat: add AuditJobParams, AuditResult, full review mode to types"
```

---

## Task 2: Extend `worker/worker/settings.py`

**Files:**
- Modify: `worker/worker/settings.py`

- [ ] **Step 1: Add three new optional fields to the `Settings` dataclass**

After `odoo_callback_api_key: str = ""`, add:
```python
repo_cache_dir: str = "/repos"
repo_cache_ttl_days: int = 30
skills_dir: str = "/app/prompts/skills"
```

- [ ] **Step 2: Read them in `from_env()`**

In the `return cls(...)` call, add after `odoo_callback_api_key=...`:
```python
repo_cache_dir=os.environ.get("REVA_REPO_CACHE_DIR", "/repos"),
repo_cache_ttl_days=int(os.environ.get("REVA_REPO_CACHE_TTL_DAYS", "30")),
skills_dir=os.environ.get("REVA_SKILLS_DIR", "/app/prompts/skills"),
```

- [ ] **Step 3: Verify**
```bash
cd worker && .venv/bin/python -c "
from worker.settings import Settings
import os; os.environ.update({'REDIS_URL':'r','DATABASE_URL':'d','ANTHROPIC_API_KEY':'k','GITHUB_APP_ID':'1','GITHUB_PRIVATE_KEY_PATH':'/dev/null'})
# patch file open
import builtins; builtins.open = lambda *a,**k: type('F',(),{'read':lambda s:'x','__enter__':lambda s:s,'__exit__':lambda *a:None})()
s = Settings(redis_url='r',database_url='d',anthropic_api_key='k',github_app_id=1,github_private_key='k')
print(s.repo_cache_dir, s.repo_cache_ttl_days, s.skills_dir)
"
```
Expected: `/repos 30 /app/prompts/skills`

- [ ] **Step 4: Commit**
```bash
git add worker/worker/settings.py
git commit -m "feat: add repo_cache_dir, repo_cache_ttl_days, skills_dir to Settings"
```

---

## Task 3: `ClaudeCodeRunner.ensure_repo()` + tests

**Files:**
- Create: `reva/claude_code_runner.py`
- Create: `worker/tests/test_claude_code_runner.py`

- [ ] **Step 1: Create `reva/claude_code_runner.py` with `ensure_repo()`**

```python
"""Headless Claude Code CLI runner.

Wraps `claude --print` as a subprocess. Manages a local repo cache at
`repo_cache_dir/{owner}/{name}/` — clones once, fetches on subsequent calls.

Structured review output is written by Claude to a temp JSON file (the
output path is appended to the task string). REVA reads the file after the
process exits and deletes it regardless of outcome.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from reva.errors import PermanentError, TransientError
from reva.types import ClaudeResponse

DEFAULT_MODEL = "claude-sonnet-4-6"
DEEP_MODEL = "claude-opus-4-7"
_CLAUDE_BIN = "claude"


class ClaudeCodeRunner:
    def __init__(
        self,
        repo_cache_dir: str,
        api_key: str,
        skills_dir: str,
        default_model: str = DEFAULT_MODEL,
        deep_model: str = DEEP_MODEL,
    ) -> None:
        self.repo_cache_dir = repo_cache_dir
        self.api_key = api_key
        self.skills_dir = skills_dir
        self.default_model = default_model
        self.deep_model = deep_model

    # ------------------------------------------------------------------ public

    def ensure_repo(
        self,
        owner: str,
        name: str,
        head_sha: str | None,
        token: str,
    ) -> str:
        """Clone or fetch the repo; checkout head_sha (or reset to FETCH_HEAD if None).

        Returns the absolute path to the working tree.

        Raises:
            TransientError: git clone/fetch failure (network, auth expiry).
            PermanentError: git checkout failure (SHA not found in repo).
        """
        repo_path = os.path.join(self.repo_cache_dir, owner, name)
        clone_url = f"https://x-access-token:{token}@github.com/{owner}/{name}"

        if not os.path.isdir(repo_path):
            os.makedirs(os.path.dirname(repo_path), exist_ok=True)
            self._run_git_transient(["clone", clone_url, repo_path])
        else:
            self._run_git_transient(["-C", repo_path, "remote", "set-url", "origin", clone_url])
            self._run_git_transient(["-C", repo_path, "fetch", "origin"])

        if head_sha:
            self._run_git_permanent(["-C", repo_path, "checkout", head_sha])
        else:
            self._run_git_permanent(["-C", repo_path, "reset", "--hard", "FETCH_HEAD"])

        return repo_path

    def review(
        self,
        repo_path: str,
        skill: str,
        params: dict,
        model: str | None = None,
    ) -> ClaudeResponse:
        """Run `claude --print` in repo_path using a skill template.

        Reads `prompts/skills/{skill}.md`, appends task parameters and a temp
        output path, runs the CLI, reads the JSON written by Claude, and
        returns a ClaudeResponse with `tool_use_input` set to that JSON.

        Raises:
            PermanentError: non-zero exit code 1, or Claude wrote no valid JSON.
            TransientError: non-zero exit code other than 1 (killed, OOM, etc.).
        """
        output_path = self._create_output_path()
        skill_content = self._read_skill(skill)
        param_lines = "\n".join(f"{k}: {v}" for k, v in params.items())
        task = (
            f"{skill_content}\n\n"
            f"## Task Parameters\n\n"
            f"{param_lines}\n"
            f"output_path: {output_path}"
        )
        try:
            proc = subprocess.run(
                [
                    _CLAUDE_BIN, "--print",
                    "--output-format", "json",
                    "--model", model or self.default_model,
                    "--allowedTools", "Read,Bash,Grep,Write",
                    task,
                ],
                cwd=repo_path,
                env={**os.environ, "ANTHROPIC_API_KEY": self.api_key},
                capture_output=True,
                text=True,
                timeout=300,
            )
            if proc.returncode != 0:
                raise _exit_to_error(proc.returncode, proc.stderr)

            try:
                with open(output_path) as f:
                    tool_use_input = json.load(f)
            except FileNotFoundError as exc:
                raise PermanentError(
                    f"Claude did not create output file at {output_path}"
                ) from exc
            except json.JSONDecodeError as exc:
                raise PermanentError(
                    f"Claude wrote invalid JSON to {output_path}: {exc}"
                ) from exc

            return ClaudeResponse(
                model=model or self.default_model,
                stop_reason="tool_use",
                tool_use_input=tool_use_input,
            )
        finally:
            Path(output_path).unlink(missing_ok=True)

    # ----------------------------------------------------------------- helpers

    def _create_output_path(self) -> str:
        fd, path = tempfile.mkstemp(suffix=".json", prefix="reva_review_")
        os.close(fd)
        return path

    def _read_skill(self, skill: str) -> str:
        path = os.path.join(self.skills_dir, f"{skill}.md")
        try:
            with open(path) as f:
                return f.read()
        except FileNotFoundError as exc:
            raise PermanentError(f"Skill file not found: {path}") from exc

    def _run_git_transient(self, args: list[str]) -> None:
        """Run a git command; raises TransientError on failure."""
        result = subprocess.run(
            ["git"] + args, capture_output=True, text=True
        )
        if result.returncode != 0:
            raise TransientError(f"git {args[0]} failed: {result.stderr[:200]}")

    def _run_git_permanent(self, args: list[str]) -> None:
        """Run a git command; raises PermanentError on failure."""
        result = subprocess.run(
            ["git"] + args, capture_output=True, text=True
        )
        if result.returncode != 0:
            raise PermanentError(f"git {args[0]} failed: {result.stderr[:200]}")


# ---------------------------------------------------------------------- module


def _exit_to_error(returncode: int, stderr: str) -> Exception:
    snippet = stderr[:200]
    if returncode == 1:
        return PermanentError(f"claude exited 1: {snippet}")
    return TransientError(f"claude exited {returncode}: {snippet}")
```

- [ ] **Step 2: Write failing tests for `ensure_repo()`**

Create `worker/tests/test_claude_code_runner.py`:
```python
"""Tests for ClaudeCodeRunner.ensure_repo and review."""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, call, patch

import pytest

from reva.claude_code_runner import ClaudeCodeRunner
from reva.errors import PermanentError, TransientError


@pytest.fixture
def runner(tmp_path):
    return ClaudeCodeRunner(
        repo_cache_dir=str(tmp_path / "repos"),
        api_key="test-key",
        skills_dir=str(tmp_path / "skills"),
    )


def _ok():
    m = MagicMock()
    m.returncode = 0
    m.stderr = ""
    return m


def _fail(code=1, stderr="error"):
    m = MagicMock()
    m.returncode = code
    m.stderr = stderr
    return m


# ---- ensure_repo ----

def test_ensure_repo_clones_when_missing(runner, tmp_path):
    with patch("subprocess.run", return_value=_ok()) as mock_run:
        result = runner.ensure_repo("acme", "widgets", "abc123", "tok")

    assert result.endswith("acme/widgets")
    calls = [c.args[0] for c in mock_run.call_args_list]
    assert calls[0][:2] == ["git", "clone"]
    assert "x-access-token:tok@github.com/acme/widgets" in calls[0][2]
    assert calls[-1][:3] == ["git", "-C", result]
    assert "checkout" in calls[-1]
    assert "abc123" in calls[-1]


def test_ensure_repo_fetches_when_exists(runner, tmp_path):
    repo_path = tmp_path / "repos" / "acme" / "widgets"
    repo_path.mkdir(parents=True)

    with patch("subprocess.run", return_value=_ok()) as mock_run:
        runner.ensure_repo("acme", "widgets", "abc123", "tok")

    calls = [c.args[0] for c in mock_run.call_args_list]
    assert not any("clone" in c for c in calls)
    assert any("fetch" in c for c in calls)
    assert any("checkout" in c for c in calls)


def test_ensure_repo_no_sha_resets_to_fetch_head(runner, tmp_path):
    repo_path = tmp_path / "repos" / "acme" / "widgets"
    repo_path.mkdir(parents=True)

    with patch("subprocess.run", return_value=_ok()) as mock_run:
        runner.ensure_repo("acme", "widgets", None, "tok")

    calls = [c.args[0] for c in mock_run.call_args_list]
    assert any("reset" in c and "FETCH_HEAD" in c for c in calls)


def test_ensure_repo_clone_failure_raises_transient(runner):
    with patch("subprocess.run", return_value=_fail(code=128, stderr="network error")):
        with pytest.raises(TransientError, match="clone failed"):
            runner.ensure_repo("acme", "widgets", "abc123", "tok")


def test_ensure_repo_checkout_failure_raises_permanent(runner, tmp_path):
    repo_path = tmp_path / "repos" / "acme" / "widgets"
    repo_path.mkdir(parents=True)

    responses = [_ok(), _ok(), _fail(code=1, stderr="pathspec 'badsha' not found")]
    with patch("subprocess.run", side_effect=responses):
        with pytest.raises(PermanentError, match="checkout failed"):
            runner.ensure_repo("acme", "widgets", "badsha", "tok")
```

- [ ] **Step 3: Run tests — expect failures**
```bash
cd worker && .venv/bin/pytest tests/test_claude_code_runner.py::test_ensure_repo_clones_when_missing -v
```
Expected: `FAILED` — `ModuleNotFoundError` or attribute error (file not created yet).

Actually the file *is* created in step 1, so tests should fail on assertion. Run all `ensure_repo` tests:
```bash
cd worker && .venv/bin/pytest tests/test_claude_code_runner.py -k "ensure_repo" -v
```
Expected: failures due to assertion mismatches (the logic is new, tests verify it works correctly). Fix any issues until all `ensure_repo` tests pass.

- [ ] **Step 4: Run tests — expect pass**
```bash
cd worker && .venv/bin/pytest tests/test_claude_code_runner.py -k "ensure_repo" -v
```
Expected: all `ensure_repo` tests `PASSED`.

- [ ] **Step 5: Commit**
```bash
git add reva/claude_code_runner.py worker/tests/test_claude_code_runner.py
git commit -m "feat: add ClaudeCodeRunner.ensure_repo with tests"
```

---

## Task 4: `ClaudeCodeRunner.review()` + tests

**Files:**
- Modify: `worker/tests/test_claude_code_runner.py` (add review tests)

The `review()` method is already written in `reva/claude_code_runner.py` from Task 3. This task adds the tests.

- [ ] **Step 1: Add a `skill` fixture to the test file**

In `worker/tests/test_claude_code_runner.py`, add after the fixtures:
```python
@pytest.fixture
def skill_file(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    skill = skills_dir / "reva-diff-review.md"
    skill.write_text("You are REVA. Write JSON to output_path.")
    return tmp_path


@pytest.fixture
def runner_with_skill(tmp_path, skill_file):
    return ClaudeCodeRunner(
        repo_cache_dir=str(tmp_path / "repos"),
        api_key="test-key",
        skills_dir=str(skill_file / "skills"),
    )
```

- [ ] **Step 2: Add `review()` tests**

Append to `worker/tests/test_claude_code_runner.py`:
```python
# ---- review ----

def test_review_passes_correct_args(runner_with_skill, tmp_path):
    repo_path = str(tmp_path / "repo")
    os.makedirs(repo_path)
    review_output = {"summary": "Looks good.", "findings": []}

    def fake_run(args, **kwargs):
        # Locate and write the output file (last param in task string)
        task_str = args[-1]
        output_path = [line.split(": ", 1)[1] for line in task_str.splitlines() if line.startswith("output_path:")][0]
        with open(output_path, "w") as f:
            json.dump(review_output, f)
        return _ok()

    with patch("subprocess.run", side_effect=fake_run) as mock_run:
        resp = runner_with_skill.review(
            repo_path=repo_path,
            skill="reva-diff-review",
            params={"pr_title": "Fix bug", "diff": "diff content"},
        )

    assert resp.tool_use_input == review_output
    assert resp.model == "claude-sonnet-4-6"
    call_args = mock_run.call_args.args[0]
    assert "claude" in call_args[0]
    assert "--print" in call_args
    assert "--output-format" in call_args
    assert repo_path == mock_run.call_args.kwargs["cwd"]


def test_review_cleans_up_output_file_on_success(runner_with_skill, tmp_path):
    repo_path = str(tmp_path / "repo")
    os.makedirs(repo_path)
    captured_path = []

    def fake_run(args, **kwargs):
        task_str = args[-1]
        output_path = [line.split(": ", 1)[1] for line in task_str.splitlines() if line.startswith("output_path:")][0]
        captured_path.append(output_path)
        with open(output_path, "w") as f:
            json.dump({"summary": "ok", "findings": []}, f)
        return _ok()

    with patch("subprocess.run", side_effect=fake_run):
        runner_with_skill.review(repo_path=repo_path, skill="reva-diff-review", params={})

    assert not os.path.exists(captured_path[0])


def test_review_raises_permanent_on_exit_1(runner_with_skill, tmp_path):
    repo_path = str(tmp_path / "repo")
    os.makedirs(repo_path)

    with patch("subprocess.run", return_value=_fail(code=1, stderr="bad prompt")):
        with pytest.raises(PermanentError, match="exited 1"):
            runner_with_skill.review(repo_path=repo_path, skill="reva-diff-review", params={})


def test_review_raises_transient_on_exit_2(runner_with_skill, tmp_path):
    repo_path = str(tmp_path / "repo")
    os.makedirs(repo_path)

    with patch("subprocess.run", return_value=_fail(code=2, stderr="killed")):
        with pytest.raises(TransientError, match="exited 2"):
            runner_with_skill.review(repo_path=repo_path, skill="reva-diff-review", params={})


def test_review_raises_permanent_when_no_json_file(runner_with_skill, tmp_path):
    repo_path = str(tmp_path / "repo")
    os.makedirs(repo_path)

    with patch("subprocess.run", return_value=_ok()):
        with pytest.raises(PermanentError, match="did not create output file"):
            runner_with_skill.review(repo_path=repo_path, skill="reva-diff-review", params={})


def test_review_raises_permanent_on_invalid_json(runner_with_skill, tmp_path):
    repo_path = str(tmp_path / "repo")
    os.makedirs(repo_path)

    def fake_run(args, **kwargs):
        task_str = args[-1]
        output_path = [line.split(": ", 1)[1] for line in task_str.splitlines() if line.startswith("output_path:")][0]
        with open(output_path, "w") as f:
            f.write("not json {{{")
        return _ok()

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(PermanentError, match="invalid JSON"):
            runner_with_skill.review(repo_path=repo_path, skill="reva-diff-review", params={})


def test_review_missing_skill_file_raises_permanent(runner_with_skill, tmp_path):
    repo_path = str(tmp_path / "repo")
    os.makedirs(repo_path)

    with pytest.raises(PermanentError, match="Skill file not found"):
        runner_with_skill.review(repo_path=repo_path, skill="nonexistent-skill", params={})
```

- [ ] **Step 3: Run tests**
```bash
cd worker && .venv/bin/pytest tests/test_claude_code_runner.py -v
```
Expected: all tests pass. Fix any failures before continuing.

- [ ] **Step 4: Commit**
```bash
git add worker/tests/test_claude_code_runner.py
git commit -m "test: add ClaudeCodeRunner.review tests"
```

---

## Task 5: Skill template files

**Files:**
- Create: `prompts/skills/reva-diff-review.md`
- Create: `prompts/skills/reva-full-review.md`
- Create: `prompts/skills/reva-repo-audit.md`

- [ ] **Step 1: Create `prompts/skills/` directory**
```bash
mkdir -p /home/joseph/Projects/Cloudunify/cu_reva/prompts/skills
```

- [ ] **Step 2: Create `prompts/skills/reva-diff-review.md`**

```markdown
You are REVA, an automated code review assistant for an Odoo development team.

## Your task

Review the GitHub pull request described in the Task Parameters below. Your goal is to find genuine bugs, security vulnerabilities, performance problems, and maintainability issues — not style nitpicks unless they cause real harm.

## Review process

1. Read the diff in the Task Parameters section
2. Use the Read tool to examine the complete content of each changed file
3. Use the Read tool to check imports, parent classes, related models, and tests for the changed code
4. Use Grep to find callers of changed functions if the change is non-trivial
5. Identify only issues you are confident about (confidence >= 0.7)
6. Write your findings as JSON to the output_path

## Output format

Use the Write tool to write a JSON file to `output_path` with exactly this structure:

```json
{
  "summary": "2-3 sentence overview of the PR quality and key concerns",
  "findings": [
    {
      "severity": "critical",
      "category": "bug",
      "file": "custom_addons/module/models/partner.py",
      "line_start": 42,
      "line_end": 45,
      "title": "N+1 query in compute method (max 80 chars)",
      "body": "Detailed explanation of the issue and why it matters.",
      "suggestion": "Suggested fix code or approach, or null if none",
      "confidence": 0.9,
      "is_odoo_specific": true
    }
  ]
}
```

**Severity guide:**
- `critical`: data loss, security breach, crash, broken core functionality
- `major`: significant bug or performance problem that affects users
- `minor`: code smell, inconsistency, or mild inefficiency
- `info`: observation worth noting but not a problem

**Category values:** `bug`, `security`, `performance`, `maintainability`, `test`, `docs`, `style`, `architecture`, `odoo`

**Rules:**
- `file`, `line_start`, `line_end`, `suggestion` may be `null`
- `confidence` must be between 0.0 and 1.0 — omit findings below 0.7
- `is_odoo_specific` is `true` only for Odoo framework issues (ORM misuse, view conflicts, module manifest errors, etc.)
- Maximum 15 findings; prefer fewer, higher-confidence findings
- If the PR looks clean, return an empty `findings` array with an informative summary
```

- [ ] **Step 3: Create `prompts/skills/reva-full-review.md`**

```markdown
You are REVA, an automated code review assistant for an Odoo development team.

## Your task

Perform a thorough review of the pull request described in the Task Parameters. Unlike a diff-only review, you have full access to the repository — use it.

## Review process

1. Read the diff to understand what changed
2. For each changed file, use Read to examine the full file (not just the changed lines)
3. Trace relationships: imports, base classes, computed fields, related models, XML views, test files
4. Use Bash to run `grep -r "{function_name}" custom_addons/` to find all callers of changed APIs
5. Check if tests exist for changed behaviour; flag missing test coverage
6. Write your findings as JSON to the output_path

## Output format

Use the Write tool to write a JSON file to `output_path` with exactly this structure:

```json
{
  "summary": "2-3 sentence overview of the PR quality and key concerns",
  "findings": [
    {
      "severity": "critical",
      "category": "bug",
      "file": "custom_addons/module/models/partner.py",
      "line_start": 42,
      "line_end": 45,
      "title": "N+1 query in compute method (max 80 chars)",
      "body": "Detailed explanation of the issue and why it matters.",
      "suggestion": "Suggested fix code or approach, or null if none",
      "confidence": 0.9,
      "is_odoo_specific": true
    }
  ]
}
```

**Severity guide:**
- `critical`: data loss, security breach, crash, broken core functionality
- `major`: significant bug or performance problem that affects users
- `minor`: code smell, inconsistency, or mild inefficiency
- `info`: observation worth noting but not a problem

**Category values:** `bug`, `security`, `performance`, `maintainability`, `test`, `docs`, `style`, `architecture`, `odoo`

**Rules:**
- `file`, `line_start`, `line_end`, `suggestion` may be `null`
- `confidence` must be between 0.0 and 1.0 — omit findings below 0.7
- `is_odoo_specific` is `true` only for Odoo framework issues
- Maximum 15 findings; prefer fewer, higher-confidence findings
```

- [ ] **Step 4: Create `prompts/skills/reva-repo-audit.md`**

```markdown
You are REVA, an automated code audit assistant for an Odoo development team.

## Your task

Perform a comprehensive audit of the repository described in the Task Parameters. This is not a PR review — explore the full codebase and identify systemic issues.

## Audit process

1. Use Bash to list the module structure: `find custom_addons -name "__manifest__.py" | head -30`
2. For each module, read the manifest and key models/controllers
3. Use Grep to identify common anti-patterns: `grep -r "sudo()" custom_addons/ --include="*.py" -l`
4. Look for: security misuse (unrestricted sudo), missing access rules, N+1 ORM patterns, deprecated API usage, missing test coverage
5. Write your structured audit report as JSON to the output_path

## Output format

Use the Write tool to write a JSON file to `output_path` with exactly this structure:

```json
{
  "summary": "3-5 sentence overview of the codebase health and top concerns",
  "findings": [
    {
      "severity": "major",
      "category": "security",
      "file": "custom_addons/module/models/partner.py",
      "line_start": null,
      "line_end": null,
      "title": "Unrestricted sudo() in public method (max 80 chars)",
      "body": "The method `get_partner_data` calls sudo() without checking the caller's access rights, allowing any authenticated user to read all partner records.",
      "suggestion": "Add a `check_access_rights('read')` call before sudo(), or restrict the method to internal users.",
      "confidence": 0.85,
      "is_odoo_specific": true
    }
  ]
}
```

**Severity guide:**
- `critical`: data loss, security breach, crash in production
- `major`: significant bug or security risk
- `minor`: code smell or inconsistency
- `info`: observation worth noting

**Category values:** `bug`, `security`, `performance`, `maintainability`, `test`, `docs`, `style`, `architecture`, `odoo`

**Rules:**
- Focus on systemic patterns, not one-off style issues
- Maximum 15 findings; pick the highest-impact ones
- `confidence` >= 0.7 only
```

- [ ] **Step 5: Commit**
```bash
git add prompts/skills/
git commit -m "feat: add REVA skill templates for diff, full, and repo audit reviews"
```

---

## Task 6: Update `Reviewer` to use `ClaudeCodeRunner`

**Files:**
- Modify: `worker/worker/reviewer.py`
- Modify: `worker/tests/test_reviewer.py`

- [ ] **Step 1: Write failing test — `Reviewer` with `FakeRunner`**

In `worker/tests/test_reviewer.py`:

Replace the `FakeClaude` class and `_make_reviewer` function with:
```python
@dataclass
class FakeRunner:
    """Fake ClaudeCodeRunner for Reviewer tests."""

    response: ClaudeResponse | None = None
    raise_exc: Exception | None = None
    default_model: str = "claude-sonnet-4-6"
    deep_model: str = "claude-opus-4-7"
    last_model: str | None = None
    last_skill: str | None = None
    last_params: dict | None = None
    repo_path_returned: str = "/fake/repos/acme/widgets"

    def ensure_repo(self, owner: str, name: str, head_sha: str | None, token: str) -> str:
        return self.repo_path_returned

    def review(self, repo_path: str, skill: str, params: dict, model: str | None = None) -> ClaudeResponse:
        self.last_model = model
        self.last_skill = skill
        self.last_params = params
        if self.raise_exc:
            raise self.raise_exc
        return self.response


def _make_reviewer(**overrides) -> tuple[Reviewer, FakeGitHub, FakeRepos, FakeRunner, FakePrompts]:
    github = overrides.pop("github", None) or FakeGitHub()
    repos = overrides.pop("repos", None) or FakeRepos()
    runner = overrides.pop("runner", None) or FakeRunner()
    prompts = overrides.pop("prompts", None) or FakePrompts()
    reviewer = Reviewer(
        runner=runner,  # type: ignore[arg-type]
        github=github,
        repos=repos,
        prompts=prompts,  # type: ignore[arg-type]
        **overrides,
    )
    return reviewer, github, repos, runner, prompts
```

- [ ] **Step 2: Run the existing reviewer tests — expect failures**
```bash
cd worker && .venv/bin/pytest tests/test_reviewer.py -v 2>&1 | head -30
```
Expected: `TypeError` — `Reviewer.__init__` still expects `claude`, not `runner`.

- [ ] **Step 3: Update `Reviewer.__init__` and `execute()` in `worker/worker/reviewer.py`**

Change the import block (remove `ClaudeClient`, add `ClaudeCodeRunner`):
```python
# Remove:
from reva.claude_client import ClaudeClient
# Add:
from reva.claude_code_runner import ClaudeCodeRunner
```

Change `Reviewer.__init__`:
```python
class Reviewer:
    def __init__(
        self,
        runner: ClaudeCodeRunner,
        github: GitHubReader,
        repos: RepoLookup,
        prompts: PromptBuilder,
        max_diff_lines: int = DEFAULT_MAX_DIFF_LINES,
        max_diff_tokens: int = DEFAULT_MAX_DIFF_TOKENS,
    ) -> None:
        self.runner = runner
        self.github = github
        self.repos = repos
        self.prompts = prompts
        self.max_diff_lines = max_diff_lines
        self.max_diff_tokens = max_diff_tokens
```

In `execute()`, replace steps 5-12 of the current implementation. The full updated `execute()` method from the `# 5.` comment onward (after the stale check block):

```python
        # 5. Fetch diff + changed files.
        raw_diff = self.github.get_pull_request_diff(token, owner, name, pr_number)
        diff = filter_diff(raw_diff)
        if len(diff) < len(raw_diff):
            logger.info(
                "diff_filtered",
                owner=owner, repo=name, pr=pr_number,
                raw_bytes=len(raw_diff), filtered_bytes=len(diff),
                review_prefixes=DEFAULT_REVIEW_PREFIXES,
                excluded_extensions=sorted(DEFAULT_EXCLUDE_EXTENSIONS),
            )
        if not diff.strip():
            prefixes = ", ".join(f"`{p}`" for p in DEFAULT_REVIEW_PREFIXES)
            return _decline(
                f"No reviewable files found. Only changes under {prefixes} "
                f"are reviewed (excluding {', '.join(sorted(DEFAULT_EXCLUDE_EXTENSIONS))})."
            )

        changed_files_payload = self.github.get_changed_files(token, owner, name, pr_number)
        changed_files = [
            f["filename"] for f in changed_files_payload
            if any(f["filename"].startswith(p) for p in DEFAULT_REVIEW_PREFIXES)
            and os.path.splitext(f["filename"])[1].lower() not in DEFAULT_EXCLUDE_EXTENSIONS
        ]

        # 6. Load .claude-review.yml (CLAUDE.md is picked up automatically by Claude Code).
        repo_config = self._load_repo_config(token, owner, name, params.head_sha)

        # 7. Resolve per-review limits.
        max_lines, max_tokens = self._resolve_limits(repo_config)

        # 8. Diff size guards.
        diff_lines = count_diff_lines(diff)
        diff_tokens = estimate_diff_tokens(diff)
        if diff_lines > max_lines:
            return _decline(
                f"Diff too large ({diff_lines} lines > {max_lines} max). "
                f"Please split this PR into smaller, focused changes."
            )
        if diff_tokens > max_tokens:
            return _decline(
                f"Diff exceeds the token budget ({diff_tokens} tokens > "
                f"{max_tokens} max). Please split this PR."
            )

        # 9. skip_paths filtering.
        if repo_config.skip_paths:
            diff = filter_diff_by_paths(diff, repo_config.skip_paths)
            if not diff.strip():
                return _decline(
                    "All changed files matched skip_paths; nothing reviewable remains."
                )
            diff_lines = count_diff_lines(diff)
            diff_tokens = estimate_diff_tokens(diff)
            if diff_lines > max_lines:
                return _decline(
                    f"Diff still too large after skip_paths filtering "
                    f"({diff_lines} lines > {max_lines} max). "
                    f"Add more patterns to skip_paths or split the PR."
                )
            if diff_tokens > max_tokens:
                return _decline(
                    f"Diff still too large after skip_paths filtering "
                    f"({diff_tokens} tokens > {max_tokens} max). "
                    f"Add more patterns to skip_paths or split the PR."
                )

        # 10. Select model and skill.
        model = self.runner.deep_model if params.review_mode == "deep" else self.runner.default_model
        skill = "reva-full-review" if params.review_mode == "full" else "reva-diff-review"

        skill_params = {
            "pr_title": pr_basic.get("title", ""),
            "pr_body": pr_basic.get("body") or pr_detail.get("body") or "",
            "diff": diff,
            "changed_files": "\n".join(f"- {f}" for f in changed_files),
            "base_branch": pr_basic["base_branch"],
            "head_branch": pr_basic["head_branch"],
        }

        # 11. Ensure repo is cloned/updated, then call Claude Code.
        started_at = datetime.now(timezone.utc)
        repo_path = self.runner.ensure_repo(owner, name, params.head_sha, token)
        response = self.runner.review(repo_path=repo_path, skill=skill, params=skill_params, model=model)
        completed_at = datetime.now(timezone.utc)
        duration_ms = int((completed_at - started_at).total_seconds() * 1000)

        # 12. Validate and parse findings.
        summary, findings = _parse_tool_use(response.tool_use_input)

        # 13. Cap findings by severity * confidence, then recompute risk_level.
        capped = _cap_findings(findings, MAX_FINDINGS)
        risk_level = _recompute_risk_level(capped)

        # 14. Cost (not available from CLI path; token counts are zero).
        cost = estimate_cost(
            response.model or model,
            response.input_tokens,
            response.output_tokens,
            response.cache_read_tokens,
            response.cache_creation_tokens,
        )

        # 15. Prompt version (best-effort).
        try:
            prompt_version = self.prompts.get_version()
        except Exception as exc:  # noqa: BLE001
            logger.warning("prompt_version_unavailable", error=str(exc))
            prompt_version = None

        return ReviewResult(
            status="completed",
            summary=summary,
            risk_level=risk_level,
            findings=capped,
            diff=diff,
            model=response.model or model,
            prompt_version=prompt_version,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=duration_ms,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cache_read_tokens=response.cache_read_tokens,
            cache_creation_tokens=response.cache_creation_tokens,
            estimated_cost_usd=cost,
        )
```

Also remove `build_system_blocks` and `build_user_prompt` calls from `FakePrompts` in the tests — they're no longer called by `Reviewer`.

- [ ] **Step 4: Run reviewer tests**
```bash
cd worker && .venv/bin/pytest tests/test_reviewer.py -v
```
Expected: all tests pass. Fix any failures (likely import errors or signature mismatches).

- [ ] **Step 5: Run the full test suite to confirm no regressions**
```bash
cd worker && .venv/bin/pytest tests/ -v
```
Expected: all tests pass.

- [ ] **Step 6: Commit**
```bash
git add worker/worker/reviewer.py worker/tests/test_reviewer.py
git commit -m "feat: Reviewer now uses ClaudeCodeRunner for all PR review modes"
```

---

## Task 7: Wire `ClaudeCodeRunner` into `WorkerContext`

**Files:**
- Modify: `worker/worker/runner.py`

- [ ] **Step 1: Add `ClaudeCodeRunner` import and `runner` field**

At the top of `worker/worker/runner.py`, add:
```python
from reva.claude_code_runner import ClaudeCodeRunner
```

In `WorkerContext`, add `runner: ClaudeCodeRunner` after `claude`:
```python
@dataclass(frozen=True)
class WorkerContext:
    db: Database
    claude: ClaudeClient
    runner: ClaudeCodeRunner
    github: GitHubClient
    reviewer: Reviewer
    ticket_analyzer: TicketAnalyzer
    odoo: OdooCallbackClient
    google_chat_webhook_url: str = ""
```

- [ ] **Step 2: Instantiate `ClaudeCodeRunner` in `build_worker_context()` and update `Reviewer`**

In `build_worker_context()`, after `claude = ClaudeClient(...)`:
```python
runner = ClaudeCodeRunner(
    repo_cache_dir=settings.repo_cache_dir,
    api_key=settings.anthropic_api_key,
    skills_dir=settings.skills_dir,
)
```

Change the `Reviewer(...)` call to:
```python
reviewer = Reviewer(
    runner=runner,
    github=github,
    repos=DatabaseRepoLookup(db),
    prompts=prompts,
)
```

Add `runner=runner` to the `WorkerContext(...)` call:
```python
context = WorkerContext(
    db=db,
    claude=claude,
    runner=runner,
    github=github,
    reviewer=reviewer,
    ticket_analyzer=ticket_analyzer,
    odoo=odoo,
    google_chat_webhook_url=settings.google_chat_webhook_url,
)
```

- [ ] **Step 3: Run tests**
```bash
cd worker && .venv/bin/pytest tests/ -v
```
Expected: all tests pass (the runner tests mock `subprocess.run` so no real `claude` binary needed).

- [ ] **Step 4: Commit**
```bash
git add worker/worker/runner.py
git commit -m "feat: wire ClaudeCodeRunner into WorkerContext and Reviewer"
```

---

## Task 8: `Auditor` class + DB helper + tests

**Files:**
- Create: `worker/worker/auditor.py`
- Modify: `reva/db/repo_lookup.py`
- Create: `worker/tests/test_auditor.py`

- [ ] **Step 1: Add `get_repo_meta()` to `reva/db/repo_lookup.py`**

Append to `reva/db/repo_lookup.py`:
```python
def get_repo_meta(db: Database, repository_id: int) -> dict:
    """Return {owner, name, installation_id, default_branch} for a repo."""
    with db.session() as s:
        row = s.execute(
            select(
                Repository.owner,
                Repository.name,
                Repository.installation_id,
                Repository.default_branch,
            ).where(Repository.id == repository_id)
        ).first()
    if not row:
        raise LookupError(f"Repository {repository_id} not found")
    return {
        "owner": row.owner,
        "name": row.name,
        "installation_id": row.installation_id,
        "default_branch": row.default_branch or "main",
    }
```

- [ ] **Step 2: Write failing test for `Auditor`**

Create `worker/tests/test_auditor.py`:
```python
"""Tests for Auditor.execute."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest

from reva.types import AuditJobParams, AuditResult, ClaudeResponse, Finding
from worker.auditor import Auditor


@dataclass
class FakeGitHub:
    def get_installation_token(self, installation_id: int) -> str:
        return "ghs_tok"


@dataclass
class FakeRunner:
    response: ClaudeResponse | None = None
    raise_exc: Exception | None = None
    default_model: str = "claude-sonnet-4-6"
    deep_model: str = "claude-opus-4-7"
    last_skill: str | None = None
    last_params: dict | None = None

    def ensure_repo(self, owner, name, head_sha, token) -> str:
        return f"/fake/repos/{owner}/{name}"

    def review(self, repo_path, skill, params, model=None) -> ClaudeResponse:
        self.last_skill = skill
        self.last_params = params
        if self.raise_exc:
            raise self.raise_exc
        return self.response


@dataclass
class FakeRepos:
    meta: dict = field(default_factory=lambda: {
        "owner": "acme",
        "name": "widgets",
        "installation_id": 100,
        "default_branch": "main",
    })

    def get_repo_meta(self, repository_id: int) -> dict:
        return self.meta


def _audit_response(findings=None) -> ClaudeResponse:
    return ClaudeResponse(
        model="claude-sonnet-4-6",
        stop_reason="tool_use",
        tool_use_input={
            "summary": "Codebase looks reasonable.",
            "findings": findings or [],
        },
    )


def _params(**overrides) -> AuditJobParams:
    base = {"repository_id": 1, "installation_id": 100}
    base.update(overrides)
    return AuditJobParams(**base)


def _make_auditor(**overrides):
    runner = overrides.pop("runner", None) or FakeRunner(response=_audit_response())
    github = overrides.pop("github", None) or FakeGitHub()
    repos = overrides.pop("repos", None) or FakeRepos()
    return Auditor(runner=runner, github=github, repos=repos), runner, github, repos


def test_audit_completed_returns_result():
    auditor, _, _, _ = _make_auditor()
    result = auditor.execute(_params())
    assert result.status == "completed"
    assert result.summary == "Codebase looks reasonable."
    assert result.findings == []


def test_audit_uses_reva_repo_audit_skill():
    auditor, runner, _, _ = _make_auditor()
    auditor.execute(_params())
    assert runner.last_skill == "reva-repo-audit"


def test_audit_ensure_repo_called_with_none_sha():
    called_with = []

    class TrackingRunner(FakeRunner):
        def ensure_repo(self, owner, name, head_sha, token):
            called_with.append(head_sha)
            return "/fake"

    auditor, _, _, _ = _make_auditor(runner=TrackingRunner(response=_audit_response()))
    auditor.execute(_params())
    assert called_with == [None]


def test_audit_propagates_transient_error():
    from reva.errors import TransientError
    runner = FakeRunner(raise_exc=TransientError("timeout"))
    auditor, _, _, _ = _make_auditor(runner=runner)
    with pytest.raises(TransientError):
        auditor.execute(_params())
```

- [ ] **Step 3: Run test — expect failure (Auditor not yet created)**
```bash
cd worker && .venv/bin/pytest tests/test_auditor.py -v 2>&1 | head -10
```
Expected: `ModuleNotFoundError: No module named 'worker.auditor'`

- [ ] **Step 4: Create `worker/worker/auditor.py`**

```python
"""Standalone repo audit — pure, no side effects.

`Auditor.execute` clones or fetches the repo at its latest HEAD, runs Claude
Code with the reva-repo-audit skill, and returns an `AuditResult`. It does
NOT write to Postgres or post to GitHub — those side effects live in
`audit_tasks.run_audit`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from reva.claude_code_runner import ClaudeCodeRunner
from reva.errors import PermanentError
from reva.types import AuditJobParams, AuditResult, Finding
from pydantic import ValidationError
import structlog

logger = structlog.get_logger()


class GitHubTokenProvider(Protocol):
    def get_installation_token(self, installation_id: int) -> str: ...


class RepoMetaLookup(Protocol):
    def get_repo_meta(self, repository_id: int) -> dict: ...


class Auditor:
    def __init__(
        self,
        runner: ClaudeCodeRunner,
        github: GitHubTokenProvider,
        repos: RepoMetaLookup,
    ) -> None:
        self.runner = runner
        self.github = github
        self.repos = repos

    def execute(self, params: AuditJobParams) -> AuditResult:
        """Run a full repo audit. Returns AuditResult.

        Raises:
            TransientError: bubbles from ClaudeCodeRunner (network/git failure).
            PermanentError: Claude output invalid, or repo not found.
        """
        meta = self.repos.get_repo_meta(params.repository_id)
        owner, name = meta["owner"], meta["name"]

        token = self.github.get_installation_token(params.installation_id)

        started_at = datetime.now(timezone.utc)
        repo_path = self.runner.ensure_repo(owner, name, None, token)
        response = self.runner.review(
            repo_path=repo_path,
            skill="reva-repo-audit",
            params={"repo": f"{owner}/{name}", "default_branch": meta["default_branch"]},
        )
        completed_at = datetime.now(timezone.utc)
        duration_ms = int((completed_at - started_at).total_seconds() * 1000)

        tool_input = response.tool_use_input
        if not isinstance(tool_input, dict):
            raise PermanentError("Audit: Claude returned no tool_use input")
        summary = tool_input.get("summary", "")
        if not summary:
            raise PermanentError("Audit: Claude returned empty summary")

        raw_findings = tool_input.get("findings", [])
        try:
            findings = [Finding.model_validate(f) for f in raw_findings]
        except ValidationError as exc:
            raise PermanentError(f"Audit finding failed schema validation: {exc}") from exc

        return AuditResult(
            status="completed",
            summary=summary,
            findings=findings,
            model=response.model,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=duration_ms,
        )
```

- [ ] **Step 5: Run auditor tests**
```bash
cd worker && .venv/bin/pytest tests/test_auditor.py -v
```
Expected: all pass. Fix any failures.

- [ ] **Step 6: Run full test suite**
```bash
cd worker && .venv/bin/pytest tests/ -v
```
Expected: all pass.

- [ ] **Step 7: Commit**
```bash
git add reva/db/repo_lookup.py worker/worker/auditor.py worker/tests/test_auditor.py
git commit -m "feat: add Auditor class and get_repo_meta DB helper"
```

---

## Task 9: DB migration for `audit_runs`

**Files:**
- Create: `db/migrations/007_audit_runs.sql`

- [ ] **Step 1: Create the migration**

Create `db/migrations/007_audit_runs.sql`:
```sql
-- Audit runs: one row per triggered repo audit.
CREATE TABLE IF NOT EXISTS audit_runs (
    id BIGSERIAL PRIMARY KEY,
    repository_id BIGINT NOT NULL REFERENCES repositories(id),
    status TEXT NOT NULL DEFAULT 'started',
    requested_by TEXT,
    summary TEXT,
    model TEXT,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    duration_ms INTEGER,
    finding_count INTEGER DEFAULT 0,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_audit_runs_repository_id ON audit_runs (repository_id);
CREATE INDEX idx_audit_runs_status ON audit_runs (status);
```

- [ ] **Step 2: Add the `AuditRun` SQLAlchemy model to `reva/db/models.py`**

Append to `reva/db/models.py`:
```python
# --------------------------------------------------------------- audit_runs


class AuditRun(Base):
    __tablename__ = "audit_runs"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    repository_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("repositories.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, default="started")
    requested_by: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    finding_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("idx_audit_runs_repository_id", "repository_id"),
        Index("idx_audit_runs_status", "status"),
    )
```

- [ ] **Step 3: Verify migration runs without error (SQLite in-memory)**
```bash
cd worker && .venv/bin/python -c "
from reva.db.engine import create_engine_from_url, Database
engine = create_engine_from_url('sqlite://')
db = Database(engine)
db.migrate('../db/migrations')
print('migration OK')
"
```
Expected: `migration OK`

- [ ] **Step 4: Commit**
```bash
git add db/migrations/007_audit_runs.sql reva/db/models.py
git commit -m "feat: add audit_runs table and AuditRun SQLAlchemy model"
```

---

## Task 10: Audit RQ task + API endpoint

**Files:**
- Create: `worker/worker/audit_tasks.py`
- Modify: `worker/worker/runner.py` — add `auditor` to `WorkerContext`
- Modify: `api/app/routes/v1/repos.py` — add audit trigger endpoint
- Modify: `api/app/routes/v1/__init__.py` — no change needed (audit is in repos.py)

- [ ] **Step 1: Create `worker/worker/audit_tasks.py`**

```python
"""RQ task entry point for repo audit jobs."""

from __future__ import annotations

import structlog

from reva.types import AuditJobParams
from worker.runner import get_context

logger = structlog.get_logger()


def run_audit(job_params: dict) -> dict:
    """RQ task: run a full repo audit and persist the result."""
    from reva.db.models import AuditRun
    from sqlalchemy import insert, update

    ctx = get_context()
    params = AuditJobParams.model_validate(job_params)
    log = logger.bind(repository_id=params.repository_id)
    log.info("audit_job_start")

    # Record audit started.
    with ctx.db.session() as s:
        result_row = s.execute(
            insert(AuditRun).values(
                repository_id=params.repository_id,
                status="started",
                requested_by=params.requested_by,
            ).returning(AuditRun.id)
        )
        audit_id = result_row.scalar_one()
        s.commit()

    try:
        result = ctx.auditor.execute(params)
    except Exception as exc:
        with ctx.db.session() as s:
            s.execute(
                update(AuditRun)
                .where(AuditRun.id == audit_id)
                .values(status="failed", error_message=str(exc)[:500])
            )
            s.commit()
        log.error("audit_job_failed", error=str(exc))
        raise

    with ctx.db.session() as s:
        s.execute(
            update(AuditRun)
            .where(AuditRun.id == audit_id)
            .values(
                status="completed",
                summary=result.summary,
                model=result.model,
                started_at=result.started_at,
                completed_at=result.completed_at,
                duration_ms=result.duration_ms,
                finding_count=len(result.findings),
            )
        )
        s.commit()

    log.info("audit_job_done", findings=len(result.findings))
    return {"audit_id": audit_id, "status": "completed", "findings": len(result.findings)}
```

- [ ] **Step 2: Add `auditor` to `WorkerContext` in `runner.py`**

In `worker/worker/runner.py`:

Add import:
```python
from worker.auditor import Auditor
```

Add `auditor: Auditor` to `WorkerContext` after `reviewer`:
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
    google_chat_webhook_url: str = ""
```

In `build_worker_context()`, add after `reviewer = Reviewer(...)`:
```python
from reva.db import repo_lookup
auditor = Auditor(
    runner=runner,
    github=github,
    repos=type("_Repos", (), {"get_repo_meta": lambda self, rid: repo_lookup.get_repo_meta(db, rid)})(),
)
```

Add `auditor=auditor` to `WorkerContext(...)`.

- [ ] **Step 3: Add `POST /repos/{repository_id}/audit` endpoint to `api/app/routes/v1/repos.py`**

```python
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from rq import Queue

from app.dependencies import get_db, get_queue
from app.queries import repos as q
from app.schemas.repos import RepoPage, RepoSummary
from reva.db.engine import Database
from reva.db.models import Repository
from sqlalchemy import select

router = APIRouter()


@router.get("/repos", response_model=RepoPage)
def list_repos(db: Database = Depends(get_db)) -> dict:
    items, total = q.list_repos(db)
    return {"items": [RepoSummary.model_validate(r) for r in items], "total": total}


@router.post("/repos/{repository_id}/audit", status_code=202)
def trigger_audit(
    repository_id: int,
    db: Database = Depends(get_db),
    queue: Queue = Depends(get_queue),
) -> dict:
    """Enqueue a full repo audit job. Returns the RQ job ID."""
    from reva.db.repo_lookup import get_repo_meta
    from worker.audit_tasks import run_audit

    try:
        meta = get_repo_meta(db, repository_id)
    except LookupError:
        raise HTTPException(status_code=404, detail=f"Repository {repository_id} not found")

    job = queue.enqueue(
        run_audit,
        {
            "repository_id": repository_id,
            "installation_id": meta["installation_id"],
        },
    )
    return {"job_id": job.id, "repository_id": repository_id}
```

- [ ] **Step 4: Add `get_queue` dependency to `api/app/dependencies.py`**

Open `api/app/dependencies.py` and add:
```python
def get_queue(request: Request) -> Queue:
    return request.app.state.rq_queue
```
(Add `from fastapi import Request` and `from rq import Queue` imports if not already present.)

- [ ] **Step 5: Run tests**
```bash
cd worker && .venv/bin/pytest tests/ -v
```
Expected: all pass.

- [ ] **Step 6: Commit**
```bash
git add worker/worker/audit_tasks.py worker/worker/runner.py api/app/routes/v1/repos.py api/app/dependencies.py
git commit -m "feat: add audit RQ task, Auditor in WorkerContext, POST /repos/{id}/audit endpoint"
```

---

## Task 11: Docker changes

**Files:**
- Modify: `worker/Dockerfile`
- Modify: `docker-compose.yml`
- Modify: `docker-compose.prod.yml`

- [ ] **Step 1: Read the existing worker Dockerfile**
```bash
cat worker/Dockerfile
```

- [ ] **Step 2: Add git, Node.js, and Claude Code CLI to the Dockerfile**

Add to the worker's `Dockerfile` in the system packages install block:
```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code
```

- [ ] **Step 3: Copy skill files and superpowers plugin into the image**

After the `COPY` commands for app code, add:
```dockerfile
# REVA skill templates
COPY prompts/skills/ /app/prompts/skills/

# Superpowers plugin (copy from host; no auth needed at build time)
COPY .claude/plugins/superpowers/ /root/.claude/plugins/superpowers/
```

- [ ] **Step 4: Add `repo_cache` volume to `docker-compose.yml`**

In `docker-compose.yml`, add to the `worker` service volumes:
```yaml
    volumes:
      - repo_cache:/repos
```

And add to the top-level `volumes:` section:
```yaml
volumes:
  repo_cache:
```

Also add the env var to the worker service's environment block:
```yaml
    environment:
      - REVA_REPO_CACHE_DIR=/repos
```

- [ ] **Step 5: Mirror the same changes in `docker-compose.prod.yml`**

Same volume mount and env var as step 4.

- [ ] **Step 6: Verify the compose files parse without error**
```bash
docker compose config --quiet && echo "compose OK"
docker compose -f docker-compose.prod.yml config --quiet && echo "prod compose OK"
```
Expected: `compose OK` and `prod compose OK`.

- [ ] **Step 7: Commit**
```bash
git add worker/Dockerfile docker-compose.yml docker-compose.prod.yml
git commit -m "feat: add Claude Code CLI, git, skill files, and repo_cache volume to worker"
```

---

## Self-review checklist

After all tasks are committed, verify the full plan against the spec:

- [ ] `ReviewMode` includes `"full"` — Task 1
- [ ] `ClaudeCodeRunner.ensure_repo()` clones, fetches, handles `None` head_sha — Task 3
- [ ] `ClaudeCodeRunner.review()` writes temp file, reads it back, cleans up — Task 4
- [ ] Three skill files cover diff, full, and audit — Task 5
- [ ] `Reviewer` no longer uses `ClaudeClient` — Task 6
- [ ] `Reviewer` fetches diff for size guards but lets Claude read full files from cloned repo — Task 6
- [ ] `ClaudeClient` (API) still used for `run_comment_reply` — unchanged in Task 7
- [ ] `Auditor` passes `head_sha=None` to `ensure_repo` (reset to FETCH_HEAD) — Task 8
- [ ] Migration `007_audit_runs.sql` and `AuditRun` model — Task 9
- [ ] `POST /repos/{id}/audit` endpoint returns 202 with `job_id` — Task 10
- [ ] `REVA_REPO_CACHE_DIR=/repos` env var wired in docker-compose — Task 11
- [ ] `prompts/skills/` copied into worker image — Task 11

**Not in this plan (separate tasks):**
- TUI `a` keybind on Repos tab (Go code)
- Docs restructure (`doc/` → `docs/architecture/`)
- Repo cache daily prune scheduler job
