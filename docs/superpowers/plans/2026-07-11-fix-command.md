# /fix — Finding Fix Agent — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A trusted developer replies `/fix` (optionally with guidance text) on one of REVA's inline finding comments, and REVA fixes **that finding** with a headless Claude Code run on a throwaway worktree of the PR branch, then pushes a **single commit** (`REVA fix: <finding title>` + thread permalink, authored as the REVA bot) directly to the PR branch — never force-pushed, behind a per-repo `fix_command` opt-in that is **OFF by default**. Deterministic gates (non-empty diff, ≤ 150 changed lines, ≤ 5 files, nothing under `.github/`, head-SHA race guard) run before any push; every abort replies on the thread and records an ops event. The push deliberately triggers the normal debounce → incremental delta re-review.

**Spec:** `docs/superpowers/specs/2026-07-11-fix-command-design.md` (approved 2026-07-11 — the authority; re-read it before starting).

**Architecture:** api `_handle_review_comment` (`api/app/routes/webhooks.py:349` — the `pull_request_review_comment` path where `/dismiss`/`/mute`/`/unmute` live behind the existing bot guard + `_TRUSTED_ASSOCIATIONS` gate) gains a `/fix` branch that resolves the finding via the **existing comment→finding mapping** — `review_findings.github_comment_id`, read by `writers.lookup_finding_by_comment_id` (`reva/db/writers.py:905`) — and enqueues the new RQ job `worker.fix_tasks.run_finding_fix` with `job_timeout=REVIEW_JOB_TIMEOUT` (a headless-CLI run, same budget as a review). The worker runner (`worker/worker/fix_runner.py`) re-resolves the finding, applies the paid-path gates (PR open, fork guard, `RepoConfig.fix_command` opt-in read **fail-closed**, rolling budget), then reuses the review clone machinery: `ClaudeCodeRunner.repo_lock` + `ensure_repo(owner, name, head_sha, token)` (`reva/claude_code_runner.py:135/177` — blobless cached clone, token via transient `http.extraHeader` so it never lands in git config), a new **detached worktree** helper, and `ClaudeCodeRunner.review()` with a new flat skill `prompts/skills/reva-fix.md` (output contract: temp-JSON `{files_changed, summary}`; a new `allow_edit=True` parameter adds the CLI `Edit` tool — no new capability beyond the already-unscoped, cwd-confined `Write`). **Push mechanics found during planning:** clones authenticate via `-c http.extraHeader=Authorization: Basic <b64(x-access-token:TOKEN)>` on a token-less `https://github.com/{owner}/{name}` remote — the push reuses exactly that pattern (`git push origin HEAD:refs/heads/<branch>`, plain, never `--force`; a branch move between race-check and push is caught by git's own non-fast-forward rejection). **Three non-obvious mechanics this plan encodes:** (1) `review()`'s SECU-1 `_scrub_clone` deletes tracked `CLAUDE.md`/`.claude/`/`.mcp.json`/`AGENTS.md` from the working tree before every CLI run — they must be **restored from HEAD before staging** or the fix commit would delete them from the repo; (2) the worktree must live under the **system temp dir, not the repo cache** — `evict_stale_repos` treats every directory under an owner dir as a repo and its name validation rejects dot-names, so a leaked worktree there would crash eviction at worker startup; (3) gates run on `git add -A` + `git diff --cached --numstat` (staging first is what makes files the CLI *created* count toward the ≤ 5-file gate — an unstaged `git diff` misses untracked files). **Spec gap discovered during planning:** the spec is silent on fork PRs — the head branch of a fork PR lives in a repo the installation token cannot push to, so this plan adds a deterministic fork gate (abort + reply); Task 6 records it in the spec. RQ-retry idempotency is keyed on a `REVA-Fix-Command: <command comment id>` commit-message line: after `ensure_repo`'s fetch, `git log origin/<branch> --fixed-strings --grep=<marker> -n 1` finding a commit means this exact command already pushed — skip to the thread reply. `lookup_finding_by_comment_id` gains `line_end` (the ORM column `review_findings.line_end` exists since migration 001 — **no DB migration needed**, only the writer's SELECT).

**Tech Stack:** Python 3.14 (FastAPI webhook, RQ, pydantic `RepoConfig`, subprocess git ops, pytest per-service venvs with mocked subprocess + MagicMock context), headless Claude Code CLI via `reva/claude_code_runner.py`. No Messages-API path, no Go/TUI work, no new dependencies.

## Global Constraints

Five plans execute as one wave — these rules are verbatim coordination law:

