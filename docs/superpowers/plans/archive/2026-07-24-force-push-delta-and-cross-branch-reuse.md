# Force-push-aware delta + cross-branch review reuse — Implementation Plan

> **Status:** implemented + committed 2026-07-24. All 9 tasks done; worker/api/scheduler suites, ruff, TUI, and real-git integration green (see the shipping commit). Checkboxes left unticked — the archive location is authoritative for "shipped", per CLAUDE.md.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop REVA re-reviewing force-pushed and promoted PRs from scratch: make same-PR delta survive force-push/amend (local two-tree diff), and carry a prior review's verdict forward when a promotion PR's diff is identical to one already reviewed in the repo.

**Architecture:** Two features share one path in `worker/worker/reviewer.py::Reviewer.execute`. #2 replaces the "diverged → full review" bail with a local, lock-free `git diff <prior> <new>` gated on the PR's merge-base with its target branch being unchanged. #3 fingerprints every full-scope completed review with a `diff_hash` and, on a PR's *first* review, carries the matched PR's open findings forward with no Claude call. Both reuse a shared `_carry_forward_result` builder.

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy ORM + raw-SQL migrations, RQ, Pydantic; Go/Bubble Tea TUI; pytest; SQLite (unit) + Postgres (`make test-integration`).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-24-force-push-delta-and-cross-branch-reuse-design.md`. **v1 = diff_hash only; `content_hash`, cross-branch *partial* delta, and core-version gating are OUT of scope** (Future work).
- Tests build tables from ORM models (`create_all`), NOT the SQL migration — a new column MUST be added to `reva/db/models.py::ReviewRun` or it is invisible to tests.
- Migration files: numbered, idempotent (`ADD COLUMN IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`), `BIGSERIAL`/`BIGINT` convention (not `IDENTITY`). Applied at startup under an advisory lock.
- Degradation-visibility invariant: every caught-and-degraded path MUST log AND `self._record_ops_event(component, severity, event, data)` (reviewer already carries `_record_ops_event`, backed by the injected `ops_recorder`).
- New #2 git ops are **read-only object reads + a PR-ref fetch — never checkout/reset** — so they run WITHOUT the per-repo working-tree lock and add nothing to `REVIEW_JOB_TIMEOUT`. Every git failure is caught at the call site and degrades to a full review; **never re-raised** (a missing object is permanent; re-raising burns RQ's 3 paid retries).
- Definition of done: a `reva/` change runs worker + api + scheduler suites (`make test`) + `ruff`. A `tui/` change runs `cd tui && go build ./... && go vet ./... && go test ./...`. No Odoo contract is touched → no `contracts/` regeneration.
- Do NOT commit to `main`. Work on a feature branch; the user has said not to push/merge. Commit per task on the branch.

## File Structure

- `db/migrations/042_review_reuse.sql` — **create**: 3 columns + 1 index on `review_runs`.
- `reva/db/models.py` — modify `ReviewRun`: `diff_hash`, `delta_base_sha`, `carried_from_run_id`.
- `reva/types.py` — modify `ReviewResult` (`diff_hash`, `carried_from_run_id`; `delta_base_sha` already exists) and `RepoConfig` (`cross_branch_reuse`).
- `reva/config.py` — add `CROSS_BRANCH_REUSE`.
- `reva/db/writers.py` — modify `record_review_completed` to persist the 3 fields; add `find_reusable_review`.
- `reva/db/repo_lookup.py` — add `find_reusable_review` + `get_all_open_findings_for_pr` methods on `DatabaseRepoLookup`.
- `reva/diff_utils.py` — add `diff_content_hash`.
- `reva/claude_code_runner.py` — add `two_tree_diff` (lock-free, authenticated, returns `(raw_diff|None, reason)`).
- `worker/worker/reviewer.py` — the core: rework the delta block (#2), add the #3 lookup + `_carry_forward_result` + `_finding_from_row`, extend the `_ReviewRepository` protocol, thread `diff_hash`.
- `api/app/queries/reviews.py` + `api/app/routes/v1/reviews.py` — expose `carried_from`.
- `tui/internal/api/{types,client,iface,mock}.go` + `tui/internal/ui/*reviews*.go` — show a "carried from #N" label.
- Tests alongside each (`worker/tests/`, `api/tests/`, `tui/...`), plus a Postgres-gated real-git test.

## Dispatch order (for the worker fleet)

- **Wave 1 (parallel, no file overlap):** Task 1 (foundation), Task 2 (`diff_content_hash`), Task 3 (`two_tree_diff`).
- **Wave 2 (after 1):** Task 4 (DB read queries) — then the reviewer chain Task 5 → Task 6 (both edit `reviewer.py`, so **sequential, same worker**). Task 7 (API) can run in parallel with the reviewer chain once Task 1 lands.
- **Wave 3:** Task 8 (TUI, after Task 7), Task 9 (real-git integration, after Task 6).

---

## Task 1: Schema + type + config foundation

**Files:**
- Create: `db/migrations/042_review_reuse.sql`
- Modify: `reva/db/models.py` (ReviewRun, ~line 142), `reva/types.py` (ReviewResult ~top; RepoConfig line 49-89), `reva/config.py` (~line 24), `reva/db/writers.py::record_review_completed` (line 191)
- Test: `worker/tests/test_writers.py` (or the existing writers test module), `worker/tests/test_repo_config.py`

**Interfaces:**
- Produces: `ReviewRun.diff_hash: str|None`, `ReviewRun.delta_base_sha: str|None`, `ReviewRun.carried_from_run_id: int|None`; `ReviewResult.diff_hash: str|None`, `ReviewResult.carried_from_run_id: int|None`; `RepoConfig.cross_branch_reuse: bool` (default True); `reva.config.CROSS_BRANCH_REUSE: bool`.

- [ ] **Step 1: Write the migration**

Create `db/migrations/042_review_reuse.sql`:

```sql
-- Force-push-aware delta + cross-branch review reuse (spec 2026-07-24).
-- diff_hash: fingerprint of a full-scope review's filtered diff (NULL on delta/declined).
-- delta_base_sha: persisted review scope (NULL = full-scope; non-NULL = delta run).
-- carried_from_run_id: the review_runs.id a carried-forward run reused (NULL = a real review).
ALTER TABLE review_runs ADD COLUMN IF NOT EXISTS diff_hash           TEXT;
ALTER TABLE review_runs ADD COLUMN IF NOT EXISTS delta_base_sha      TEXT;
ALTER TABLE review_runs ADD COLUMN IF NOT EXISTS carried_from_run_id BIGINT;
CREATE INDEX IF NOT EXISTS ix_review_runs_diff_hash
    ON review_runs (repository_id, diff_hash) WHERE diff_hash IS NOT NULL;
```

- [ ] **Step 2: Add ORM columns**

In `reva/db/models.py`, class `ReviewRun`, next to `reset_at`:

```python
    diff_hash: Mapped[str | None] = mapped_column(Text)
    delta_base_sha: Mapped[str | None] = mapped_column(Text)
    carried_from_run_id: Mapped[int | None] = mapped_column(BigInteger)
```

- [ ] **Step 3: Add ReviewResult + RepoConfig + config fields**

In `reva/types.py`, `ReviewResult` (near `model: str | None = None`):

```python
    # Cross-branch reuse fingerprint (full-scope reviews only; NULL on delta runs).
    diff_hash: str | None = None
    # Set on a carried-forward run: the review_runs.id whose verdict was reused.
    carried_from_run_id: int | None = None
```

In `reva/types.py`, `RepoConfig`, after `learned_memory`:

```python
    # Kill switch for cross-branch review reuse (dev→stage→prod promotions).
    cross_branch_reuse: bool = True
```

In `reva/config.py`, near the other module constants:

```python
# Global kill switch for cross-branch review reuse (spec 2026-07-24). Default on.
CROSS_BRANCH_REUSE = os.environ.get("REVA_CROSS_BRANCH_REUSE", "true").strip().lower() not in ("false", "0", "no")
```

- [ ] **Step 4: Persist the fields in `record_review_completed`**

In `reva/db/writers.py::record_review_completed`, after `run.triage_escalation = result.triage_escalation`:

```python
        run.diff_hash = result.diff_hash
        run.delta_base_sha = result.delta_base_sha
        run.carried_from_run_id = result.carried_from_run_id
```

- [ ] **Step 5: Write the failing tests**

In the writers test module (e.g. `worker/tests/test_writers.py`):

```python
def test_record_review_completed_persists_reuse_fields(db, review_params):
    from reva.types import ReviewResult
    result = ReviewResult(status="completed", summary="s", risk_level="low",
                          diff_hash="abc123", delta_base_sha=None, carried_from_run_id=None)
    run_id = writers.record_review_completed(db, review_params, result)
    with db.session() as s:
        run = s.get(ReviewRun, run_id)
        assert run.diff_hash == "abc123"
        assert run.delta_base_sha is None
        assert run.carried_from_run_id is None
```

In `worker/tests/test_repo_config.py`:

```python
def test_repo_config_parses_cross_branch_reuse():
    from reva.types import RepoConfig
    assert RepoConfig().cross_branch_reuse is True
    assert RepoConfig(**{"cross_branch_reuse": False}).cross_branch_reuse is False
```

(Reuse the existing fixtures in those modules for `db`/`review_params`; if none exists, copy the setup from a neighbouring test in the same file.)

- [ ] **Step 6: Run tests to verify they fail**

Run: `cd worker && .venv/bin/python -m pytest tests/test_writers.py -k reuse_fields tests/test_repo_config.py -k cross_branch -v`
Expected: FAIL (attributes/columns missing).

- [ ] **Step 7: Run tests to verify they pass**

Run: same command. Expected: PASS.

- [ ] **Step 8: Run the full trio + ruff**

Run: `make test && ruff check reva worker/worker api/app scheduler/scheduler`
Expected: green (a shared `reva/` change touches all three services).

- [ ] **Step 9: Commit**

```bash
git add db/migrations/042_review_reuse.sql reva/db/models.py reva/types.py reva/config.py reva/db/writers.py worker/tests/
git commit -m "feat(reviewer): schema + plumbing for delta/reuse fingerprints"
```

---

## Task 2: `diff_content_hash` utility

**Files:**
- Modify: `reva/diff_utils.py`
- Test: `worker/tests/test_diff_utils.py` (or the existing diff_utils test)

**Interfaces:**
- Produces: `reva.diff_utils.diff_content_hash(diff: str) -> str` (hex SHA-256, `index` lines normalised out).

- [ ] **Step 1: Write the failing test**

```python
def test_diff_content_hash_ignores_index_lines():
    from reva.diff_utils import diff_content_hash
    a = ("diff --git a/f.py b/f.py\nnew file mode 100644\n"
         "index 0000000..1a2b3c4\n--- /dev/null\n+++ b/f.py\n@@ -0,0 +1 @@\n+x = 1\n")
    b = a.replace("1a2b3c4", "9f8e7d6")  # different abbrev, same content
    assert diff_content_hash(a) == diff_content_hash(b)

def test_diff_content_hash_differs_on_content():
    from reva.diff_utils import diff_content_hash
    a = "@@ -0,0 +1 @@\n+x = 1\n"
    b = "@@ -0,0 +1 @@\n+x = 2\n"
    assert diff_content_hash(a) != diff_content_hash(b)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd worker && .venv/bin/python -m pytest tests/test_diff_utils.py -k content_hash -v`
Expected: FAIL with `ImportError`/`AttributeError`.

- [ ] **Step 3: Implement**

At the top of `reva/diff_utils.py` add `import hashlib` and `import re` if absent, then:

```python
_INDEX_LINE_RE = re.compile(r"^index [0-9a-fA-F]+\.\.[0-9a-fA-F]+.*$", re.MULTILINE)


def diff_content_hash(diff: str) -> str:
    """Stable SHA-256 (hex) of a unified diff for cross-branch matching.

    `index <old>..<new>` lines are normalised out: their blob-abbreviation width
    is repo-state-dependent, so the same content can render them differently on
    two clones. `\\ No newline` and rename markers are kept — they are content.
    """
    normalized = _INDEX_LINE_RE.sub("index", diff)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
```

- [ ] **Step 4: Run test to verify it passes**

Run: same command. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add reva/diff_utils.py worker/tests/test_diff_utils.py
git commit -m "feat(diff): add diff_content_hash for cross-branch matching"
```

---

## Task 3: `two_tree_diff` git helper (lock-free)

**Files:**
- Modify: `reva/claude_code_runner.py` (add a public method on `ClaudeCodeRunner`)
- Test: `worker/tests/test_claude_code_runner.py`

**Interfaces:**
- Produces: `ClaudeCodeRunner.two_tree_diff(token, owner, name, base_ref, prior_sha, new_head, pr_number) -> tuple[str | None, str]`. Returns `(raw_diff, "ok")` on success, else `(None, reason)` where reason ∈ `{"cold_cache","object_missing","base_moved","error"}`. **Never raises.** Caller applies `filter_diff` and records the ops event. Lock-free: object reads + a PR-ref fetch, no working-tree mutation.

- [ ] **Step 1: Write the failing test**

Use a real temp git repo (this helper is thin over `git`; mocking it tests nothing):

```python
import base64, os, subprocess
from reva.claude_code_runner import ClaudeCodeRunner

def _git(cwd, *args):
    subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True, text=True)

def test_two_tree_diff_amend_same_base(tmp_path):
    # origin bare repo with a base commit; two amended heads sharing that base.
    origin = tmp_path / "origin.git"; subprocess.run(["git","init","--bare",str(origin)],check=True,capture_output=True)
    work = tmp_path / "work"; subprocess.run(["git","clone",str(origin),str(work)],check=True,capture_output=True)
    w = str(work)
    _git(w,"config","user.email","t@t"); _git(w,"config","user.name","t")
    os.makedirs(work/"custom_addons"/"m", exist_ok=True)
    (work/"custom_addons"/"m"/"a.py").write_text("x = 1\n")
    _git(w,"add","-A"); _git(w,"commit","-m","base"); _git(w,"push","origin","HEAD:main")
    base = subprocess.run(["git","-C",w,"rev-parse","HEAD"],capture_output=True,text=True).stdout.strip()
    # amend 1
    (work/"custom_addons"/"m"/"b.py").write_text("y = 1\n")
    _git(w,"add","-A"); _git(w,"commit","-m","feat")
    prior = subprocess.run(["git","-C",w,"rev-parse","HEAD"],capture_output=True,text=True).stdout.strip()
    # amend 2 (same base): tweak b.py
    (work/"custom_addons"/"m"/"b.py").write_text("y = 2\n")
    _git(w,"add","-A"); _git(w,"commit","--amend","-m","feat")
    new = subprocess.run(["git","-C",w,"rev-parse","HEAD"],capture_output=True,text=True).stdout.strip()
    _git(w,"update-ref",f"refs/pull/7/head",new); _git(w,"push","origin",f"refs/pull/7/head")

    runner = ClaudeCodeRunner(repo_cache_dir=str(tmp_path/"cache"), api_key="k", skills_dir="s")
    # Prime the cache clone as ensure_repo would (blobless full-history clone of origin).
    cache = tmp_path/"cache"/"o"/"r"; os.makedirs(cache.parent, exist_ok=True)
    subprocess.run(["git","clone",str(origin),str(cache)],check=True,capture_output=True)
    subprocess.run(["git","-C",str(cache),"fetch","origin",f"+refs/pull/7/head:refs/pull/7/head",prior],capture_output=True)

    diff, reason = runner.two_tree_diff("tok","o","r","main",prior,new,7)
    assert reason == "ok"
    assert "y = 2" in diff and "b.py" in diff
    assert "a.py" not in diff  # unchanged file absent from a two-tree diff

def test_two_tree_diff_missing_prior_object(tmp_path):
    runner = ClaudeCodeRunner(repo_cache_dir=str(tmp_path/"cache"), api_key="k", skills_dir="s")
    # No cache clone at all → cold_cache, never raises.
    diff, reason = runner.two_tree_diff("tok","o","r","main","dead"*10,"beef"*10,1)
    assert diff is None and reason == "cold_cache"
```

(Adjust the auth-args expectation to your test's needs — the origin here is local so the `http.extraHeader` is harmless. If your CI blocks network, this test uses only local paths, so no real GitHub is contacted; the PR-ref fetch in the helper targets `origin`, which is the local bare repo.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd worker && .venv/bin/python -m pytest tests/test_claude_code_runner.py -k two_tree_diff -v`
Expected: FAIL with `AttributeError: two_tree_diff`.

- [ ] **Step 3: Implement the helper**

Add to `reva/claude_code_runner.py` (imports `filter_diff` are NOT needed here — the caller filters). Match the `subprocess.run(..., env=self._subprocess_env(), timeout=_GIT_TIMEOUT)` idiom of `_is_git_repo`:

```python
    def two_tree_diff(
        self, token: str, owner: str, name: str, base_ref: str,
        prior_sha: str, new_head: str, pr_number: int,
    ) -> tuple[str | None, str]:
        """Raw `git diff <prior_sha> <new_head>`, or (None, reason) to signal a
        full-review fallback. Lock-free (object reads + a PR-ref fetch, no
        checkout/reset), authenticated (blobless clone needs the header for the
        lazy promisor blob fetch), and it NEVER raises — any failure returns a
        reason and the caller degrades to a full review."""
        repo_path = os.path.join(self.repo_cache_dir, owner, name)
        if not (os.path.isdir(repo_path) and self._is_git_repo(repo_path)):
            return None, "cold_cache"
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        auth = ["-c", f"http.extraHeader=Authorization: Basic {basic}"]

        def _run(args: list[str]) -> subprocess.CompletedProcess:
            return subprocess.run(
                ["git"] + args, capture_output=True, text=True,
                timeout=_GIT_TIMEOUT, env=self._subprocess_env(),
            )
        try:
            # Materialise new_head via the PR head ref (reliably served); best-effort.
            _run(auth + ["-C", repo_path, "fetch", "origin",
                         f"+refs/pull/{pr_number}/head:refs/pull/{pr_number}/head"])
            # prior_sha must already be local (its own review fetched it); a
            # force-pushed-away commit is NOT assumed re-fetchable.
            if _run(["-C", repo_path, "cat-file", "-e", f"{prior_sha}^{{commit}}"]).returncode != 0:
                return None, "object_missing"
            if _run(["-C", repo_path, "cat-file", "-e", f"{new_head}^{{commit}}"]).returncode != 0:
                return None, "object_missing"
            mb_prior = _run(["-C", repo_path, "merge-base", f"origin/{base_ref}", prior_sha])
            mb_new = _run(["-C", repo_path, "merge-base", f"origin/{base_ref}", new_head])
            if mb_prior.returncode != 0 or mb_new.returncode != 0:
                return None, "object_missing"
            if mb_prior.stdout.strip() != mb_new.stdout.strip():
                return None, "base_moved"
            diff = _run(auth + ["-C", repo_path, "diff", prior_sha, new_head])
            if diff.returncode != 0:
                return None, "error"
            return diff.stdout, "ok"
        except (subprocess.TimeoutExpired, OSError):
            logger.warning("two_tree_diff_failed", owner=owner, repo=name,
                           prior=prior_sha[:8], new=new_head[:8], exc_info=True)
            return None, "error"
```

- [ ] **Step 4: Run test to verify it passes**

Run: same command. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add reva/claude_code_runner.py worker/tests/test_claude_code_runner.py
git commit -m "feat(runner): lock-free two_tree_diff for force-push delta"
```

---

## Task 4: DB read queries for reuse

**Files:**
- Modify: `reva/db/writers.py` (add `find_reusable_review`), `reva/db/repo_lookup.py` (add methods on `DatabaseRepoLookup`)
- Test: `worker/tests/test_repo_lookup.py` (or the writers test module)

**Interfaces:**
- Produces:
  - `DatabaseRepoLookup.find_reusable_review(repository_id: int, diff_hash: str, exclude_pull_request_id: int) -> dict | None` → `{"id", "pull_request_id", "pr_number"}`.
  - `DatabaseRepoLookup.get_all_open_findings_for_pr(pull_request_id: int) -> list[dict]` (no 30-cap, unlike `get_prior_open_findings`).

- [ ] **Step 1: Write the failing test**

```python
def test_find_reusable_review_matches_other_pr_by_diff_hash(db, make_run):
    # make_run(pr_id, sha, status, diff_hash, check_run_id, completed_at) helper
    make_run(pr_id=1, sha="a", status="completed", diff_hash="H", check_run_id=10)
    lookup = DatabaseRepoLookup(db)
    hit = lookup.find_reusable_review(repository_id=1, diff_hash="H", exclude_pull_request_id=2)
    assert hit["pull_request_id"] == 1
    # same PR excluded, unposted excluded, NULL diff_hash (delta) excluded:
    assert lookup.find_reusable_review(1, "H", exclude_pull_request_id=1) is None
    make_run(pr_id=3, sha="b", status="completed", diff_hash=None, check_run_id=11)
    assert lookup.find_reusable_review(1, "H", exclude_pull_request_id=99)["pull_request_id"] == 1
```

(Build `make_run` from the ORM directly — insert `ReviewRun` + a `PullRequest` row with `pr_number`. Look at an existing repo_lookup/writers test for the row-construction pattern and the real `PullRequest` model/field names.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd worker && .venv/bin/python -m pytest tests/test_repo_lookup.py -k reusable -v`
Expected: FAIL (`AttributeError`).

- [ ] **Step 3: Implement `find_reusable_review` in writers.py**

```python
def find_reusable_review(
    db: Database, repository_id: int, diff_hash: str, exclude_pull_request_id: int
) -> dict | None:
    """Most-recent completed, POSTED, full-scope review in this repo whose
    diff_hash matches, on a DIFFERENT PR. diff_hash is NULL on delta runs, so
    the equality filter excludes them. Returns {id, pull_request_id, pr_number}."""
    with db.session() as s:
        row = s.execute(
            select(ReviewRun.id, ReviewRun.pull_request_id, PullRequest.pr_number)
            .join(PullRequest, ReviewRun.pull_request_id == PullRequest.id)
            .where(ReviewRun.repository_id == repository_id)
            .where(ReviewRun.status == "completed")
            .where(ReviewRun.diff_hash == diff_hash)
            .where(ReviewRun.check_run_id.is_not(None))
            .where(ReviewRun.pull_request_id != exclude_pull_request_id)
            .order_by(ReviewRun.completed_at.desc())
            .limit(1)
        ).first()
    if not row:
        return None
    return {"id": row[0], "pull_request_id": row[1], "pr_number": row[2]}
```

(Confirm the `PullRequest` model import + `pr_number` attribute name at the top of `writers.py`; `get_pr_basic` already reads it, so the field exists.)

- [ ] **Step 4: Add the two methods to `DatabaseRepoLookup`**

In `reva/db/repo_lookup.py`:

```python
    def find_reusable_review(self, repository_id: int, diff_hash: str, exclude_pull_request_id: int) -> dict | None:
        from reva.db import writers
        return writers.find_reusable_review(self._db, repository_id, diff_hash, exclude_pull_request_id)

    def get_all_open_findings_for_pr(self, pull_request_id: int) -> list[dict]:
        # Full PR-wide open set (no 30-item prompt cap) for carry-forward.
        from reva.db import writers
        return writers.get_open_findings_for_pr(self._db, pull_request_id)
```

- [ ] **Step 5: Run test to verify it passes**

Run: same command. Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add reva/db/writers.py reva/db/repo_lookup.py worker/tests/
git commit -m "feat(db): find_reusable_review + full open-findings read for reuse"
```

---

## Task 5: Reviewer — force-push-aware delta (#2)

**Files:**
- Modify: `worker/worker/reviewer.py` (delta block, lines 353-398; protocol `_ReviewRepository`, ~229-244; final `ReviewResult`, line 831)
- Test: `worker/tests/test_reviewer.py`

**Interfaces:**
- Consumes: `ClaudeCodeRunner.two_tree_diff` (Task 3), `diff_content_hash` (Task 2), `DatabaseRepoLookup.get_all_open_findings_for_pr` (Task 4).
- Produces: reworked diverged-delta behavior + `ReviewResult.delta_base_sha`/`diff_hash` set consistently. `_carry_forward_result` is added in Task 6 but is FIRST USED here (empty-delta path) — implement Task 6's builder before this task's Step 3, or stub then fill. Recommended: do Task 6 Steps 3-4 first, then this task.

- [ ] **Step 1: Extend the repository protocol**

In `worker/worker/reviewer.py`, the `_ReviewRepository` Protocol (near line 229), add:

```python
    def find_reusable_review(self, repository_id: int, diff_hash: str, exclude_pull_request_id: int) -> dict | None: ...
    def get_all_open_findings_for_pr(self, pull_request_id: int) -> list[dict]: ...
```

Add the same two methods to the test fake in `worker/tests/test_reviewer.py` (near line 131), returning `None` / `[]` by default, overridable per-test.

- [ ] **Step 2: Write the failing tests**

```python
def test_diverged_amend_same_base_uses_two_tree_delta(reviewer, fakes):
    # prior review exists; compare_status -> "diverged"; two_tree_diff -> ("<delta>","ok")
    fakes.repos.last_review = {"id": 5, "head_sha": "prior"}
    fakes.github.compare_status = "diverged"
    fakes.runner.two_tree_diff_result = ("diff --git a/custom_addons/m/b.py b/custom_addons/m/b.py\n@@ -1 +1 @@\n-y = 1\n+y = 2\n", "ok")
    result = reviewer.execute(params_diverged)
    assert result.delta_base_sha == "prior"          # delta used
    assert fakes.github.get_pull_request_diff_called is False   # NOT a full review

def test_diverged_base_moved_falls_back_to_full(reviewer, fakes):
    fakes.repos.last_review = {"id": 5, "head_sha": "prior"}
    fakes.github.compare_status = "diverged"
    fakes.runner.two_tree_diff_result = (None, "base_moved")
    result = reviewer.execute(params_diverged)
    assert result.delta_base_sha is None
    assert fakes.github.get_pull_request_diff_called is True
    assert ("reviewer", "delta_base_moved") in fakes.ops_events_kinds  # (component, event)

def test_diverged_empty_delta_carries_forward_not_stale(reviewer, fakes):
    fakes.repos.last_review = {"id": 5, "head_sha": "prior", "pull_request_id": 1, "pr_number": 42}
    fakes.github.compare_status = "diverged"
    fakes.runner.two_tree_diff_result = ("", "ok")     # reviewed-path delta empty
    fakes.repos.open_findings = [ {"id":1,"file_path":"custom_addons/m/a.py","line_start":3,"title":"t","body":"b","severity":"major","category":"correctness","github_comment_id":9} ]
    result = reviewer.execute(params_diverged)
    assert result.status == "completed"
    assert result.carried_from_run_id == 5
    assert any(f.severity == "major" for f in result.findings)   # NOT laundered to skipped
```

(Extend the existing `fakes`/reviewer fixtures in `test_reviewer.py`: add `two_tree_diff_result` to the runner fake, `compare_status` to the github fake, an ops-event capture list, and `last_review`/`open_findings` to the repos fake. Mirror how `get_pull_request_diff_called` is already tracked if present; otherwise add a boolean flag.)

- [ ] **Step 3: Rework the delta block**

Replace `worker/worker/reviewer.py` lines 353-398 (from `last_review = ...` through the `else: ... delta_base_sha = None` block) with:

```python
        last_review = self.repos.get_last_completed_review(params.pull_request_id)
        prior_findings: list[dict] = []
        delta_base_sha: str | None = None
        base_ref = (pr_detail.get("base") or {}).get("ref")

        def _full_diff() -> str:
            raw = self.github.get_pull_request_diff(token, owner, name, pr_number)
            return filter_diff(raw, include_prefixes=review_prefixes)

        if last_review:
            try:
                status = self.github.get_compare_status(
                    token, owner, name, last_review["head_sha"], params.head_sha)
            except Exception:  # noqa: BLE001
                log.warning("review_delta_status_failed",
                            delta_base=last_review["head_sha"][:8], exc_info=True)
                status = ""
            if status in ("ahead", "identical"):
                # Clean follow-up push: the API two-dot compare diff is a true delta.
                raw_diff = self.github.get_compare_diff(
                    token, owner, name, last_review["head_sha"], params.head_sha)
                diff = filter_diff(raw_diff, include_prefixes=review_prefixes)
                delta_base_sha = last_review["head_sha"]
                prior_findings = self.repos.get_prior_open_findings(params.pull_request_id)
                if not diff.strip():
                    return self._stale("No reviewable changes since last review.")
            elif status in ("diverged", "behind"):
                # Force-push / amend: try a LOCAL two-tree delta, gated on the base
                # being unchanged. Lock-free; any failure → full review.
                td, reason = self.runner.two_tree_diff(
                    token, owner, name, base_ref or "HEAD",
                    last_review["head_sha"], params.head_sha, pr_number)
                if reason == "ok" and td is not None:
                    diff = filter_diff(td, include_prefixes=review_prefixes)
                    delta_base_sha = last_review["head_sha"]
                    prior_findings = self.repos.get_prior_open_findings(params.pull_request_id)
                    if not diff.strip():
                        # Amend touched only non-reviewed paths → content unchanged in
                        # reviewed paths. Do NOT emit a `skipped` check (would clear a
                        # blocking gate the prior review failed). Carry the prior verdict
                        # forward onto the new SHA instead.
                        self._record_ops_event("reviewer", "info", "delta_empty_carry_forward",
                                               {"pr": pr_number, "prior_run": last_review["id"]})
                        muted = self.repos.get_muted_categories(params.repository_id)
                        matched = {"id": last_review["id"],
                                   "pull_request_id": params.pull_request_id,
                                   "pr_number": pr_number}
                        return self._carry_forward_result(matched, _full_diff(), repo_config, muted)
                else:
                    log.info("review_delta_fallback", status=status, reason=reason,
                             delta_base=last_review["head_sha"][:8])
                    self._record_ops_event(
                        "reviewer", "info",
                        "delta_base_moved" if reason == "base_moved" else "delta_object_missing",
                        {"pr": pr_number, "reason": reason,
                         "prior": last_review["head_sha"][:8], "new": params.head_sha[:8]})
                    diff = _full_diff()
            else:
                diff = _full_diff()
        else:
            diff = _full_diff()
```

Notes for the implementer:
- `pr_detail` is already fetched at line 327; reuse it for `base_ref`.
- `filter_diff` and `review_prefixes` are already in scope from the surrounding code.
- Add small helpers `_stale(summary)` and (from Task 6) `_carry_forward_result` on `Reviewer`. `_stale`:
  ```python
  def _stale(self, summary: str) -> ReviewResult:
      return ReviewResult(status="stale", summary=summary, risk_level="low")
  ```
  (Replaces the inline `ReviewResult(status="stale", ...)` the old code returned at line 385.)

- [ ] **Step 4: Thread `diff_hash` into the completed result**

In the final `return ReviewResult(status="completed", ...)` (line 831), add:

```python
            diff_hash=(diff_hash if delta_base_sha is None else None),
```

and compute `diff_hash` once, right after the `if not diff.strip(): ... declined(...)` block near line 420 (see Task 6 Step 3 — that block also runs the #3 lookup). For #2-only correctness now, at minimum set a `diff_hash` local:

```python
        diff_hash = diff_content_hash(diff) if delta_base_sha is None else None
```

(Import `diff_content_hash` from `reva.diff_utils` alongside the existing `filter_diff` import at line 29.)

- [ ] **Step 5: Run tests**

Run: `cd worker && .venv/bin/python -m pytest tests/test_reviewer.py -k "diverged or delta" -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add worker/worker/reviewer.py worker/tests/test_reviewer.py
git commit -m "feat(reviewer): force-push-aware two-tree delta (#2)"
```

---

## Task 6: Reviewer — cross-branch carry-forward (#3)

**Files:**
- Modify: `worker/worker/reviewer.py` (add `_carry_forward_result`, `_finding_from_row`; insert the #3 lookup near line 420)
- Test: `worker/tests/test_reviewer.py`

**Interfaces:**
- Consumes: `find_reusable_review`, `get_all_open_findings_for_pr`, `get_muted_categories` (repos); `diff_content_hash`; existing `_drop_muted_findings` (line 1304), `_recompute_risk_level` (line 1359), `_cap_findings`, `MAX_FINDINGS`.
- Produces: `Reviewer._carry_forward_result(matched: dict, diff: str, repo_config, muted: set[str]) -> ReviewResult` and `Reviewer._finding_from_row(row: dict) -> Finding`.

- [ ] **Step 1: Write the failing tests**

```python
def test_first_review_carries_forward_on_diff_hash_match(reviewer, fakes):
    fakes.repos.last_review = None                       # first review of this PR
    fakes.repos.reusable = {"id": 77, "pull_request_id": 5, "pr_number": 101}
    fakes.repos.open_findings_by_pr = {5: [
        {"id":1,"file_path":"custom_addons/m/x.py","line_start":10,"title":"Zero-recipient gap",
         "body":"...","severity":"major","category":"correctness","github_comment_id":9}]}
    result = reviewer.execute(params_first_review)
    assert result.status == "completed"
    assert result.carried_from_run_id == 77
    assert result.model is None and result.estimated_cost_usd in (None, 0, 0.0)
    assert any(f.severity == "major" for f in result.findings)
    assert fakes.claude.run_called is False              # NO Claude call
    assert ("reviewer", "review_carried_forward") in fakes.ops_events_kinds

def test_carry_forward_drops_muted_and_respects_first_review_only(reviewer, fakes):
    fakes.repos.last_review = {"id": 5, "head_sha": "prior"}   # NOT first review
    fakes.repos.reusable = {"id": 77, "pull_request_id": 5, "pr_number": 101}
    result = reviewer.execute(params_ahead)               # a normal delta review, no carry-forward
    assert result.carried_from_run_id is None

def test_carry_forward_disabled_by_flag(reviewer, fakes, monkeypatch):
    monkeypatch.setattr("reva.config.CROSS_BRANCH_REUSE", False)
    fakes.repos.last_review = None
    fakes.repos.reusable = {"id": 77, "pull_request_id": 5, "pr_number": 101}
    result = reviewer.execute(params_first_review)
    assert result.carried_from_run_id is None            # real review path

def test_explicit_trigger_never_carries_forward(reviewer, fakes):
    fakes.repos.last_review = None
    fakes.repos.reusable = {"id": 77, "pull_request_id": 5, "pr_number": 101}
    result = reviewer.execute(params_comment_trigger)    # trigger_event="comment"
    assert result.carried_from_run_id is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd worker && .venv/bin/python -m pytest tests/test_reviewer.py -k "carry or carried or reuse" -v`
Expected: FAIL.

- [ ] **Step 3: Insert the #3 lookup**

In `worker/worker/reviewer.py`, immediately after the empty-diff `declined(...)` block (line 410-420, right after the `if not diff.strip():` return) and BEFORE `changed_files_payload = ...` (line 422), add:

```python
        # 5c. diff_hash + cross-branch reuse (#3). Computed here — after filter_diff,
        # before the size guards — so store-point == lookup-point and a matched
        # carry-forward pre-empts a "diff too large" decline (reuse spends no Claude).
        diff_hash = diff_content_hash(diff) if delta_base_sha is None else None
        explicit = params.trigger_event in ("comment", "manual_requeue")
        if (delta_base_sha is None and last_review is None and not explicit
                and config.CROSS_BRANCH_REUSE and repo_config.cross_branch_reuse):
            matched = self.repos.find_reusable_review(
                params.repository_id, diff_hash, params.pull_request_id)
            if matched:
                log.info("review_carried_forward",
                         matched_pr=matched["pr_number"], matched_run=matched["id"])
                self._record_ops_event("reviewer", "info", "review_carried_forward",
                    {"pr": pr_number, "matched_pr": matched["pr_number"],
                     "matched_run_id": matched["id"]})
                muted = self.repos.get_muted_categories(params.repository_id)
                return self._carry_forward_result(matched, diff, repo_config, muted)
```

Add `import reva.config as config` (or `from reva import config`) at the top if not present, and confirm `diff_content_hash` is imported (Task 5 Step 4).

**Important:** remove the standalone `diff_hash = ...` line added in Task 5 Step 4 if it now duplicates this one — there must be exactly one `diff_hash` assignment, here. The final `ReviewResult` (Task 5 Step 4) already reads `diff_hash`.

- [ ] **Step 4: Implement the builder + row mapper**

Add these methods to `Reviewer` (near the other helpers after line 854):

```python
    def _finding_from_row(self, row: dict) -> Finding:
        # get_open_findings_for_pr rows omit line_end/suggestion/confidence — carried
        # findings are display-only re-posts, so fill safe defaults.
        return Finding(
            severity=row["severity"], category=row["category"],
            file=row["file_path"], line_start=row["line_start"],
            line_end=row["line_start"], title=row["title"], body=row["body"],
            suggestion=None, confidence=1.0, is_odoo_specific=False,
        )

    def _carry_forward_result(
        self, matched: dict, diff: str, repo_config: "RepoConfig", muted: set[str],
    ) -> ReviewResult:
        rows = self.repos.get_all_open_findings_for_pr(matched["pull_request_id"])
        findings = [self._finding_from_row(r) for r in rows]
        findings = _drop_muted_findings(findings, muted)
        findings = _cap_findings(findings, MAX_FINDINGS)
        risk = _recompute_risk_level(findings)
        note = (f"Content matches already-reviewed #{matched['pr_number']} "
                f"(run #{matched['id']}); verdict carried forward. "
                f"Reply `/review` to force a fresh review.")
        now = datetime.now(timezone.utc)
        return ReviewResult(
            status="completed", summary=note, risk_level=risk, findings=findings,
            diff=diff, model=None, prompt_version=None,
            started_at=now, completed_at=now, duration_ms=0,
            input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_creation_tokens=0,
            estimated_cost_usd=0.0, delta_base_sha=None,
            diff_hash=diff_content_hash(diff),
            carried_from_run_id=matched["id"],
            block_on_severity=repo_config.block_on_severity,
        )
```

(`Finding`, `datetime`, `timezone`, `_drop_muted_findings`, `_recompute_risk_level`, `_cap_findings`, `MAX_FINDINGS` are all already imported/defined in `reviewer.py`. `RepoConfig` type is only a hint — quote it or import for typing.)

- [ ] **Step 5: Run tests**

Run: `cd worker && .venv/bin/python -m pytest tests/test_reviewer.py -k "carry or carried or reuse or diverged or delta" -v`
Expected: PASS.

- [ ] **Step 6: Full trio + ruff**

Run: `make test && ruff check reva worker/worker api/app scheduler/scheduler`
Expected: green.

- [ ] **Step 7: Commit**

```bash
git add worker/worker/reviewer.py worker/tests/test_reviewer.py
git commit -m "feat(reviewer): cross-branch diff_hash carry-forward (#3)"
```

---

## Task 7: API — expose `carried_from`

**Files:**
- Modify: `api/app/queries/reviews.py` (add to the run dict(s) at lines ~69, ~147, ~240), `api/app/routes/v1/reviews.py` (response model if typed)
- Test: `api/tests/test_v1_reviews.py`

**Interfaces:**
- Produces: each review row in the `/api/v1` reviews payload gains `"carried_from"`: `{"run_id": int, "pr": int}` or `null`.

- [ ] **Step 1: Write the failing test**

```python
def test_reviews_expose_carried_from(client_and_db):
    client, db = client_and_db
    # seed: a carried-forward run with carried_from_run_id -> a source run on PR #101
    ...  # reuse the seeding helpers already in this test module
    resp = client.get("/api/v1/reviews", headers=AUTH)
    row = next(r for r in resp.json()["items"] if r["carried_from"])
    assert row["carried_from"]["run_id"] == source_run_id
    assert row["carried_from"]["pr"] == 101
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd api && .venv/bin/python -m pytest tests/test_v1_reviews.py -k carried_from -v`
Expected: FAIL (`KeyError`).

- [ ] **Step 3: Implement**

In `api/app/queries/reviews.py`, wherever the query selects run columns, add `ReviewRun.carried_from_run_id` and resolve the source PR number with a self-join or a second lookup. In each returned run dict (lines ~69, ~147, ~240), add:

```python
                "carried_from": (
                    {"run_id": rr.carried_from_run_id, "pr": carried_from_pr_by_run.get(rr.carried_from_run_id)}
                    if rr.carried_from_run_id else None
                ),
```

Build `carried_from_pr_by_run` once per query (a dict from `carried_from_run_id` → source PR number) by selecting `ReviewRun.id, PullRequest.pr_number` for the referenced ids. If a typed Pydantic response model exists in `routes/v1/reviews.py`, add `carried_from: CarriedFrom | None = None` with a small `CarriedFrom(BaseModel){run_id:int; pr:int|None}`.

- [ ] **Step 4: Run test to verify it passes**

Run: same command. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add api/app/queries/reviews.py api/app/routes/v1/reviews.py api/tests/test_v1_reviews.py
git commit -m "feat(api): expose carried_from on reviews"
```

---

## Task 8: TUI — "carried from #N" label

**Files:**
- Modify: `tui/internal/api/types.go` (Review struct), `tui/internal/api/client.go` + `iface.go` + `mock.go` (if the field must thread through), `tui/internal/ui/*reviews*.go` (render)
- Test: `tui/internal/ui/*_test.go` (extend an existing reviews-view test)

**Interfaces:**
- Consumes: the `carried_from` JSON field from Task 7.

- [ ] **Step 1: Add the field to the Review type**

In `tui/internal/api/types.go`, on the Review struct:

```go
	CarriedFrom *CarriedFrom `json:"carried_from,omitempty"`
```
```go
type CarriedFrom struct {
	RunID int `json:"run_id"`
	PR    int `json:"pr"`
}
```

- [ ] **Step 2: Write the failing test**

Extend the existing reviews-view test (find it with `grep -rln "risk" tui/internal/ui`): assert a row whose `CarriedFrom` is set renders a `carried from #101` marker. Follow the assertion style already used there (usually `strings.Contains(view, ...)`).

- [ ] **Step 3: Run it to verify it fails**

Run: `cd tui && go test ./internal/ui/ -run Carried -v`
Expected: FAIL.

- [ ] **Step 4: Render the label**

In the reviews view render function, when `r.CarriedFrom != nil`, append `fmt.Sprintf(" (carried from #%d)", r.CarriedFrom.PR)` to the row's status/summary cell. Update `mock.go` to populate the field for demo mode (`go run . --demo`).

- [ ] **Step 5: Run test + build/vet**

Run: `cd tui && go test ./... && go build ./... && go vet ./...`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add tui/
git commit -m "feat(tui): show carried-forward reviews"
```

---

## Task 9: Real-git integration tests

**Files:**
- Create/Modify: `worker/tests/integration/test_two_tree_delta.py` (or the existing integration test dir; gate with the same marker `make test-integration` uses)

**Interfaces:** none (end-to-end assertions on real git).

- [ ] **Step 1: Write the fixtures + tests**

Build real repos (as in Task 3 Step 1) and assert `ClaudeCodeRunner.two_tree_diff` for four cases:
1. **amend, same base** → `("<delta>", "ok")`, delta contains only the changed file.
2. **rebase onto a newer base** (advance `main`, rebase the feature commit onto it) → `(None, "base_moved")`.
3. **prior object evicted** (delete it from the cache clone: `git -C cache prune`/rm the object, or clone fresh so it was never present) → `(None, "object_missing")`, no raise.
4. **cold cache** (no cache clone) → `(None, "cold_cache")`.

```python
import pytest
pytestmark = pytest.mark.integration  # match the marker make test-integration selects
```

- [ ] **Step 2: Run**

Run: `make test-integration` (or `cd worker && .venv/bin/python -m pytest tests/integration/test_two_tree_delta.py -v`)
Expected: PASS on a machine with `git`.

- [ ] **Step 3: Commit**

```bash
git add worker/tests/integration/
git commit -m "test(reviewer): real-git two_tree_diff integration cases"
```

---

## Self-Review

**Spec coverage:**
- #2 force-push delta + base-unchanged gate + lock-free git + degrade-in-place → Tasks 3, 5. ✅
- #2 empty-delta → same-PR carry-forward (not `skipped`) → Task 5 Step 3. ✅
- #3 `diff_hash` fingerprint (index-stripped, pre-skip_paths), first-review-only, delta-run exclusion, explicit-trigger bypass, global + per-repo flag → Tasks 1, 2, 6. ✅
- Carry-forward = PR-wide open findings, current mutes, recomputed risk, block_on_severity from current config, 0-cost result → Task 6 Step 4. ✅
- Migration/ORM/ReviewResult/persistence → Task 1. ✅
- Ops events (`delta_base_moved`/`delta_object_missing`/`delta_empty_carry_forward`/`review_carried_forward`) → Tasks 5, 6. ✅
- API `carried_from` + TUI label → Tasks 7, 8. ✅
- Real-git integration incl. evicted-object → Task 9. ✅
- OUT of scope confirmed absent: no `content_hash`, no per-addon table, no cross-branch partial delta, no core-version gating. ✅

**Placeholder scan:** all code steps carry concrete code; the `...` in Tasks 7/8 tests points at reusing existing seeding/assertion helpers in those specific test modules (named), not undefined behavior. Signatures used downstream (`two_tree_diff`, `find_reusable_review`, `_carry_forward_result`, `diff_content_hash`) are all defined in earlier tasks with matching names/types.

**Type consistency:** `two_tree_diff` returns `(str|None, str)` everywhere; `find_reusable_review` returns `{id, pull_request_id, pr_number}` and callers read exactly those keys; `matched` dict keys (`id`, `pull_request_id`, `pr_number`) match between Task 5's empty-delta path, Task 6's lookup, and `_carry_forward_result`; `diff_hash` has exactly one assignment point (Task 6 Step 3; Task 5 Step 4 notes to remove its interim line).