- **`reva/types.py` `RepoConfig`: THIS plan adds ONLY `fix_command: bool = False` (OFF by default — locked decision) + its README config-table row.** Siblings add `commit_suggestions`, `describe_command`, `issue_plans` — merge-trivial, don't touch theirs. This plan anchors its field after `change_notes: bool = True` (line 86) precisely so it can't collide with the siblings' anchors.
- **`prompts/CHANGELOG.md`: top entry is currently v2.8 (unshipped). Wave rule: ALL five features share ONE v2.9 entry** — this plan's skill task must create the v2.9 entry if absent, otherwise append a bullet to the existing v2.9. **Never mint v2.10.** Coupling: `worker/tests/test_prompt_files.py:47` (`test_get_version_returns_current_version`) asserts the parsed version string — whichever plan creates v2.9 updates that assertion to `"v2.9"`; if it already says v2.9, leave it.
- **`api/app/routes/webhooks.py` is ALSO touched by the sibling /describe plan** (different handler: `_handle_issue_comment`, the `issue_comment` path) — keep this plan's changes scoped to the `pull_request_review_comment` path (`_handle_review_comment` + the new `_handle_fix_command`). `_process_delivery` line 111 already passes `rq_queue` to `_handle_review_comment` — no dispatch change needed.
- **No changes to `reva/odoo_contracts.py` or `contracts/`. No DB migrations** — verified: the comment→finding mapping needs none (`review_findings.line_end` already exists in the ORM model `reva/db/models.py:222` and the shipped SQL; only the writer's SELECT gains the column).
- **Ops-event invariant (CLAUDE.md):** every caught-and-degraded/abort path must log AND `writers.record_ops_event(...)` — this feature uses `component="fix_command"`; **every abort ALSO replies on the thread** (spec). The only reply-without-ops-event paths are the two non-degradations: the default-off "how to enable" reply and the idempotent "already ran" reply.
- **README GitHub-App setup table:** add the **Contents: Read & write** requirement note (operator action) — a push rejected for missing permission gets the spec'd thread reply + ops event, never silent.
- **Locked decisions (encode verbatim):** the fix is **committed directly to the PR branch** (single commit, message `REVA fix: <finding title>` + thread permalink, authored as the App bot); **never force-push**; head-SHA race guard (moved → abort + reply); **default OFF per repo**; the push deliberately triggers the normal debounce → **delta re-review of REVA's own fix commit** (do NOT suppress it — loop bound: the fix commit's webhook comes from the App's own bot, `/fix` only fires on a human reply, and `_is_bot_sender` already ignores bot replies).
- **Untrusted-content fencing invariant:** the finding text and operator guidance pass through `ClaudeCodeRunner.review()`'s per-call nonce fencing (SECU-6) unchanged; anything posted back to GitHub (fix summary, failure reasons) goes through `_redact_internal_paths`.
- **No `tui/` changes in this plan.**
- `reva/` is shared by all three services: final verification is `make test` + `worker/.venv/bin/ruff check reva worker/worker api/app scheduler/scheduler`.
- Per-service venvs: `cd worker && .venv/bin/python -m pytest tests/...` (same for `api/`).

---

### Task 0: Preconditions

**Files:** none (checks only)

- [ ] **Step 1: Verify the working tree, base commit, and spec**

```bash
cd /home/joseph/Projects/Cloudunify/cu_reva
git status --porcelain                       # expected: no output (clean tree)
git merge-base --is-ancestor 9079f99 HEAD && echo ANCESTOR_OK   # expected: ANCESTOR_OK
ls docs/superpowers/specs/2026-07-11-fix-command-design.md      # expected: the path prints
```

If any check fails, STOP and report — do not improvise a rebase. (A sibling wave plan may already be merged — that is fine; only *uncommitted* changes block.)

- [ ] **Step 2: Record the wave state of the shared files**

```bash
head -1 prompts/CHANGELOG.md      # note: v2.8 still on top, or a sibling's v2.9 already?
grep -n "fix_command\|commit_suggestions\|describe_command\|issue_plans" reva/types.py
grep -n "v2.8\|v2.9" worker/tests/test_prompt_files.py
```

Expected today: `## v2.8 — Issue-conformance verdicts`, no `fix_command` in `reva/types.py`, the version test asserting `"v2.8"` — but siblings may have landed first. Task 2 (RepoConfig) and Task 3 (CHANGELOG create-vs-append, version-assert bump) branch on what you see; record it.

---

### Task 1: `ClaudeCodeRunner` — fix worktree + git push machinery + `allow_edit`

**Files:**
- Modify: `reva/claude_code_runner.py` (branch validator after `_validate_head_sha`, lines 48–53; bot-identity constants after `_CLAUDE_BIN`, line 55; six new methods after `ensure_repo`, which ends at line 263; `review()` signature at lines 265–273 and the `allowed_tools` block at lines 329–333)
- Test: `worker/tests/test_claude_code_runner.py` (append at end of file, currently 1194 lines; reuse the `runner` fixture at line 41 and `_ok`/`_fail` helpers at lines 50–62; the `allow_edit` test mirrors `test_review_returns_claude_response` at line 405 with the `runner_with_skill` fixture at 385 and `_extract_output_path` at 397)

**Interfaces:**
- Consumes: existing `_run_git_permanent`, `_subprocess_env`, `_SCRUB_NAMES`, `_validate_head_sha`, `_GIT_TIMEOUT`.
- Produces (Task 4's runner calls all of these on `ctx.runner`):
  - `fix_worktree(self, repo_path: str, head_sha: str)` — context manager yielding the worktree path (str)
  - `restore_scrubbed(self, worktree: str) -> None`
  - `stage_and_diff_stats(self, worktree: str) -> list[tuple[str, int | None]]` — `(path, added+deleted)`; `None` lines = binary
  - `commit_fix(self, worktree: str, message: str) -> str` — returns the commit SHA
  - `push_fix(self, worktree: str, token: str, branch: str) -> None` — plain push, raises `PermanentError` with git's stderr snippet
  - `head_contains_marker(self, repo_path: str, branch: str, marker: str) -> bool`
  - `review(..., allow_edit: bool = False)` — `True` appends `Edit` to `--allowedTools`
  - module constants `FIX_COMMIT_AUTHOR = "reva[bot]"`, `FIX_COMMIT_EMAIL = "reva[bot]@users.noreply.github.com"`

- [ ] **Step 1: Write the failing tests**

Append to `worker/tests/test_claude_code_runner.py` (the file already imports `os`, `json`, `pytest`, `patch`, `MagicMock`, `ClaudeCodeRunner`, `PermanentError`, `TransientError` — add `FIX_COMMIT_AUTHOR, FIX_COMMIT_EMAIL` to the `reva.claude_code_runner` import line):

```python
# ---- /fix machinery: worktree, scrub-restore, stage/diff, commit, push --------


def test_fix_worktree_adds_detached_and_cleans_up(runner):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return _ok()

    with patch("subprocess.run", side_effect=fake_run):
        with runner.fix_worktree("/repos/acme/widgets", "a" * 40) as wt:
            parent = os.path.dirname(wt)
            assert os.path.isdir(parent)          # real mkdtemp parent exists
            assert os.path.basename(wt) == "wt"
            # NOT under the repo cache — a leaked dir there would crash eviction
            assert not wt.startswith(runner.repo_cache_dir)
    assert not os.path.exists(parent)             # rmtree'd in the finally
    prune, add, remove = calls[0], calls[1], calls[-1]
    assert prune[:5] == ["git", "-C", "/repos/acme/widgets", "worktree", "prune"]
    assert add[3:6] == ["worktree", "add", "--detach"]
    assert add[6] == wt and add[7] == "a" * 40
    assert remove[3:7] == ["worktree", "remove", "--force", wt]


def test_fix_worktree_removes_worktree_even_when_body_raises(runner):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return _ok()

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(RuntimeError):
            with runner.fix_worktree("/repos/acme/widgets", "b" * 40):
                raise RuntimeError("cli blew up")
    assert any("remove" in c for c in calls)      # cleanup ran despite the raise


def test_fix_worktree_rejects_unsafe_sha(runner):
    with patch("subprocess.run") as mock_run:
        with pytest.raises(PermanentError):
            with runner.fix_worktree("/repos/a/b", "--upload-pack=evil"):
                pass
    mock_run.assert_not_called()


def test_restore_scrubbed_checks_out_each_name_ignoring_failures(runner):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return _fail(code=1, stderr="pathspec did not match")  # repo doesn't track it

    with patch("subprocess.run", side_effect=fake_run):
        runner.restore_scrubbed("/wt")            # must not raise
    assert [c[-1] for c in calls] == list(ClaudeCodeRunner._SCRUB_NAMES)
    assert all(c[:4] == ["git", "-C", "/wt", "checkout"] for c in calls)


def test_stage_and_diff_stats_stages_all_then_parses_numstat(runner):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        m = _ok()
        if "diff" in args:
            m.stdout = "3\t1\tcustom_addons/x/models/foo.py\n-\t-\tstatic/img.png\n"
        return m

    with patch("subprocess.run", side_effect=fake_run):
        stats = runner.stage_and_diff_stats("/wt")
    assert calls[0][3:] == ["add", "-A"]          # stage FIRST so new files count
    assert "--cached" in calls[1] and "--numstat" in calls[1]
    assert stats == [("custom_addons/x/models/foo.py", 4), ("static/img.png", None)]


def test_commit_fix_uses_bot_identity_no_verify_and_returns_sha(runner):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        m = _ok()
        m.stdout = "abc123def\n" if "rev-parse" in args else ""
        return m

    with patch("subprocess.run", side_effect=fake_run):
        sha = runner.commit_fix("/wt", "REVA fix: t\n\nFinding: url\nREVA-Fix-Command: 9001")
    assert sha == "abc123def"
    commit = calls[0]
    assert "commit" in commit and "--no-verify" in commit and "-m" in commit
    assert f"user.name={FIX_COMMIT_AUTHOR}" in commit
    assert f"user.email={FIX_COMMIT_EMAIL}" in commit


def test_push_fix_plain_push_with_transient_auth_header(runner):
    with patch("subprocess.run", return_value=_ok()) as mock_run:
        runner.push_fix("/wt", "sekret-token", "feat/foo")
    args = mock_run.call_args[0][0]
    assert args[-2:] == ["origin", "HEAD:refs/heads/feat/foo"]
    assert "push" in args
    assert "--force" not in args and "-f" not in args   # locked decision: never force
    joined = " ".join(args)
    assert "http.extraHeader=Authorization: Basic" in joined
    assert "sekret-token" not in joined                 # token only inside the b64 header


def test_push_fix_rejects_unsafe_branch(runner):
    with patch("subprocess.run") as mock_run:
        with pytest.raises(PermanentError):
            runner.push_fix("/wt", "tok", "-evil")
    mock_run.assert_not_called()


def test_head_contains_marker_true_on_hit_false_on_miss(runner):
    hit = _ok()
    hit.stdout = "deadbeef\n"
    miss = _ok()
    miss.stdout = ""
    with patch("subprocess.run", return_value=hit) as mock_run:
        assert runner.head_contains_marker("/repos/a/b", "feat/foo", "REVA-Fix-Command: 9001")
    args = mock_run.call_args[0][0]
    assert "--fixed-strings" in args and "--grep=REVA-Fix-Command: 9001" in args
    assert args[-1] == "origin/feat/foo"
    with patch("subprocess.run", return_value=miss):
        assert not runner.head_contains_marker("/repos/a/b", "feat/foo", "REVA-Fix-Command: 9001")


def test_review_allow_edit_appends_edit_tool(runner_with_skill, tmp_path):
    repo_path = str(tmp_path / "repo")
    os.makedirs(repo_path)
    captured = {}

    def fake_run(args, **kwargs):
        captured["argv"] = args
        out_path = _extract_output_path(kwargs["input"])
        with open(out_path, "w") as f:
            json.dump({"files_changed": ["a.py"], "summary": "s"}, f)
        return _ok()

    with patch("subprocess.run", side_effect=fake_run):
        runner_with_skill.review(
            repo_path=repo_path, skill="reva-diff-review",
            params={"finding": "x"}, allow_edit=True,
        )
    argv = captured["argv"]
    assert argv[argv.index("--allowedTools") + 1] == "Read,Grep,Glob,Write,Edit"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd worker && .venv/bin/python -m pytest tests/test_claude_code_runner.py -k "fix_worktree or restore_scrubbed or stage_and_diff or commit_fix or push_fix or contains_marker or allow_edit" -v`
Expected: import-time failure first (`ImportError: cannot import name 'FIX_COMMIT_AUTHOR'`); after temporarily stubbing nothing, the methods fail with `AttributeError: 'ClaudeCodeRunner' object has no attribute 'fix_worktree'` and `test_review_allow_edit_appends_edit_tool` with `TypeError: review() got an unexpected keyword argument 'allow_edit'`. The pre-existing `test_review_cli_flags`-style test at line 532 (`allowedTools == "Read,Grep,Glob,Write"`) must stay green throughout.

- [ ] **Step 3: Implement**

`reva/claude_code_runner.py` — after `_validate_head_sha` (line 53), add:

```python
# PR head-branch names are passed to git push/log refspecs. Same shape as the
# repo components but "/" is allowed (feature/x); git refs can't start with "-"
# (blocks option injection) or contain "..".
_SAFE_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


def _validate_branch(branch: str) -> None:
    if not _SAFE_BRANCH.match(branch or "") or ".." in branch:
        raise PermanentError(f"unsafe branch name: {branch!r}")
```

After `_CLAUDE_BIN = "claude"` (line 55), add:

```python
# Commit identity for /fix commits (display attribution; the push itself is
# authenticated by the installation token, so GitHub records the App as the
# pusher regardless).
FIX_COMMIT_AUTHOR = "reva[bot]"
FIX_COMMIT_EMAIL = "reva[bot]@users.noreply.github.com"
```

After `ensure_repo` (line 263), before `def review`, add the six methods:

```python
    # --------------------------------------------------------- /fix machinery

    @contextmanager
    def fix_worktree(self, repo_path: str, head_sha: str):
        """Throwaway detached worktree at head_sha for the /fix job.

        Lives under the system temp dir, NOT the repo cache: evict_stale_repos
        treats every directory under an owner dir as a repo (and repo_lock's
        component validation rejects dot-names), so a leaked worktree there
        would crash eviction at worker startup. Objects are shared with the
        cached clone, and ensure_repo(head_sha) already materialized the blobs
        at this SHA, so creation needs no network. `worktree prune` on entry
        clears metadata a previously crashed job left behind; cleanup in the
        finally is best-effort (a leaked temp dir is bounded by the container
        lifetime) and must never mask the body's real error.
        """
        _validate_head_sha(head_sha)
        self._run_git_permanent(["-C", repo_path, "worktree", "prune"])
        parent = tempfile.mkdtemp(prefix="reva-fix-")
        worktree = os.path.join(parent, "wt")
        try:
            self._run_git_permanent(
                ["-C", repo_path, "worktree", "add", "--detach", worktree, head_sha]
            )
            yield worktree
        finally:
            try:
                subprocess.run(
                    ["git", "-C", repo_path, "worktree", "remove", "--force", worktree],
                    capture_output=True, text=True, timeout=_GIT_TIMEOUT,
                    env=self._subprocess_env(),
                )
            except Exception:  # noqa: BLE001 — cleanup must never mask the real error
                logger.warning("fix_worktree_remove_failed", worktree=worktree,
                               exc_info=True)
            shutil.rmtree(parent, ignore_errors=True)

    def restore_scrubbed(self, worktree: str) -> None:
        """Undo _scrub_clone's deletions before the /fix diff is staged.

        The scrub (SECU-1) deletes tracked CLAUDE.md/.claude/… from the working
        tree before every CLI run; left un-restored they would enter the fix
        diff as deletions and get COMMITTED to the PR branch. checkout restores
        each name from HEAD; a non-zero exit just means the repo doesn't track
        that name — ignored by design (nothing degrades, no ops event)."""
        for name in self._SCRUB_NAMES:
            try:
                subprocess.run(
                    ["git", "-C", worktree, "checkout", "--", name],
                    capture_output=True, text=True, timeout=_GIT_TIMEOUT,
                    env=self._subprocess_env(),
                )
            except subprocess.TimeoutExpired as exc:
                raise TransientError(f"git checkout {name} timed out") from exc

    def stage_and_diff_stats(self, worktree: str) -> list[tuple[str, int | None]]:
        """`git add -A` then `git diff --cached --numstat` → [(path, changed_lines)].

        changed_lines = added + deleted; None for binary changes (numstat "-").
        Staging FIRST is what makes files the CLI created count toward the /fix
        gates — an unstaged working-tree diff misses untracked files entirely."""
        self._run_git_permanent(["-C", worktree, "add", "-A"])
        result = subprocess.run(
            ["git", "-C", worktree, "diff", "--cached", "--numstat"],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT,
            env=self._subprocess_env(),
        )
        if result.returncode != 0:
            raise PermanentError(f"git diff failed: {result.stderr[:200]}")
        stats: list[tuple[str, int | None]] = []
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            added, deleted, path = parts
            if added == "-" or deleted == "-":
                stats.append((path, None))
            else:
                stats.append((path, int(added) + int(deleted)))
        return stats

    def commit_fix(self, worktree: str, message: str) -> str:
        """Commit the staged fix as the REVA bot; returns the commit SHA.

        --no-verify: never execute hooks around a commit built from CLI-edited
        content. Identity is passed per-command (-c), never written to config."""
        self._run_git_permanent([
            "-C", worktree,
            "-c", f"user.name={FIX_COMMIT_AUTHOR}",
            "-c", f"user.email={FIX_COMMIT_EMAIL}",
            "commit", "--no-verify", "-m", message,
        ])
        result = subprocess.run(
            ["git", "-C", worktree, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT,
            env=self._subprocess_env(),
        )
        if result.returncode != 0:
            raise PermanentError(f"git rev-parse failed: {result.stderr[:200]}")
        return result.stdout.strip()

    def push_fix(self, worktree: str, token: str, branch: str) -> None:
        """Plain (NEVER force) push of the worktree HEAD to the PR branch.

        Auth mirrors ensure_repo: transient http.extraHeader on the token-less
        remote, so the token never lands in git config. Raises PermanentError
        carrying git's stderr snippet — the caller classifies permission (403)
        vs. non-fast-forward (branch moved) for the thread reply."""
        _validate_branch(branch)
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        self._run_git_permanent([
            "-c", f"http.extraHeader=Authorization: Basic {basic}",
            "-C", worktree, "push", "origin", f"HEAD:refs/heads/{branch}",
        ])

    def head_contains_marker(self, repo_path: str, branch: str, marker: str) -> bool:
        """True when a commit on origin/<branch> carries `marker` in its message.

        RQ-retry idempotency for /fix: ensure_repo just fetched origin, so the
        branch tip is local — a hit means this exact command already pushed."""
        _validate_branch(branch)
        result = subprocess.run(
            ["git", "-C", repo_path, "log", "--fixed-strings", f"--grep={marker}",
             "--format=%H", "-n", "1", f"origin/{branch}"],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT,
            env=self._subprocess_env(),
        )
        return result.returncode == 0 and bool(result.stdout.strip())
```

`review()` — add the parameter (signature, lines 265–273):

```python
    def review(
        self,
        repo_path: str,
        skill: str,
        params: dict,
        model: str | None = None,
        odoo: bool = False,
        extra_dirs: list[str] | None = None,
        allow_edit: bool = False,
    ) -> ClaudeResponse:
```

and replace the `allowed_tools` block (lines 329–333):

```python
        allowed_tools = "Read,Grep,Glob,Write"
        if allow_edit:
            # /fix edits repo files in a throwaway worktree. Edit adds no
            # capability beyond the already-unscoped Write (both confined to
            # the cwd workspace boundary) but yields surgical in-place changes
            # instead of whole-file rewrites.
            allowed_tools += ",Edit"
        mcp_args: list[str] = []
        if mcp_config_path:
            mcp_args = ["--mcp-config", mcp_config_path]
            allowed_tools += ",mcp__codegraph__*"
```

(The `+=` for the MCP suffix produces byte-identical strings for every existing call — the tests at lines 532/1034/1045/1062 stay green.)

- [ ] **Step 4: Run the runner suite**

Run: `cd worker && .venv/bin/python -m pytest tests/test_claude_code_runner.py -q`
Expected: all PASS, including every pre-existing ensure_repo/review/codegraph test.

- [ ] **Step 5: Commit**

```bash
git add reva/claude_code_runner.py worker/tests/test_claude_code_runner.py
git commit -m "feat(runner): /fix worktree + commit/push machinery, Edit tool opt-in"
```

---

### Task 2: Shared-lib plumbing — `line_end` on the finding lookup + `RepoConfig.fix_command` + README config row

**Files:**
- Modify: `reva/db/writers.py:905-933` (`lookup_finding_by_comment_id` — add `line_end` to the SELECT and result dict)
- Modify: `reva/types.py` (`RepoConfig`, insert after `change_notes: bool = True`, line 86 — deliberate anchor away from the siblings' fields)
- Modify: `README.md:133` (per-repo config table — add the row after the `verify_findings` row)
- Test: `worker/tests/test_db.py` (after `test_lookup_finding_by_comment_id_returns_review_run_id`, line 1235; reuse `_seed_findings` at line 1166), `worker/tests/test_review_tool.py` (append after `test_repo_config_work_status_defaults_on`, line 46)

**Interfaces:**
- Produces: `writers.lookup_finding_by_comment_id(db, github_comment_id) -> dict | None` now includes `"line_end": int | None` (purely additive — the existing consumers in `api/app/routes/webhooks.py`, `worker/worker/reply_runner.py`, and `worker/tests/test_runner.py:921` read named keys and are unaffected). `RepoConfig.fix_command: bool = False` — Task 4's `_load_repo_config` reads it.

- [ ] **Step 1: Write the failing tests**

`worker/tests/test_db.py`, after line 1235:

```python
def test_lookup_finding_by_comment_id_includes_line_range(db, seeded):
    # /fix feeds the full line anchor to the fix skill; _seed_findings sets
    # line_start=line_end=1 on every seeded finding.
    [fid] = _seed_findings(db, seeded, 1)
    writers.attach_finding_comment_ids(db, {fid: 556})
    found = writers.lookup_finding_by_comment_id(db, 556)
    assert found["line_start"] == 1
    assert found["line_end"] == 1
```

`worker/tests/test_review_tool.py`, after `test_repo_config_work_status_defaults_on` (line 46):

```python
def test_repo_config_fix_command_defaults_off():
    # Locked decision (spec 2026-07-11): /fix writes to PR branches, so it is
    # the one command that defaults OFF — repos opt in via .claude-review.yml.
    assert RepoConfig().fix_command is False
    assert RepoConfig.model_validate({"fix_command": True}).fix_command is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd worker && .venv/bin/python -m pytest tests/test_db.py -k line_range -v tests/test_review_tool.py -k fix_command -v`
Expected: `test_lookup_finding_by_comment_id_includes_line_range` FAILS with `KeyError: 'line_end'`; `test_repo_config_fix_command_defaults_off` FAILS with `AttributeError: 'RepoConfig' object has no attribute 'fix_command'`.

- [ ] **Step 3: Implement**

`reva/db/writers.py` — replace the body of `lookup_finding_by_comment_id` (lines 905–933) with:

```python
def lookup_finding_by_comment_id(db: Database, github_comment_id: int) -> dict | None:
    """Return finding details for a given github_comment_id, or None."""
    with db.session() as s:
        row = s.execute(
            select(
                ReviewFinding.id,
                ReviewFinding.severity,
                ReviewFinding.title,
                ReviewFinding.body,
                ReviewFinding.file_path,
                ReviewFinding.line_start,
                ReviewFinding.line_end,
                ReviewFinding.suggestion,
                ReviewFinding.review_run_id,
                ReviewFinding.category,
            ).where(ReviewFinding.github_comment_id == github_comment_id)
        ).first()
    if row is None:
        return None
    return {
        "id": row[0],
        "severity": row[1],
        "title": row[2],
        "body": row[3],
        "file_path": row[4],
        "line_start": row[5],
        "line_end": row[6],
        "suggestion": row[7],
        "review_run_id": row[8],
        "category": row[9],
    }
```

`reva/types.py` — in `RepoConfig`, directly after `change_notes: bool = True` (line 86):

```python
    # Opt-in for the /fix command — the ONE command that writes to PR branches
    # (a single fix commit, never force-pushed). Default OFF (locked decision,
    # spec 2026-07-11); the GitHub App additionally needs Contents: Read & write
    # for the push.
    fix_command: bool = False
```

`README.md` — in the per-repo config table, after the `verify_findings` row (line 133):

```markdown
| `fix_command` | `false` | Allow `/fix` replies to push a single fix commit to the PR branch (opt-in; needs App **Contents: Read & write**). |
```

- [ ] **Step 4: Run the covering suites**

Run: `cd worker && .venv/bin/python -m pytest tests/test_db.py tests/test_review_tool.py tests/test_comment_reply.py tests/test_runner.py -q`
Expected: all PASS (the lookup change is additive; reply/feedback consumers read named keys).

- [ ] **Step 5: Commit**

```bash
git add reva/db/writers.py reva/types.py README.md worker/tests/test_db.py worker/tests/test_review_tool.py
git commit -m "feat(config): fix_command opt-in (default off); finding lookup carries line_end"
```

---

### Task 3: `prompts/skills/reva-fix.md` + prompt tests + CHANGELOG v2.9

**Files:**
- Create: `prompts/skills/reva-fix.md` (a FLAT file — skills are single markdown files read by `ClaudeCodeRunner._read_skill`, not directories)
- Modify: `prompts/CHANGELOG.md` (line 1 — wave-shared v2.9 entry, see Global Constraints)
- Modify: `worker/tests/test_prompt_files.py` (`test_get_version_returns_current_version` line 47 — conditional; `test_summary_contract_present` lines 140–145 — exclude the fix skill; new contract test after `test_intent_check_in_skill_output_contracts`, line 137)

**Interfaces:**
- Consumes: `ClaudeCodeRunner.review()` reads `skills_dir/reva-fix.md` raw and appends nonce-fenced Task Parameters + `output_path`; `_build_preamble` prepends `review_guidance.md` (+ `odoo19.md` when the repo opts in) exactly as for review skills.
- Produces: the skill's output contract — Claude writes `{"files_changed": [...], "summary": "..."}` to `output_path` — which Task 4 parses from `ClaudeResponse.tool_use_input`.

**Glob-test constraints on any new skill file (verified against the current suite):** `test_summary_contract_present` iterates `reva-*.md` asserting the literal `Summary contract` — the fix skill is not a review and must be excluded there (edited below); `test_verify_before_write_guidance_present` asserts `confidence ≥ 0.7` appears in NO skill; `test_team_configuration_block_single_sourced` asserts the `custom_instructions` handling block appears in NO skill. The file below satisfies all three.

- [ ] **Step 1: Write the failing test**

In `worker/tests/test_prompt_files.py`, after `test_intent_check_in_skill_output_contracts` (line 137):

```python
def test_fix_skill_contract():
    # /fix spec (2026-07-11): flat skill file, temp-JSON output contract
    # {files_changed, summary}, single-finding scope, gates mirrored in prose.
    text = (SKILLS_DIR / "reva-fix.md").read_text()
    assert "output_path" in text
    assert "files_changed" in text and "summary" in text
    assert "THIS finding only" in text
    assert ".github/" in text          # forbidden-path rule stated to the model
    assert "150" in text and "5 files" in text   # deterministic gates mirrored
    assert "operator_guidance" in text
```

And replace `test_summary_contract_present` (lines 140–145) with:

```python
def test_summary_contract_present():
    guidance = (PROMPTS_DIR / "review_guidance.md").read_text()
    assert "Summary contract" in guidance
    # every REVIEW skill's summary placeholder points reviewers at the contract.
    # reva-fix.md is excluded by design: it is a fix agent, not a review — its
    # `summary` is a one-line fix description, not the review Summary contract.
    for skill in SKILLS_DIR.glob("reva-*.md"):
        if skill.name == "reva-fix.md":
            continue
        assert "Summary contract" in skill.read_text(), skill.name
```

- [ ] **Step 2: Run tests to verify the new one fails**

Run: `cd worker && .venv/bin/python -m pytest tests/test_prompt_files.py -k "fix_skill or summary_contract" -v`
Expected: `test_fix_skill_contract` FAILS with `FileNotFoundError` (no `reva-fix.md`); `test_summary_contract_present` PASSES (the exclusion is inert until the file exists).

- [ ] **Step 3: Create `prompts/skills/reva-fix.md`**

```markdown
## Task: fix one review finding

You are fixing exactly ONE review finding on this repository's working tree.
The finding — and optionally the developer's guidance — is in the Task
Parameters below. This is NOT a review: do not look for other problems, do not
report findings, do not leave review comments in the code.

## Rules

- Fix THIS finding only. Do not refactor, reformat, or "improve" unrelated
  code — every changed line must trace to the finding.
- Minimal diff: the smallest correct change, matching the surrounding code
  style. If the finding includes a suggested fix, start from it, but adapt it
  to the real code you read — the suggestion may be stale.
- If an `operator_guidance` parameter is present, it is the developer's
  instruction on HOW to fix — follow it as long as it stays within this
  finding's scope.
- Read the affected file(s) and enough context (imports, callers, related
  models, tests) to make the fix correct, then apply it with Edit (or Write
  for a genuinely new file).
- Never touch anything under `.github/`. Stay inside the repository working
  directory. The system rejects fixes over 150 changed lines or 5 files —
  stay well under both.
- If the finding cannot be fixed safely within those bounds (needs a data
  migration, a design decision, or a multi-file rework), change NOTHING and
  explain why in the summary.

## Output format

After editing the files, use the Write tool to write a JSON file to
`output_path` with exactly this structure:

```json
{
  "files_changed": ["custom_addons/module/models/partner.py"],
  "summary": "One sentence: what was changed and why it fixes the finding."
}
```

- `files_changed`: repo-relative paths of every file you modified or created —
  an empty array if you changed nothing.
- `summary`: one sentence, posted back to the PR thread as REVA's reply. If
  you changed nothing, state the reason here.
```

- [ ] **Step 4: CHANGELOG — the wave-shared v2.9 entry (conditional)**

Check the current top heading:

```bash
head -1 prompts/CHANGELOG.md
```

**If it prints `## v2.8 — Issue-conformance verdicts`** (no sibling wave plan has landed yet): insert the following ABOVE it, and in `worker/tests/test_prompt_files.py:47` change `assert builder.get_version() == "v2.8"` to `assert builder.get_version() == "v2.9"`:

```markdown
## v2.9 — 2026-07-11 feature wave

- reva-fix.md: new headless-CLI skill for the `/fix` command — fixes exactly
  one REVA finding in a throwaway PR-branch worktree (minimal diff, optional
  operator guidance, Edit tool enabled); output contract
  `{files_changed, summary}`. The commit lands directly on the PR branch
  behind the per-repo `fix_command` opt-in (default off).

```

**If it already prints a `## v2.9 — ...` heading** (a sibling plan created it): append ONLY the bullet above (the `- reva-fix.md: …` list item, without the heading) to the existing v2.9 entry's bullet list, and verify `worker/tests/test_prompt_files.py:47` already asserts `"v2.9"` — leave it alone. **Never create a v2.10 heading.**

- [ ] **Step 5: Run the prompt suite**

Run: `cd worker && .venv/bin/python -m pytest tests/test_prompt_files.py -v`
Expected: all PASS — including `test_get_version_returns_current_version` (v2.9), the new `test_fix_skill_contract`, the edited `test_summary_contract_present`, and the untouched glob tests (`test_verify_before_write_guidance_present`, `test_team_configuration_block_single_sourced`) now also sweeping `reva-fix.md`.

- [ ] **Step 6: Commit**

```bash
git add prompts/skills/reva-fix.md prompts/CHANGELOG.md worker/tests/test_prompt_files.py
git commit -m "feat(prompts): reva-fix skill for the /fix command (prompts v2.9)"
```

---

### Task 4: Worker job — `fix_runner` + `fix_tasks`

**Files:**
- Create: `worker/worker/fix_runner.py`
- Create: `worker/worker/fix_tasks.py` (mirror `worker/worker/change_note_tasks.py` exactly)
- Test: `worker/tests/test_fix_runner.py` (new; DB/ctx fixture style from `worker/tests/test_comment_reply.py`)

**Interfaces:**
- Consumes: Task 1 runner methods + `repo_lock`/`ensure_repo`/`review(allow_edit=True)`; Task 2 `lookup_finding_by_comment_id` (with `line_end`) + `RepoConfig.fix_command`; Task 3 skill `reva-fix`; `worker.runner.budget_exceeded(ctx) -> float | None` (line 353) and `get_context()`; `reva.cost.estimate_cost(model, in, out, cache_read, cache_write) -> float`; `reva.review_formatter._redact_internal_paths(msg) -> str`; `writers.record_claude_spend(db, kind, cost)`, `writers.record_ops_event(db, component, severity, event, detail)`; `GitHubClient.get_pull_request(token, owner, repo, pr_number) -> dict` (raw PR: `state`, `merged`, `head.{sha,ref,repo.full_name}`, `base.repo.full_name`), `GitHubClient.get_file_content(token, owner, repo, path, ref) -> str | None`, `GitHubClient.reply_to_review_comment(token=, owner=, repo=, pr_number=, comment_id=, body=) -> int`; `worker.task_contract.terminal_on_permanent`.
- Produces: RQ entrypoint **`worker.fix_tasks.run_finding_fix`** taking `{"installation_id": int, "owner": str, "repo": str, "pr_number": int, "finding_comment_id": int, "finding_id": int, "command_comment_id": int | None, "head_sha": str, "guidance": str}` and returning a status dict — Task 5's api enqueue targets this dotted path. Spend kind: `"finding_fix"`. Ops component: `"fix_command"`.

- [ ] **Step 1: Write the failing tests**

Create `worker/tests/test_fix_runner.py`:

```python
"""run_finding_fix: opt-in gate, budget, validation gates, race guard, push,
retry idempotency, failure classification (spec 2026-07-11)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from reva.db import Base, Database, create_engine_from_url, writers
from reva.db.models import ClaudeSpend, OpsEvent, ReviewFinding, ReviewRun
from reva.errors import PermanentError, TransientError
from reva.types import ClaudeResponse
from worker.fix_runner import run_finding_fix
from worker.runner import WorkerContext, set_context

_FINDING_COMMENT_ID = 777
_COMMAND_COMMENT_ID = 9001
_MARKER = f"REVA-Fix-Command: {_COMMAND_COMMENT_ID}"


@pytest.fixture()
def db():
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    d = Database(engine)
    repo_id = writers.upsert_repository(
        d, github_repository_id=1, owner="acme", name="widgets",
        default_branch="main", installation_id=500,
    )
    pr_id = writers.upsert_pull_request(
        d, repository_id=repo_id, github_pr_id=9001, pr_number=42, title="t",
        author_login="alice", base_branch="main", head_branch="feat/foo",
        head_sha="deadbeef", state="open", draft=False,
    )
    with d.session() as s:
        run = ReviewRun(repository_id=repo_id, pull_request_id=pr_id,
                        head_sha="deadbeef", status="completed",
                        trigger_event="opened", review_mode="diff")
        s.add(run)
        s.flush()
        s.add(ReviewFinding(
            review_run_id=run.id, severity="major", category="bug",
            file_path="custom_addons/x/models/foo.py", line_start=10, line_end=12,
            title="Null deref", body="user may be None",
            suggestion="guard with `if user:`",
            github_comment_id=_FINDING_COMMENT_ID, posted_to_github=True,
        ))
    return d


def _pr(state="open", merged=False, head_sha="deadbeef", head_repo="acme/widgets"):
    return {
        "number": 42, "state": state, "merged": merged,
        "head": {"sha": head_sha, "ref": "feat/foo",
                 "repo": {"full_name": head_repo}},
        "base": {"repo": {"full_name": "acme/widgets"}},
    }


def _ctx(db, *, budget=None, config_yaml="fix_command: true\n", pr=None,
         stats=None, tool_output=None, marker_hit=False):
    runner = MagicMock()
    runner.repo_lock.return_value.__enter__.return_value = None
    runner.repo_lock.return_value.__exit__.return_value = False
    runner.ensure_repo.return_value = "/repos/acme/widgets"
    runner.head_contains_marker.return_value = marker_hit
    runner.fix_worktree.return_value.__enter__.return_value = "/tmp/reva-fix-x/wt"
    runner.fix_worktree.return_value.__exit__.return_value = False
    runner.review.return_value = ClaudeResponse(
        model="claude-sonnet-5",
        tool_use_input=tool_output if tool_output is not None else
            {"files_changed": ["custom_addons/x/models/foo.py"],
             "summary": "Guarded the None case."},
        total_cost_usd=0.25,
    )
    runner.stage_and_diff_stats.return_value = (
        stats if stats is not None else [("custom_addons/x/models/foo.py", 8)]
    )
    runner.commit_fix.return_value = "fix1234"
    github = MagicMock()
    github.get_installation_token.return_value = "tok"
    github.get_pull_request.return_value = pr if pr is not None else _pr()
    github.get_file_content.return_value = config_yaml
    ctx = WorkerContext(
        db=db, claude=MagicMock(), runner=runner, github=github,  # type: ignore[arg-type]
        reviewer=None, auditor=None, ticket_analyzer=None, verifier=None,  # type: ignore[arg-type]
        daily_budget_usd=budget,
    )
    set_context(ctx)
    return ctx


def _params(**over):
    p = {
        "installation_id": 500, "owner": "acme", "repo": "widgets",
        "pr_number": 42, "finding_comment_id": _FINDING_COMMENT_ID,
        "finding_id": 1, "command_comment_id": _COMMAND_COMMENT_ID,
        "head_sha": "deadbeef", "guidance": "use the ORM write method",
    }
    p.update(over)
    return p


def _reply_body(ctx):
    return ctx.github.reply_to_review_comment.call_args.kwargs["body"]


def _ops_events(db, **filters):
    with db.session() as s:
        return s.query(OpsEvent).filter_by(component="fix_command", **filters).all()


# --- happy path -----------------------------------------------------------------


def test_success_pushes_single_commit_and_replies_with_link(db):
    ctx = _ctx(db)
    result = run_finding_fix(_params())

    assert result == {"status": "completed", "commit_sha": "fix1234", "cost_usd": 0.25}
    # CLI ran the fix skill in the worktree with Edit enabled
    kwargs = ctx.runner.review.call_args.kwargs
    assert kwargs["repo_path"] == "/tmp/reva-fix-x/wt"
    assert kwargs["skill"] == "reva-fix"
    assert kwargs["allow_edit"] is True
    assert "Null deref" in kwargs["params"]["finding"]
    assert kwargs["params"]["operator_guidance"] == "use the ORM write method"
    # locked format: title + thread permalink + idempotency marker
    msg = ctx.runner.commit_fix.call_args.args[1]
    assert msg.startswith("REVA fix: Null deref")
    assert f"#discussion_r{_FINDING_COMMENT_ID}" in msg
    assert _MARKER in msg
    # plain push to the PR branch, scrub restored before staging
    ctx.runner.restore_scrubbed.assert_called_once_with("/tmp/reva-fix-x/wt")
    ctx.runner.push_fix.assert_called_once_with("/tmp/reva-fix-x/wt", "tok", "feat/foo")
    # thread reply: summary + commit link
    body = _reply_body(ctx)
    assert "Guarded the None case." in body
    assert "https://github.com/acme/widgets/commit/fix1234" in body
    # spend ledgered under the spec'd kind
    with db.session() as s:
        assert s.query(ClaudeSpend).filter_by(kind="finding_fix").count() == 1


def test_empty_guidance_omits_the_param(db):
    ctx = _ctx(db)
    run_finding_fix(_params(guidance=""))
    assert "operator_guidance" not in ctx.runner.review.call_args.kwargs["params"]


# --- gating: opt-in, config errors, budget, PR state ------------------------------


def test_default_off_replies_how_to_enable_and_makes_no_paid_call(db):
    ctx = _ctx(db, config_yaml=None)          # no .claude-review.yml at all
    result = run_finding_fix(_params())
    assert result["status"] == "disabled"
    ctx.runner.review.assert_not_called()
    ctx.runner.ensure_repo.assert_not_called()
    assert "fix_command: true" in _reply_body(ctx)


def test_explicit_false_is_also_disabled(db):
    ctx = _ctx(db, config_yaml="fix_command: false\n")
    assert run_finding_fix(_params())["status"] == "disabled"
    ctx.runner.review.assert_not_called()


def test_config_fetch_error_fails_closed_with_reply_and_ops_event(db):
    ctx = _ctx(db)
    ctx.github.get_file_content.side_effect = RuntimeError("contents API down")
    result = run_finding_fix(_params())
    assert result == {"status": "aborted", "reason": "config_fetch_failed"}
    ctx.runner.review.assert_not_called()
    ctx.github.reply_to_review_comment.assert_called_once()
    assert _ops_events(db, event="config_fetch_failed")


def test_over_budget_replies_and_records_ops_event(db):
    writers.record_claude_spend(db, "review", 50.0)
    ctx = _ctx(db, budget=10.0)
    result = run_finding_fix(_params())
    assert result == {"status": "aborted", "reason": "declined_budget"}
    ctx.runner.review.assert_not_called()
    assert "budget" in _reply_body(ctx).lower()
    assert _ops_events(db, event="declined_budget")


def test_closed_pr_aborts_before_clone(db):
    ctx = _ctx(db, pr=_pr(state="closed"))
    result = run_finding_fix(_params())
    assert result == {"status": "aborted", "reason": "pr_not_open"}
    ctx.runner.ensure_repo.assert_not_called()
    assert _ops_events(db, event="pr_not_open")


def test_fork_pr_aborts(db):
    ctx = _ctx(db, pr=_pr(head_repo="fork-owner/widgets"))
    result = run_finding_fix(_params())
    assert result == {"status": "aborted", "reason": "fork_pr_unsupported"}
    ctx.runner.ensure_repo.assert_not_called()
    assert _ops_events(db, event="fork_pr_unsupported")


def test_unknown_finding_records_ops_event(db):
    ctx = _ctx(db)
    result = run_finding_fix(_params(finding_comment_id=424242))
    assert result == {"status": "aborted", "reason": "finding_not_found"}
    ctx.runner.review.assert_not_called()
    assert _ops_events(db, event="finding_not_found")


# --- validation gates (spec: each aborts with reply + ops event, nothing pushed) ---


@pytest.mark.parametrize("stats,fragment", [
    ([], "empty diff"),
    ([(f"f{i}.py", 1) for i in range(6)], "6 files"),
    ([("a.py", 151)], "151 changed lines"),
    ([(".github/workflows/ci.yml", 2)], ".github/"),
    ([("static/img.png", None)], "binary"),
])
def test_validation_gates_abort_with_reply_and_ops_event(db, stats, fragment):
    ctx = _ctx(db, stats=stats)
    result = run_finding_fix(_params())
    assert result == {"status": "aborted", "reason": "gate_failed"}
    ctx.runner.commit_fix.assert_not_called()
    ctx.runner.push_fix.assert_not_called()
    assert fragment in _reply_body(ctx)
    assert _ops_events(db, event="gate_failed")


# --- race guard + retry idempotency ------------------------------------------------


def test_stale_command_head_aborts_before_paid_call(db):
    # PR head at job time differs from head at command time → branch moved.
    ctx = _ctx(db, pr=_pr(head_sha="1111beef"))
    result = run_finding_fix(_params())
    assert result == {"status": "aborted", "reason": "head_moved"}
    ctx.runner.review.assert_not_called()
    ctx.runner.push_fix.assert_not_called()


def test_head_moved_between_cli_and_push_aborts(db):
    ctx = _ctx(db)
    ctx.github.get_pull_request.side_effect = [_pr(), _pr(head_sha="2222beef")]
    result = run_finding_fix(_params())
    assert result == {"status": "aborted", "reason": "head_moved"}
    ctx.runner.review.assert_called_once()     # paid call happened; push must not
    ctx.runner.commit_fix.assert_not_called()
    ctx.runner.push_fix.assert_not_called()


def test_retry_after_push_is_idempotent(db):
    # RQ retry after a successful push: the branch already contains the marker.
    ctx = _ctx(db, marker_hit=True)
    result = run_finding_fix(_params())
    assert result == {"status": "already_applied"}
    ctx.runner.head_contains_marker.assert_called_once_with(
        "/repos/acme/widgets", "feat/foo", _MARKER)
    ctx.runner.review.assert_not_called()
    ctx.runner.push_fix.assert_not_called()
    assert "already" in _reply_body(ctx)


# --- push failure classification ---------------------------------------------------


def test_push_403_replies_permission_hint_with_error_ops_event(db):
    ctx = _ctx(db)
    ctx.runner.push_fix.side_effect = PermanentError(
        "git push failed: The requested URL returned error: 403")
    result = run_finding_fix(_params())
    assert result == {"status": "aborted", "reason": "push_rejected_permission"}
    assert "Contents: Read & write" in _reply_body(ctx)
    [ev] = _ops_events(db, event="push_rejected_permission")
    assert ev.severity == "error"


def test_push_non_fast_forward_is_branch_moved(db):
    ctx = _ctx(db)
    ctx.runner.push_fix.side_effect = PermanentError(
        "git push failed: ! [rejected] feat/foo -> feat/foo (fetch first)")
    result = run_finding_fix(_params())
    assert result == {"status": "aborted", "reason": "head_moved"}


# --- CLI failure paths --------------------------------------------------------------


def test_no_summary_aborts_but_spend_is_recorded(db):
    ctx = _ctx(db, tool_output={"files_changed": [], "summary": ""})
    result = run_finding_fix(_params())
    assert result == {"status": "aborted", "reason": "bad_cli_output"}
    ctx.runner.push_fix.assert_not_called()
    with db.session() as s:      # M1: the CLI charged us before the parse failed
        assert s.query(ClaudeSpend).filter_by(kind="finding_fix").count() == 1


def test_permanent_cli_error_replies_redacted_with_ops_event(db):
    ctx = _ctx(db)
    ctx.runner.review.side_effect = PermanentError(
        "claude exited 1: boom at /repos/acme/widgets/x.py")
    result = run_finding_fix(_params())
    assert result == {"status": "failed"}
    assert "/repos/" not in _reply_body(ctx)     # internal paths redacted
    [ev] = _ops_events(db, event="fix_failed")
    assert ev.severity == "error"


def test_transient_error_propagates_for_rq_retry(db):
    ctx = _ctx(db)
    ctx.runner.review.side_effect = TransientError("429")
    with pytest.raises(TransientError):
        run_finding_fix(_params())
    ctx.github.reply_to_review_comment.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd worker && .venv/bin/python -m pytest tests/test_fix_runner.py -v`
Expected: collection error — `ModuleNotFoundError: No module named 'worker.fix_runner'`.

- [ ] **Step 3: Implement `worker/worker/fix_runner.py`**

```python
"""/fix finding-fix job (spec 2026-07-11).

Commits a minimal fix for ONE REVA finding directly to the PR branch, on an
explicit `/fix` reply (trusted users — gated in the api webhook) on the
finding's inline thread. The one command that writes to branches: per-repo
opt-in (RepoConfig.fix_command, default OFF), deterministic validation gates
before any push, a single commit authored as the REVA bot, never a force push.
The pushed commit deliberately triggers the normal debounce → incremental
delta re-review (loop-bounded: /fix only fires on a human reply and the
webhook ignores bot senders). Every abort replies on the thread AND records an
ops event (component="fix_command")."""

from __future__ import annotations

import structlog
import yaml

from reva.cost import estimate_cost
from reva.db import writers
from reva.errors import PermanentError, TransientError
from reva.review_formatter import _redact_internal_paths
from reva.types import RepoConfig
from worker.runner import budget_exceeded, get_context

logger = structlog.get_logger()

# Deterministic pre-push gates (spec): a fix must stay a surgical, reviewable
# change. Anything bigger aborts with a thread reply — nothing is pushed.
MAX_FIX_LINES = 150
MAX_FIX_FILES = 5
# GitHub would reject a workflow push without the workflow permission anyway —
# refuse earlier, deterministically. Substring (not prefix) match: numstat
# rename syntax ("old => new") and nested .github dirs stay covered.
_FORBIDDEN_SEGMENT = ".github/"

_SKILL = "reva-fix"


def _marker(command_comment_id: int | None) -> str:
    """Commit-message marker keying idempotency to (finding, command comment):
    an RQ retry of THIS command skips the push; a NEW /fix on the same thread
    (different comment id) runs again by design (spec idempotency section)."""
    return f"REVA-Fix-Command: {command_comment_id}"


def run_finding_fix(job_params: dict) -> dict:
    ctx = get_context()
    try:
        owner = job_params["owner"]
        repo = job_params["repo"]
        pr_number = job_params["pr_number"]
        installation_id = job_params["installation_id"]
        finding_comment_id = job_params["finding_comment_id"]
        head_sha = job_params["head_sha"]  # PR head at command time (race baseline)
    except KeyError as exc:
        raise PermanentError(f"run_finding_fix: missing required param {exc}") from exc
    command_comment_id = job_params.get("command_comment_id")
    guidance = (job_params.get("guidance") or "").strip()
    log = logger.bind(owner=owner, repo=repo, pr=pr_number,
                      finding_comment_id=finding_comment_id)

    finding = writers.lookup_finding_by_comment_id(ctx.db, finding_comment_id)
    if finding is None:
        # The api resolved the finding at enqueue time; missing now is DB drift.
        log.warning("fix_finding_not_found")
        _ops(ctx, "warning", "finding_not_found",
             {"repo": f"{owner}/{repo}", "comment_id": finding_comment_id})
        return {"status": "aborted", "reason": "finding_not_found"}

    token = ctx.github.get_installation_token(installation_id)
    pr = ctx.github.get_pull_request(token, owner, repo, pr_number)

    def abort(event: str, message: str, detail: dict | None = None,
              severity: str = "warning") -> dict:
        """Spec rule: every abort replies on the thread AND records an ops event."""
        _ops(ctx, severity, event,
             {"repo": f"{owner}/{repo}", "pr": pr_number, **(detail or {})})
        _reply(ctx, token, owner, repo, pr_number, finding_comment_id,
               f"⚠️ **REVA** `/fix`: {message}", log)
        log.info("fix_aborted", reason=event)
        return {"status": "aborted", "reason": event}

    if pr.get("state") != "open" or pr.get("merged"):
        return abort("pr_not_open",
                     "this PR is not open — `/fix` only runs on open PRs.")

    head = pr.get("head") or {}
    head_ref = head.get("ref") or ""
    head_repo = (head.get("repo") or {}).get("full_name")
    base_repo = ((pr.get("base") or {}).get("repo") or {}).get("full_name")
    if not head_repo or head_repo != base_repo:
        # Spec gap closed during planning: the installation token cannot push
        # to a fork's branch — refuse deterministically instead of failing late.
        return abort("fork_pr_unsupported",
                     "this PR's branch lives in a fork — REVA can only push "
                     "fixes to branches in this repository.")

    config = _load_repo_config(ctx, token, owner, repo, head.get("sha") or head_sha, log)
    if config is None:
        # /fix writes to branches: an unverifiable opt-in fails CLOSED (the
        # read-only config paths fail open; this one must not).
        return abort("config_fetch_failed",
                     "couldn't read `.claude-review.yml` to confirm `/fix` is "
                     "enabled here — aborting without changes.")
    if not config.fix_command:
        _reply(ctx, token, owner, repo, pr_number, finding_comment_id,
               "⚠️ **REVA** `/fix` is disabled for this repository (it lets REVA "
               "push commits to PR branches). Opt in with `fix_command: true` in "
               "`.claude-review.yml`; the REVA GitHub App also needs "
               "**Contents: Read & write**.", log)
        log.info("fix_disabled")
        return {"status": "disabled"}

    spent = budget_exceeded(ctx)
    if spent is not None:
        return abort(
            "declined_budget",
            f"REVA's rolling 24-hour budget (${ctx.daily_budget_usd:.0f}) is "
            f"reached (≈${spent:.0f} spent) — try again once spend rolls off.",
            {"spent_usd": round(spent, 2)},
        )

    marker = _marker(command_comment_id)
    permalink = (f"https://github.com/{owner}/{repo}/pull/{pr_number}"
                 f"#discussion_r{finding_comment_id}")
    try:
        # Same locking discipline as a review (worker/worker/reviewer.py:728):
        # the shared clone must not be reset under us between ensure_repo and
        # the worktree checkout.
        with ctx.runner.repo_lock(owner, repo):
            try:
                repo_path = ctx.runner.ensure_repo(owner, repo, head_sha, token)
            except PermanentError as exc:
                # Command-time SHA unreachable = the branch was force-pushed away.
                return abort("head_moved",
                             "the branch moved since the command (its commit is "
                             "gone) — re-run `/fix` if the finding still applies.",
                             {"error": str(exc)[:200]})

            # RQ-retry idempotency: a prior attempt may have pushed and then
            # crashed before replying. ensure_repo just fetched origin, so the
            # branch tip is local — grep its history for this command's marker.
            if ctx.runner.head_contains_marker(repo_path, head_ref, marker):
                _reply(ctx, token, owner, repo, pr_number, finding_comment_id,
                       "🔧 **REVA** `/fix`: this command already ran — its commit "
                       "is already on the branch; nothing new was pushed.", log)
                log.info("fix_already_applied")
                return {"status": "already_applied"}

            if (head.get("sha") or "") != head_sha:
                return abort("head_moved",
                             "the branch moved since you ran `/fix` — review the "
                             "new commits and re-run `/fix` if it still applies.")

            with ctx.runner.fix_worktree(repo_path, head_sha) as worktree:
                params = {"finding": _finding_block(finding)}
                if guidance:
                    params["operator_guidance"] = guidance
                response = ctx.runner.review(
                    repo_path=worktree, skill=_SKILL, params=params,
                    odoo=config.odoo, allow_edit=True,
                )
                # M1 discipline: the CLI already charged us — ledger the spend
                # before anything below can fail. Prefer the CLI's own total.
                cost = response.total_cost_usd or estimate_cost(
                    response.model or "",
                    response.input_tokens, response.output_tokens,
                    response.cache_read_tokens, response.cache_creation_tokens,
                )
                writers.record_claude_spend(ctx.db, "finding_fix", cost)

                summary = str(
                    (response.tool_use_input or {}).get("summary") or ""
                ).strip()
                if not summary:
                    return abort("bad_cli_output",
                                 "the fix run returned no usable result — "
                                 "nothing was pushed.", severity="error")

                # The SECU-1 scrub deleted tracked CLAUDE.md/.claude/… before
                # the CLI ran; restore them or they'd be committed as deletions.
                ctx.runner.restore_scrubbed(worktree)
                stats = ctx.runner.stage_and_diff_stats(worktree)
                gate = _gate_failure(stats)
                if gate is not None:
                    return abort("gate_failed",
                                 f"validation gate failed — {gate}. Nothing was "
                                 "pushed.",
                                 {"gate": gate,
                                  "files": [p for p, _ in stats][:10]})

                # Race guard (spec): re-fetch the head; if it moved since command
                # time, abort with a reply. TOCTOU backstop: a move between this
                # check and the push is caught by git itself — the plain push of
                # a commit built on the old head is rejected as non-fast-forward
                # (classified below as head_moved).
                pr_now = ctx.github.get_pull_request(token, owner, repo, pr_number)
                if ((pr_now.get("head") or {}).get("sha") or "") != head_sha:
                    return abort("head_moved",
                                 "the branch moved while the fix was being "
                                 "prepared — re-run `/fix` if it still applies.")

                commit_sha = ctx.runner.commit_fix(
                    worktree, _commit_message(finding, permalink, marker)
                )
                try:
                    ctx.runner.push_fix(worktree, token, head_ref)
                except PermanentError as exc:
                    msg = str(exc)
                    lowered = msg.lower()
                    if ("403" in msg or "not granted" in lowered
                            or "protected branch" in lowered):
                        return abort(
                            "push_rejected_permission",
                            "the push was rejected. The REVA GitHub App most "
                            "likely lacks **Contents: Read & write** (or this "
                            "branch is protected) — see the README App-setup "
                            "table.",
                            {"error": msg[:200]}, severity="error",
                        )
                    if ("fetch first" in lowered or "non-fast-forward" in lowered
                            or "cannot lock ref" in lowered):
                        return abort("head_moved",
                                     "the branch moved while pushing — re-run "
                                     "`/fix` if the finding still applies.")
                    return abort("push_failed",
                                 "the push failed — nothing landed on the "
                                 "branch: "
                                 + _redact_internal_paths(msg)[:300],
                                 {"error": msg[:200]}, severity="error")
    except TransientError:
        raise  # RQ retries; the marker check above makes the retry idempotent
    except PermanentError as exc:
        log.warning("fix_failed", exc_info=True)
        _ops(ctx, "error", "fix_failed",
             {"repo": f"{owner}/{repo}", "pr": pr_number, "error": str(exc)[:300]})
        _reply(ctx, token, owner, repo, pr_number, finding_comment_id,
               "⚠️ **REVA** `/fix` failed — "
               + _redact_internal_paths(str(exc))[:300], log)
        return {"status": "failed"}

    commit_url = f"https://github.com/{owner}/{repo}/commit/{commit_sha}"
    _reply(ctx, token, owner, repo, pr_number, finding_comment_id,
           f"🔧 **REVA** pushed a fix: {_redact_internal_paths(summary)}\n\n"
           f"Commit: {commit_url}\n\n"
           "_The push gets the normal (debounced) incremental re-review._", log)
    log.info("fix_completed", commit=commit_sha, cost_usd=cost)
    return {"status": "completed", "commit_sha": commit_sha, "cost_usd": cost}


def _gate_failure(stats: list[tuple[str, int | None]]) -> str | None:
    """Deterministic pre-push gates (spec §5). Returns the human-readable
    failure, or None when the staged diff is pushable. Out-of-worktree writes
    can't appear here by construction: the CLI's workspace boundary confines
    writes to the worktree, and numstat only ever reports repo-relative paths."""
    if not stats:
        return "the fix changed nothing (empty diff)"
    if len(stats) > MAX_FIX_FILES:
        return f"{len(stats)} files changed (max {MAX_FIX_FILES})"
    for path, lines in stats:
        if _FORBIDDEN_SEGMENT in path:
            return f"`{path}` touches {_FORBIDDEN_SEGMENT}"
        if lines is None:
            return f"`{path}` is a binary change"
    total = sum(lines for _, lines in stats if lines is not None)
    if total > MAX_FIX_LINES:
        return f"{total} changed lines (max {MAX_FIX_LINES})"
    return None


def _finding_block(finding: dict) -> str:
    """The finding as a fenced Task Parameter (review() nonce-wraps it, SECU-6)."""
    lines = [
        f"Severity: {finding['severity']}",
        f"Category: {finding['category']}",
        f"Title: {finding['title']}",
    ]
    if finding.get("file_path"):
        loc = finding["file_path"]
        if finding.get("line_start"):
            loc += f" lines {finding['line_start']}"
            if finding.get("line_end") and finding["line_end"] != finding["line_start"]:
                loc += f"-{finding['line_end']}"
        lines.append(f"Location: {loc}")
    lines.append(f"Description: {finding['body']}")
    if finding.get("suggestion"):
        lines.append(f"Suggested fix:\n{finding['suggestion']}")
    return "\n".join(lines)


def _commit_message(finding: dict, permalink: str, marker: str) -> str:
    """Locked format: `REVA fix: <finding title>` + the thread permalink; the
    marker line keys retry idempotency (head_contains_marker greps for it)."""
    return f"REVA fix: {finding['title']}\n\nFinding: {permalink}\n{marker}\n"


def _load_repo_config(ctx, token: str, owner: str, repo: str, ref: str,
                      log) -> RepoConfig | None:
    """RepoConfig at the PR head, or None when it can't be read — the caller
    fails CLOSED (a branch-writing command must not run on an unverifiable
    opt-in). Missing file / non-dict YAML = defaults (fix_command False)."""
    try:
        raw = ctx.github.get_file_content(token, owner, repo, ".claude-review.yml", ref)
        if not raw:
            return RepoConfig()
        parsed = yaml.safe_load(raw)
        if not isinstance(parsed, dict):
            return RepoConfig()
        return RepoConfig.model_validate(parsed)
    except Exception as exc:  # noqa: BLE001 — fail closed, visibly
        log.warning("fix_config_fetch_failed", exc_info=True)
        return None  # the caller records the ops event + thread reply


def _ops(ctx, severity: str, event: str, detail: dict) -> None:
    writers.record_ops_event(ctx.db, "fix_command", severity, event, detail)


def _reply(ctx, token, owner, repo, pr_number, comment_id, body: str, log) -> None:
    """Thread reply on the finding's comment thread. Best-effort + visible:
    a failed reply must not fail the job (the push may already be done)."""
    try:
        ctx.github.reply_to_review_comment(
            token=token, owner=owner, repo=repo, pr_number=pr_number,
            comment_id=comment_id, body=body,
        )
    except Exception as exc:  # noqa: BLE001 — degrade, stay visible
        log.warning("fix_reply_failed", exc_info=True)
        _ops(ctx, "warning", "reply_failed",
             {"repo": f"{owner}/{repo}", "pr": pr_number, "error": str(exc)[:300]})
```

Create `worker/worker/fix_tasks.py`:

```python
"""Stable RQ task entry for /fix finding fixes."""

from worker.fix_runner import run_finding_fix as _run_finding_fix
from worker.task_contract import terminal_on_permanent

run_finding_fix = terminal_on_permanent(_run_finding_fix)

__all__ = ["run_finding_fix"]
```

- [ ] **Step 4: Run the worker suites**

Run: `cd worker && .venv/bin/python -m pytest tests/test_fix_runner.py tests/test_task_contract.py tests/test_comment_reply.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add worker/worker/fix_runner.py worker/worker/fix_tasks.py worker/tests/test_fix_runner.py
git commit -m "feat(worker): finding_fix job — gated CLI fix committed to the PR branch"
```

---

### Task 5: api webhook — `/fix` command + README App-setup/docs

**Files:**
- Modify: `api/app/routes/webhooks.py`:
  - `_INLINE_COMMANDS` (line 285): add the `_FIX_COMMAND` constant next to it
  - new `_handle_fix_command` after `_handle_inline_command` (ends line 346), before `_handle_review_comment` (line 349)
  - `_handle_review_comment`: insert the `/fix` branch directly after the `_INLINE_COMMANDS` short-circuit (lines 381–384)
  - imports: `Retry` (line 12) and `REVIEW_JOB_TIMEOUT` (line 18) are **already imported** — no import changes
- Modify: `api/tests/test_webhooks.py` (append after the `/unmute` tests, ~line 630; reuse `_review_comment_payload` at 487, `_FakeQueue` at 478, `_seed_posted_finding` at 146)
- Modify: `README.md` (permissions table line 65; webhook-events table line 76; new `/fix` subsection after the inline-commands section, line 102)

**Interfaces:**
- Consumes: Task 4's dotted path `"worker.fix_tasks.run_finding_fix"`; existing `writers.lookup_finding_by_comment_id`, `_is_bot_sender`, `_TRUSTED_ASSOCIATIONS` (both gates already run in `_handle_review_comment` **before** command parsing — `/fix` inherits them for free).
- Produces: enqueued job args `{"installation_id", "owner", "repo", "pr_number", "finding_comment_id", "finding_id", "command_comment_id", "head_sha", "guidance"}` with `job_timeout=REVIEW_JOB_TIMEOUT` and `Retry(max=3, interval=[30, 120, 300])` — exactly what Task 4 parses.

- [ ] **Step 1: Write the failing tests**

Append to `api/tests/test_webhooks.py` (after the `/unmute` test, ~line 630):

```python
# --- /fix command -----------------------------------------------------------------


def _fix_payload(body="/fix use the ORM write method", association="MEMBER",
                 in_reply_to=777) -> dict:
    payload = _review_comment_payload(association=association, in_reply_to=in_reply_to)
    payload["comment"]["body"] = body
    payload["comment"]["id"] = 9001                      # the command comment itself
    payload["repository"]["id"] = 1001
    # real pull_request_review_comment payloads carry the full PR object
    payload["pull_request"] = {"number": 42, "head": {"sha": "deadbeef", "ref": "feat/foo"}}
    return payload


def test_fix_command_by_member_enqueues_fix_job_not_a_reply(client_and_db):
    client, db = client_and_db
    fid = _seed_posted_finding(db)
    q = _FakeQueue()
    app.state.rq_queue = q
    try:
        resp = _post(client, _fix_payload(), event="pull_request_review_comment",
                     delivery="fix1")
    finally:
        app.state.rq_queue = None
    assert resp.status_code == 202
    # exactly the fix job — never the paid conversational reply as well
    assert [e["func"] for e in q.enqueued] == ["worker.fix_tasks.run_finding_fix"]
    assert q.enqueued[0]["args"][0] == {
        "installation_id": 99, "owner": "acme", "repo": "widgets",
        "pr_number": 42, "finding_comment_id": 777, "finding_id": fid,
        "command_comment_id": 9001, "head_sha": "deadbeef",
        "guidance": "use the ORM write method",
    }


def test_fix_without_guidance_sends_empty_string(client_and_db):
    client, db = client_and_db
    _seed_posted_finding(db)
    q = _FakeQueue()
    app.state.rq_queue = q
    try:
        _post(client, _fix_payload(body="/FIX"), event="pull_request_review_comment",
              delivery="fix2")   # command matching is case-insensitive
    finally:
        app.state.rq_queue = None
    assert q.enqueued[0]["args"][0]["guidance"] == ""


def test_fix_on_non_finding_thread_is_ignored(client_and_db):
    client, _ = client_and_db          # nothing seeded for comment 555
    q = _FakeQueue()
    app.state.rq_queue = q
    try:
        _post(client, _fix_payload(in_reply_to=555),
              event="pull_request_review_comment", delivery="fix3")
    finally:
        app.state.rq_queue = None
    assert q.enqueued == []


def test_fix_by_outsider_is_ignored(client_and_db):
    client, db = client_and_db
    _seed_posted_finding(db)
    q = _FakeQueue()
    app.state.rq_queue = q
    try:
        _post(client, _fix_payload(association="NONE"),
              event="pull_request_review_comment", delivery="fix4")
    finally:
        app.state.rq_queue = None
    assert q.enqueued == []


def test_fix_by_bot_is_ignored(client_and_db):
    client, db = client_and_db
    _seed_posted_finding(db)
    payload = _fix_payload()
    payload["sender"]["type"] = "Bot"    # anti-loop: REVA can never /fix itself
    q = _FakeQueue()
    app.state.rq_queue = q
    try:
        _post(client, payload, event="pull_request_review_comment", delivery="fix5")
    finally:
        app.state.rq_queue = None
    assert q.enqueued == []
```

- [ ] **Step 2: Run tests to verify the enqueue tests fail**

Run: `cd api && .venv/bin/python -m pytest tests/test_webhooks.py -k fix_ -v`
Expected: `test_fix_command_by_member_enqueues_fix_job_not_a_reply` FAILS — the handler treats `/fix ...` as a question and enqueues `worker.tasks.run_comment_reply` instead (the func-list assertion catches it); `test_fix_without_guidance_sends_empty_string` FAILS the same way. The three ignore-tests already pass (bot/trust gates sit above command parsing; an unseeded thread enqueues a reply though — so `test_fix_on_non_finding_thread_is_ignored` FAILS too until the `/fix` branch short-circuits before the reply enqueue). They must ALL pass after Step 3.

- [ ] **Step 3: Implement**

`api/app/routes/webhooks.py` — next to `_INLINE_COMMANDS` (line 285):

```python
_INLINE_COMMANDS = frozenset({"/dismiss", "/mute", "/unmute"})
# /fix enqueues the (paid, branch-writing) finding-fix job — spec 2026-07-11.
# Same thread, but NOT in _INLINE_COMMANDS: those are zero-cost DB writes.
_FIX_COMMAND = "/fix"
```

After `_handle_inline_command` (line 346), add:

```python
def _handle_fix_command(db: Database, payload: dict, body: str,
                        in_reply_to_id: int, rq_queue) -> None:
    """Enqueue the finding-fix job for a `/fix` reply on a REVA finding thread.

    The bot guard and the SECU-3 trust gate already ran in
    _handle_review_comment. Resolving the finding here (zero cost) keeps
    non-REVA threads from enqueueing anything; the worker re-resolves it and
    applies every paid-path gate itself (fix_command opt-in — default OFF,
    budget, PR open, validation gates, head-SHA race guard)."""
    finding = writers.lookup_finding_by_comment_id(db, in_reply_to_id)
    if finding is None:
        logger.info("fix_no_finding", comment_id=in_reply_to_id)
        return

    pr_data = payload.get("pull_request") or {}
    repo_data = payload.get("repository") or {}
    comment = payload.get("comment") or {}
    owner = (repo_data.get("owner") or {}).get("login")
    repo = repo_data.get("name")
    pr_number = pr_data.get("number")
    head_sha = (pr_data.get("head") or {}).get("sha")  # head at command time
    installation_id = (payload.get("installation") or {}).get("id")
    if (not owner or not repo or not pr_number or not head_sha
            or not installation_id or rq_queue is None):
        logger.warning("fix_command_missing_context",
                       repo=repo_data.get("full_name"), pr=pr_number)
        return

    parts = body.split(maxsplit=1)
    guidance = parts[1].strip() if len(parts) > 1 else ""
    rq_queue.enqueue(
        "worker.fix_tasks.run_finding_fix",
        {
            "installation_id": installation_id,
            "owner": owner,
            "repo": repo,
            "pr_number": pr_number,
            "finding_comment_id": in_reply_to_id,
            "finding_id": finding["id"],
            "command_comment_id": comment.get("id"),
            "head_sha": head_sha,
            "guidance": guidance,
        },
        # A headless-CLI run: same time budget as a review job, or the RQ
        # work-horse is SIGKILLed mid-paid-run (see REVIEW_JOB_TIMEOUT).
        job_timeout=REVIEW_JOB_TIMEOUT,
        # Transient blips retry with backoff; the job is idempotent per
        # (finding, command comment) via its commit-message marker.
        retry=Retry(max=3, interval=[30, 120, 300]),
    )
    logger.info("finding_fix_queued", repo=repo_data.get("full_name"),
                pr=pr_number, finding_id=finding["id"])
```

In `_handle_review_comment`, directly after the `_INLINE_COMMANDS` short-circuit (lines 381–384):

```python
    # Structured zero-cost commands short-circuit the paid reply.
    if question.split()[0].lower() in _INLINE_COMMANDS:
        _handle_inline_command(db, payload, question, in_reply_to_id)
        return

    # /fix — the (paid, branch-writing) fix job; everything after the command
    # word is operator guidance for the fixer (spec 2026-07-11).
    if question.split()[0].lower() == _FIX_COMMAND:
        _handle_fix_command(db, payload, question, in_reply_to_id, rq_queue)
        return
```

Everything below (the paid-reply enqueue) stays byte-identical. `_process_delivery` already passes `rq_queue` into `_handle_review_comment` (line 111) — no dispatch change.

`README.md`:

Replace the permissions row (line 65):

```markdown
| Contents | Read & write (write is used only by the opt-in `/fix` command; Read suffices if you never enable it) |
```

Replace the webhook-events row (line 76):

```markdown
| `pull_request_review_comment` | Replies to questions on inline comments; `/dismiss` `/mute` `/unmute` `/fix` commands |
```

After the inline-commands section's closing paragraph (line 102), before `## Comment replies` (line 104), insert:

```markdown
### `/fix` — commit the fix (opt-in, paid)

Reply `/fix` on one of REVA's inline finding comments (trusted users only) and REVA fixes **that finding** with a headless Claude Code run on a throwaway worktree of the PR branch, then pushes a **single commit** (`REVA fix: <finding title>`, authored as the REVA bot) directly to the branch — never force-pushed. Text after the command is passed as guidance: `/fix use the ORM method instead`. Deterministic gates run before any push: non-empty diff, ≤ 150 changed lines, ≤ 5 files, nothing under `.github/`, no fork branches, and the branch must not have moved since the command — every abort is explained in a thread reply. The push then gets the normal debounced **incremental delta re-review** (intentional). Off by default: opt in per repo with `fix_command: true` in `.claude-review.yml`, and grant the GitHub App **Contents: Read & write** — a push rejected for a missing permission is reported on the thread.
```

- [ ] **Step 4: Run the api suite**

Run: `cd api && .venv/bin/python -m pytest tests/test_webhooks.py -q`
Expected: all PASS — including the pre-existing reply/dismiss/mute tests (the `/fix` branch sits between them and changes neither path).

- [ ] **Step 5: Commit**

```bash
git add api/app/routes/webhooks.py api/tests/test_webhooks.py README.md
git commit -m "feat(api): /fix review-comment command enqueues the finding_fix job"
```

---

### Task 6: Verification sweep + spec sync

**Files:**
- Modify: `docs/superpowers/specs/2026-07-11-fix-command-design.md` (Status line + fork-gate note)

- [ ] **Step 1: Full gates** (shared `reva/` touched → all three services)

Run from the repo root:

```bash
make test
```
Expected: worker, api, and scheduler suites all green.

```bash
worker/.venv/bin/ruff check reva worker/worker api/app scheduler/scheduler
```
Expected: no findings (exit 0).

```bash
worker/.venv/bin/mypy reva worker/worker api/app scheduler/scheduler --ignore-missing-imports
```
Advisory (non-blocking): no NEW errors vs. main. **No `tui/` changes in this plan** — confirm with `git status --porcelain tui/` printing nothing; do not run the Go gates.

- [ ] **Step 2: Spec sync**

In `docs/superpowers/specs/2026-07-11-fix-command-design.md`:

Replace:

```markdown
- **Status:** approved (design), not yet planned
```

with:

```markdown
- **Status:** approved — implemented; see `plans/2026-07-11-fix-command.md`.
```

And in the "Job" section, at the end of item 5 (the validation-gates item), append the gate the spec was silent on (code wins over prose — the implementation refuses fork PRs deterministically):

```markdown
   Additionally: fork PRs are refused up front (the installation token cannot
   push to a fork's branch) — added during planning.
```

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-07-11-fix-command-design.md
git commit -m "docs(specs): /fix command — mark implemented, record fork gate"
```

**Honest-status note for the final report:** all coverage is unit-level — mocked `subprocess.run` for the git machinery, MagicMock runner/GitHub + SQLite for the job, FastAPI TestClient for the webhook. No live CLI run, no real git push, no real worktree round-trip happened. The spec's staging gates remain open after deploy: (1) upgrade the GitHub App to **Contents: Read & write** (operator action), (2) set `fix_command: true` on a staging repo and run one live `/fix`, (3) verify the fix commit triggers the debounced delta re-review, and (4) verify the commit's author/pusher attribution renders as the App bot on GitHub (the `reva[bot]` identity is display attribution; the push is token-authenticated either way). The `git worktree`/`push` argv shapes were designed against git semantics but only argv-asserted in tests — the first staging `/fix` is the real proof.
