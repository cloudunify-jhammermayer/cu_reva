# Change summary follow-ups — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close seven gaps around the change-summary feature: reuse stored module lists, skip drafts with nothing to deploy, route the branch fallback to the repo's declared Odoo instance, reap stuck pending notes, show change notes in the API and TUI, keep the release on a requeue, and test the Odoo header on tasks.

**Architecture:** Everything builds on what the feature already staged. The merge job reads stored lists before it asks GitHub. `resolve_ticket_by_id` takes the instance a repo declares in `.claude-review.yml`, resolved by one worker helper that both fallback callers use. The scheduler gets a second reaper beside the review reaper and enqueues a small delivery job. The TUI Tickets tab gains a third feed from a new `/api/v1/change-notes` endpoint.

**Tech Stack:** Python 3.14 (`reva/`, `worker/`, `api/`, `scheduler/`), plain SQL migration, pytest with SQLite, Go / Bubble Tea (`tui/`), Odoo 19 tests.

**Spec:** `docs/superpowers/specs/archive/2026-09-30-change-summary-followups-design.md`

## Global Constraints

- **No commits, no push.** Both repos already hold the whole change-summary feature staged and uncommitted; this plan adds to the same staged change. Every task ends with `git add` of its own files only. Never `git stash`, `checkout`, `restore`, `reset` or `clean`.
- cu_reva root: `/home/joseph/Projects/Cloudunify/cu_reva`, branch `feat/change-summary-modules`. Odoo repo: `/home/joseph/Projects/Cloudunify/Cloudunify`, branch `Prod`; its ` M 3rd_party_addons/cu/timetracking` and `?? .claude/` are foreign, never touched.
- Tests: `cd worker && .venv/bin/python -m pytest tests/<file> -q` (same under `api/`, `scheduler/`); all: `make test`; lint: `worker/.venv/bin/ruff check reva worker/worker api/app scheduler/scheduler` (ruff is not on PATH). TUI: `cd tui && go build ./... && go vet ./... && go test ./...`.
- Baseline: worker 1858 passed / 15 skipped with no warnings; api 420 passed with 26 pre-existing DeprecationWarnings (`HTTP_422_UNPROCESSABLE_ENTITY`, two route files outside this work); scheduler 45 passed / 1 skipped.
- TDD: failing test first, then the code. Tests assert behaviour; a test without an assertion is a defect.
- A caught-and-degraded error both logs and calls `writers.record_ops_event(...)`. A GitHub `TransientError` propagates so RQ retries.
- Ops events exactly as the spec names them: `odoo_callback` / `warning` / `unknown_repo_instance`; `odoo_callback` / `warning` / `repo_instance_config_failed`; `change_note` / `warning` / `stale_pending_reaped`; `change_note` / `warning` / `reaper_enqueue_failed`.
- Existing behaviour that must not change: the delivery rule (a ticket with REVA issues waits until all are closed; one without ships once no note is pending); `change_notes` dedup on (repo, pr, ticket); stored `modules` / `submodules` are never overwritten; closing refs win over the branch; rung 1 of `resolve_ticket_by_id`; every existing test stays green unless a task names it.
- Migrations: numbered plain SQL, `ADD COLUMN IF NOT EXISTS`, matching ORM column, a comment header like the neighbouring files. The next free number is `053`.
- Match neighbouring style and comment density. English. Nothing beyond the task.

## Review Focus

1. A re-run whose rows already store the lists must not call GitHub's changed-files endpoint, and a row created on that re-run for a second ticket must still get the lists. Tests in Task 1.
2. A failed lookup (`None`) is not "nothing to deploy": the note is still drafted. Test in Task 1.
3. A repo that declares an instance REVA does not know must produce no ticket, not a ticket on the default instance. Tests in Task 2.
4. A note that is pending for less than the budget wait must not be reaped. Test in Task 3.
5. A run created before migration 053 requeues without a release and does not crash. Test in Task 4.
6. `enter` on a notes-only TUI row opens the journey instead of "no GitHub issues for this ticket". Test in Task 5.

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `reva/db/writers.py` | `get_stored_change_note_lists`; `get_active_odoo_instance_id_by_name`; `reap_stale_pending_change_notes`; release columns on run create/read | 1, 2, 3, 4 |
| `worker/worker/change_note_runner.py` | stored lists first; nothing-to-deploy exit; declared instance in the fallback; `run_change_note_delivery` | 1, 2, 3 |
| `reva/ticket_links.py` | `resolve_ticket_by_id(..., instance_id=None)` | 2 |
| `worker/worker/repo_instance.py` (new) | `declared_instance_id`, `UnknownRepoInstance` | 2 |
| `worker/worker/board_status_runner.py` | declared instance in the work-status fallback | 2 |
| `worker/worker/change_note_tasks.py` | `deliver_change_notes` task entry | 3 |
| `scheduler/scheduler/main.py`, `settings.py`, both compose files | the reaper call, its threshold, env passthrough | 3 |
| `db/migrations/053_ticket_issue_runs_release.sql`, `reva/db/models.py` | the two columns | 4 |
| `api/app/routes/v1/ticket_issues.py` | requeue rebuilds `release` | 4 |
| `api/app/routes/v1/change_notes.py`, `api/app/schemas/change_notes.py`, `api/app/queries/change_notes.py` (new), `api/app/routes/v1/__init__.py` | the feed | 5 |
| `tui/internal/api/{types,iface,client,mock}.go`, `tui/internal/ui/{tickets,messages,app}.go` | third feed, notes-only rows | 5 |
| Cloudunify `custom_addons/cu_reva_ticket_analysis/tests/test_callback.py` | header on a task | 6 |
| `HANDOFF.md`, `README.md`, `docs/technical.md`, `tui/README.md`, spec + plan → `archive/` | docs | 7 |

---

### Task 1: Stored lists first, and no draft when nothing is to deploy

**Files:**
- Modify: `reva/db/writers.py` (new `get_stored_change_note_lists`, directly after `record_change_note_modules`)
- Modify: `worker/worker/change_note_runner.py` (`run_change_note`)
- Test: `worker/tests/test_change_note_modules.py` (append), `worker/tests/test_change_note_delivery.py` (append; one existing test changed)

**Interfaces:**
- Produces: `writers.get_stored_change_note_lists(db: Database, repo_full_name: str, pr_number: int) -> tuple[list[str], list[str]] | None` — `(modules, submodules)` of a `change_notes` row of that PR whose `modules` is not NULL (lowest id), else `None`. `repo_full_name` is lowercased like the other change-note writers do.
- Produces: `run_change_note` may return `{"status": "nothing_to_deploy"}`.

- [ ] **Step 1: Write the failing tests**

Append to `worker/tests/test_change_note_modules.py`:

```python
# --- stored lists of a PR (follow-ups, spec 2026-09-30) -------------------------


def test_stored_lists_are_none_for_an_unknown_pr(db):
    assert writers.get_stored_change_note_lists(db, "acme/widgets", 7) is None


def test_stored_lists_are_none_while_no_row_has_them(db):
    _note(db)
    assert writers.get_stored_change_note_lists(db, "acme/widgets", 7) is None


def test_stored_lists_come_from_a_row_of_the_same_pr(db):
    writers.record_change_note_modules(db, _note(db), ["cu_auth"], ["3rd_party_addons/cu/queue"])
    _note(db, pr_number=8)
    assert writers.get_stored_change_note_lists(db, "Acme/Widgets", 7) == (
        ["cu_auth"], ["3rd_party_addons/cu/queue"]
    )
    assert writers.get_stored_change_note_lists(db, "acme/widgets", 8) is None


def test_stored_empty_lists_count_as_stored(db):
    writers.record_change_note_modules(db, _note(db), [], [])
    assert writers.get_stored_change_note_lists(db, "acme/widgets", 7) == ([], [])
```

`worker/tests/test_change_note_delivery.py`: the existing `test_a_rerun_keeps_the_stored_modules` becomes

```python
def test_a_rerun_reuses_the_stored_lists_without_asking_github(cn_ctx):
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    _seed_run(s["db"], issues=[{"number": 50, "state": "open"}])
    s["github"].get_changed_files.return_value = _changed("custom_addons/cu_auth/models/login.py")
    run_change_note(_cn_params())
    s["github"].get_changed_files.return_value = _changed("custom_addons/cu_other/models/x.py")
    run_change_note(_cn_params())
    assert s["github"].get_changed_files.call_count == 1
    assert _note_rows(s["db"])[0].modules == ["cu_auth"]
```

Append to the same file (they use `_branch_params` and `_seed_default_instance` from the branch-linked block):

```python
# --- follow-ups (spec 2026-09-30): stored lists, nothing to deploy --------------


def test_a_row_created_on_a_rerun_gets_the_stored_lists(cn_ctx):
    # The PR's first ticket already stores the lists; a second ticket resolved
    # on the re-run gets the same ones, still without a GitHub call.
    from reva.db import writers
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    _seed_run(s["db"], issues=[{"number": 50, "state": "open"}])
    other_id, _ = writers.get_or_create_change_note(
        s["db"], "acme/widgets", 7, 4242, _INSTANCE, _MODEL
    )
    writers.record_change_note_modules(s["db"], other_id, ["cu_auth"], ["3rd_party_addons/cu/queue"])
    run_change_note(_cn_params())
    s["github"].get_changed_files.assert_not_called()
    row = next(r for r in _note_rows(s["db"]) if r.ticket_id == _TICKET)
    assert (row.modules, row.submodules) == (["cu_auth"], ["3rd_party_addons/cu/queue"])


def test_branch_linked_pr_without_modules_or_submodules_is_not_drafted(cn_ctx, monkeypatch):
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    _seed_default_instance(s["db"])
    s["github"].get_changed_files.return_value = _changed(".github/workflows/ci.yml")
    monkeypatch.setattr(
        "worker.change_note_runner.build_note",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Claude must not be called")),
    )
    assert run_change_note(_branch_params()) == {"status": "nothing_to_deploy"}
    assert _note_rows(s["db"]) == []
    assert s["odoo"].calls == []


def test_branch_linked_pr_that_moves_a_submodule_is_drafted(cn_ctx):
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    _seed_default_instance(s["db"])
    s["github"].get_changed_files.return_value = _changed(submodules=["3rd_party_addons/cu/queue"])
    assert run_change_note(_branch_params()) == {"status": "completed", "delivered": 1}


def test_branch_linked_pr_with_a_failed_lookup_is_still_drafted(cn_ctx):
    # None means "could not look", not "nothing to deploy".
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    _seed_default_instance(s["db"])
    s["github"].get_changed_files.side_effect = PermanentError("GitHub 404")
    assert run_change_note(_branch_params()) == {"status": "completed", "delivered": 1}


def test_issue_linked_pr_without_modules_is_still_drafted(cn_ctx):
    from worker.change_note_runner import run_change_note

    s = cn_ctx
    _seed_run(s["db"], issues=[{"number": 50, "state": "closed"}])  # ready
    s["github"].get_changed_files.return_value = _changed("docs/releases/lollipop.md")
    assert run_change_note(_cn_params()) == {"status": "completed", "delivered": 1}
```

Existing branch-linked tests in that file call `run_change_note(_branch_params(...))` with the fixture's default `get_changed_files.return_value = []`, which is now "nothing to deploy". Give each of them a changed addon file so they keep testing what they test: in `_branch_params`'s callers' fixture path, the smallest change is to make the `cn_ctx` fixture default `github.get_changed_files.return_value = [{"filename": "custom_addons/cu_auth/models/login.py", "status": "modified", "patch": "@@ -1 +1 @@\n-a\n+b"}]` instead of `[]`. Then these Task-3-era assertions change with it, and only these: tests that read `row.modules` / `modules` after a run without setting their own `get_changed_files` value. Run the file, fix each such assertion to `["cu_auth"]`, and list every test you touched in your report. `test_a_pr_outside_the_addons_stores_an_empty_list` sets its own value and stays.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd worker && .venv/bin/python -m pytest tests/test_change_note_modules.py tests/test_change_note_delivery.py -q`
Expected: `AttributeError: … 'get_stored_change_note_lists'`; the re-run test fails on `call_count == 2`; the nothing-to-deploy test fails on the returned status.

- [ ] **Step 3: Writer**

`reva/db/writers.py`, directly after `record_change_note_modules`:

```python
def get_stored_change_note_lists(
    db: Database, repo_full_name: str, pr_number: int
) -> tuple[list[str], list[str]] | None:
    """(modules, submodules) already stored for the PR, or None when no row of
    it has them yet. The lists are per PR, so any row that has them serves."""
    with db.session() as s:
        row = s.execute(
            select(ChangeNote.modules, ChangeNote.submodules)
            .where(
                ChangeNote.repo_full_name == repo_full_name.lower(),
                ChangeNote.pr_number == pr_number,
                ChangeNote.modules.is_not(None),
            )
            .order_by(ChangeNote.id.asc())
            .limit(1)
        ).first()
    if row is None:
        return None
    return list(row.modules), list(row.submodules or [])
```

On SQLite a JSON column holding SQL NULL and one holding JSON `null` can differ; the tests above pin the behaviour for both "never written" and `[]`. If `is_not(None)` does not filter a never-written row on SQLite, filter in Python after fetching the PR's rows instead, and say so in the report.

- [ ] **Step 4: Merge job**

`worker/worker/change_note_runner.py`, in `run_change_note`, replace the line `touched = _affected_modules(ctx, job_params, owner, name, repo, pr_number)` with:

```python
    # Lists stored by an earlier run of this job serve every ticket of the PR;
    # only a first run asks GitHub, and it does so before any row is written.
    touched = writers.get_stored_change_note_lists(ctx.db, repo, pr_number)
    if touched is None:
        touched = _affected_modules(ctx, job_params, owner, name, repo, pr_number)
    if (
        touched is not None
        and not touched[0]
        and not touched[1]
        and all(ref.run_id is None for ref in tickets)
    ):
        # Branch-linked PR that touches no addon and moves no submodule: there
        # is nothing to deploy, so no draft and no chatter note.
        logger.info("change_note_nothing_to_deploy", repo=repo, pr=pr_number)
        return {"status": "nothing_to_deploy"}
```

The storing line below it (`if touched is not None and row["modules"] is None: …`) stays as it is.

- [ ] **Step 5: Run the tests, then everything**

Run: `cd worker && .venv/bin/python -m pytest tests/test_change_note_modules.py tests/test_change_note_delivery.py -q`, then `make test` and ruff from the repo root. Expected: green, baseline warnings only.

```bash
git add reva/db/writers.py worker/worker/change_note_runner.py worker/tests/test_change_note_modules.py worker/tests/test_change_note_delivery.py
```

---

### Task 2: The repo's declared Odoo instance in the branch fallback

**Files:**
- Modify: `reva/ticket_links.py` (`resolve_ticket_by_id`)
- Modify: `reva/db/writers.py` (new `get_active_odoo_instance_id_by_name`, directly after `get_odoo_instance`)
- Create: `worker/worker/repo_instance.py`
- Modify: `worker/worker/change_note_runner.py` (`_fallback_tickets`)
- Modify: `worker/worker/board_status_runner.py` (the ticket-level fallback block)
- Test: `worker/tests/test_ticket_links.py`, `worker/tests/test_repo_instance.py` (new), `worker/tests/test_change_note_delivery.py`, `worker/tests/test_board_status_runner.py`

**Interfaces:**
- Produces: `resolve_ticket_by_id(db, repo_full_name, ticket_id, default_model="project.task", *, strict_model=False, instance_id: int | None = None) -> tuple[int, str] | None`
- Produces: `writers.get_active_odoo_instance_id_by_name(db: Database, name: str) -> int | None`
- Produces: `worker.repo_instance.declared_instance_id(ctx, repo: str, installation_id: int, log) -> int | None` and `class UnknownRepoInstance(Exception)` (its `args[0]` is the declared name)

**Behaviour of `resolve_ticket_by_id` with `instance_id` given** (without it: unchanged):
- rung 1 (`ticket_issue_runs` for this repo and ticket): unchanged.
- the `ticket_analyses` candidates query gains `TicketAnalysis.odoo_instance_id == instance_id`.
- the last rung returns `(instance_id, default_model)`; the default-instance query is not run.

**Behaviour of `declared_instance_id`:**

```python
"""The Odoo instance a repo declares in `.claude-review.yml` (`odoo_instance`)."""

from __future__ import annotations

from reva.db import writers
from reva.errors import TransientError
from worker.repo_config import load_repo_config


class UnknownRepoInstance(Exception):
    """The repo names an instance REVA has no active instance for."""


def declared_instance_id(ctx, repo: str, installation_id: int, log) -> int | None:
    """Id of the active instance the repo's default-branch config names, or
    None when it names none (repo not registered, no config, no key, or a
    config that cannot be read: ops event recorded, the caller keeps its
    default ladder). Raises UnknownRepoInstance when the repo names an instance
    that is not an active one: the caller must not fall back to the default
    instance then. TransientError -> RQ retry."""
    row = writers.get_repository_by_full_name(ctx.db, repo)
    if row is None:
        return None

    def _failed(reason: str) -> None:
        log.warning("repo_instance_config_failed", repo=repo, error=reason)
        writers.record_ops_event(
            ctx.db, "odoo_callback", "warning", "repo_instance_config_failed",
            {"repo": repo, "error": reason[:300]},
        )

    try:
        token = ctx.github.get_installation_token(installation_id)
        cfg = load_repo_config(
            ctx.github, token, row["owner"], row["name"], row["default_branch"],
            on_invalid=_failed,
        )
    except TransientError:
        raise
    except Exception as exc:  # noqa: BLE001 — degrade to the default ladder, visibly
        _failed(str(exc))
        return None
    if not cfg.odoo_instance:
        return None
    instance_id = writers.get_active_odoo_instance_id_by_name(ctx.db, cfg.odoo_instance)
    if instance_id is None:
        raise UnknownRepoInstance(cfg.odoo_instance)
    return instance_id
```

**Callers.** In both, the declared instance is resolved only when a ticket id was extracted, directly before `resolve_ticket_by_id`, and passed as `instance_id=`. On `UnknownRepoInstance` the caller logs a warning, records `odoo_callback` / `warning` / `unknown_repo_instance` with `{"repo", "pr", "ticket_id", "instance": <declared name>}` and resolves no ticket (`_fallback_tickets` returns `[]`; in `board_status_runner` `fallback` stays `None`). `_fallback_tickets` uses `job_params["installation_id"]`; `board_status_runner` uses `job_params["installation_id"]` too.

- [ ] **Step 1: Write the failing tests**

`worker/tests/test_ticket_links.py` (use that file's existing fixtures and seeding helpers; read them first): with `instance_id` given —
1. `test_declared_instance_is_the_last_rung`: a ticket unknown to REVA resolves to `(instance_id, default_model)` although another instance is the active default.
2. `test_declared_instance_filters_the_analysis_rungs`: a `ticket_analyses` row of ANOTHER instance with the same ticket id is ignored; one of the declared instance is used (its recorded model wins over the guess).
3. `test_a_run_of_the_repo_still_wins_over_the_declared_instance`: rung 1 returns the run's instance.
4. `test_without_a_declared_instance_the_ladder_is_unchanged`: the existing default-instance behaviour, called with `instance_id=None`.

`worker/tests/test_repo_instance.py` (new; SQLite `db` fixture as in `test_change_note_modules.py`, a `SimpleNamespace(db=db, github=MagicMock())` ctx, a `Repository` row seeded like `_seed_repo` in `test_change_note_delivery.py`, `OdooInstance` rows): one test per row of the spec's table — not registered → `None` and `get_file_content` not called; no config file (`get_file_content` returns `None`) → `None`; no key → `None`; declared and active → the id; declared but no such instance → `UnknownRepoInstance` with the name; declared but the instance is inactive → `UnknownRepoInstance`; malformed YAML → `None` plus the `repo_instance_config_failed` ops event; `PermanentError` from `get_file_content` → `None` plus the ops event; `TransientError` → propagates. Assert the config is read at the repo's `default_branch`.

`worker/tests/test_change_note_delivery.py` (append):
1. `test_branch_ticket_goes_to_the_instance_the_repo_declares`: two active instances, the default one and `customer`; the repo (seeded with `_seed_repo`) declares `odoo_instance: customer`; `build_odoo_client` is patched to record the instance id it is asked for; the summary is delivered through the `customer` instance and the note row carries its id.
2. `test_branch_ticket_of_a_repo_declaring_an_unknown_instance_gets_no_note`: returns `{"status": "no_tickets"}`, no row, ops event `unknown_repo_instance`, nothing sent.

`worker/tests/test_board_status_runner.py` (append, in the style of that file's fallback tests): the ticket signal goes to the declared instance; a repo declaring an unknown instance sends no signal and records `unknown_repo_instance`.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd worker && .venv/bin/python -m pytest tests/test_ticket_links.py tests/test_repo_instance.py tests/test_change_note_delivery.py tests/test_board_status_runner.py -q`
Expected: `TypeError: … unexpected keyword argument 'instance_id'`, `ModuleNotFoundError: worker.repo_instance`, and the four runner tests fail.

- [ ] **Step 3: Implement**

`writers.get_active_odoo_instance_id_by_name`: `select(OdooInstance.id).where(OdooInstance.name == name, OdooInstance.active.is_(True))`, `scalar_one_or_none()` (names are unique). `resolve_ticket_by_id` and its docstring per the behaviour block (add one sentence to the docstring: what `instance_id` is and that rung 1 ignores it). `worker/worker/repo_instance.py` as above. The two callers per the Callers paragraph.

- [ ] **Step 4: Run the tests, then everything**

Same command as Step 2, then `make test`, ruff, and `worker/.venv/bin/mypy reva worker/worker --ignore-missing-imports 2>/dev/null | grep -E "ticket_links|repo_instance|change_note_runner|board_status_runner"` (advisory; the pre-existing `reva/ticket_links.py` "Incompatible types in assignment" line may shift by a few lines, nothing new may appear). Expected: green.

```bash
git add reva/ticket_links.py reva/db/writers.py worker/worker/repo_instance.py worker/worker/change_note_runner.py worker/worker/board_status_runner.py worker/tests/test_ticket_links.py worker/tests/test_repo_instance.py worker/tests/test_change_note_delivery.py worker/tests/test_board_status_runner.py
```

---

### Task 3: Reap stuck pending notes

**Files:**
- Modify: `reva/db/writers.py` (new `reap_stale_pending_change_notes`, directly after `has_pending_change_notes`)
- Modify: `worker/worker/change_note_runner.py` (new `run_change_note_delivery`), `worker/worker/change_note_tasks.py`
- Modify: `scheduler/scheduler/settings.py`, `scheduler/scheduler/main.py`
- Modify: `docker-compose.yml`, `docker-compose.prod.yml` (scheduler service: pass `REVA_BUDGET_WAIT_MAX_SECONDS`, same form as the worker service's line)
- Test: `worker/tests/test_change_note_modules.py` (writer), `worker/tests/test_change_note_delivery.py` (delivery job), `scheduler/tests/` (the file that tests `maybe_requeue_budget_failures`; read it for the pattern)

**Interfaces:**
- Produces: `writers.reap_stale_pending_change_notes(db: Database, older_than_seconds: int) -> list[dict]` — each dict: `id`, `repo_full_name`, `pr_number`, `odoo_instance_id`, `ticket_id`, `model_name`. Rows `status == "pending"` with `created_at` older than the cutoff become `status="failed"`, `error_message=f"Reaped: stuck in 'pending' >{older_than_seconds}s (worker likely died mid-job)."`, `completed_at=now`. `with_for_update(skip_locked=True)` like `reap_stale_running_reviews`.
- Produces: `worker.change_note_runner.run_change_note_delivery(job_params: dict) -> dict` — params `odoo_instance_id`, `ticket_id`, `model_name`; builds the Odoo client, calls `maybe_deliver_change_notes`, returns `{"status": "completed", "delivered": 0 | 1}`.
- Produces: RQ task `worker.change_note_tasks.deliver_change_notes` (`terminal_on_permanent(run_change_note_delivery)`, added to `__all__`).
- Produces: `scheduler.main.reap_change_notes(db, queue, stale_seconds: int) -> int` (rows reaped) and `Settings.stale_change_note_seconds: int` = `int(os.environ.get("REVA_BUDGET_WAIT_MAX_SECONDS", "172800")) + 7200`.

**`reap_change_notes`:** calls the writer; per reaped row a warning log and the ops event `change_note` / `warning` / `stale_pending_reaped` with `{"repo", "pr", "ticket_id"}`; per distinct `(odoo_instance_id, ticket_id, model_name)` one `queue.enqueue("worker.change_note_tasks.deliver_change_notes", {...}, retry=Retry(max=3, interval=[30, 120, 300]))`; an enqueue exception is logged and recorded as `change_note` / `warning` / `reaper_enqueue_failed` and does not stop the loop over the other tickets. Called from `main()`'s loop in its own `try` / `except Exception: logger.exception("scheduler_change_note_reaper_error")` block, directly after the review reaper.

- [ ] **Step 1: Write the failing tests**

Writer (`worker/tests/test_change_note_modules.py`, append; set `created_at` by updating the row in a session): a pending row older than the threshold is failed, carries the message and a `completed_at`, and is returned; a pending row younger than the threshold is untouched and not returned; completed / failed / skipped_budget rows of any age are untouched; a second call returns `[]`.

Delivery job (`worker/tests/test_change_note_delivery.py`, append): with a ready run and one completed undelivered note, `run_change_note_delivery({"odoo_instance_id": _INSTANCE, "ticket_id": _TICKET, "model_name": _MODEL})` returns `{"status": "completed", "delivered": 1}` and stamps the row (patch `get_context` / `build_odoo_client` the way `cn_ctx` does); with nothing to deliver it returns `delivered: 0`.

Scheduler (new tests beside the budget-requeue tests): two stale pending notes of one ticket and one of another → three ops events `stale_pending_reaped`, two jobs enqueued with the right params and function name, return value 3; a fresh pending note → nothing reaped, nothing enqueued; an enqueue that raises for the first ticket still enqueues the second and records `reaper_enqueue_failed`; `Settings.from_env()` gives `172800 + 7200` by default and follows `REVA_BUDGET_WAIT_MAX_SECONDS`.

- [ ] **Step 2: Run them to verify they fail**, **Step 3: Implement** per the Interfaces block, **Step 4:** `make test`, ruff. Expected: green, baseline warnings only.

```bash
git add reva/db/writers.py worker/worker/change_note_runner.py worker/worker/change_note_tasks.py scheduler/scheduler/settings.py scheduler/scheduler/main.py docker-compose.yml docker-compose.prod.yml worker/tests/test_change_note_modules.py worker/tests/test_change_note_delivery.py scheduler/tests
```

(`git add scheduler/tests` stages only what you changed there; check `git status --short` afterwards.)

---

### Task 4: The release survives a requeue

**Files:**
- Create: `db/migrations/053_ticket_issue_runs_release.sql`
- Modify: `reva/db/models.py` (`TicketIssueRun`, after `plan_date`)
- Modify: `reva/db/writers.py` (`record_ticket_issue_run_created`, `get_ticket_issue_run`)
- Modify: `api/app/routes/v1/ticket_issues.py` (the requeue route's `TicketIssueJobParams(...)`)
- Test: `api/tests/` (the file with the create-issues requeue tests; read it for the pattern), `worker/tests/` (the file that tests `record_ticket_issue_run_created`, if one exists; else the api test covers the writer)

**Interfaces:**
- Produces: columns `ticket_issue_runs.release_id BIGINT`, `release_name TEXT` (both nullable); keys `"release_id"`, `"release_name"` in the dict `writers.get_ticket_issue_run` returns.

Migration:

```sql
-- The Odoo release a create-issues request named (spec
-- 2026-09-30-change-summary-followups): kept on the run so a requeue, which
-- rebuilds its job params from this row, writes the **Release:** line on the
-- issues again. NULL = the request carried no release, or the row predates
-- this migration.
-- Mirrors reva/db/models.py::TicketIssueRun.release_id / .release_name.
ALTER TABLE ticket_issue_runs ADD COLUMN IF NOT EXISTS release_id BIGINT;
ALTER TABLE ticket_issue_runs ADD COLUMN IF NOT EXISTS release_name TEXT;
```

Model: `release_id: Mapped[int | None] = mapped_column(BigInteger)`, `release_name: Mapped[str | None] = mapped_column(Text)` with a two-line comment. Writer: `release_id=params.release.id if params.release else None`, `release_name=params.release.name if params.release else None`. Requeue route, inside the `TicketIssueJobParams(...)` call:

```python
        # The release is kept on the run (migration 053); rows from before it
        # requeue without one.
        release=(
            ReleaseRef(id=row["release_id"], name=row["release_name"] or "")
            if row["release_id"] is not None
            else None
        ),
```

(`ReleaseRef` from `reva.types`; its `date` is not stored and not used.)

- [ ] **Step 1: Write the failing tests** — a create-issues request with `release: {id: 3275, name: "Lollipop", date: "2026-09-30 00:00:00"}`, then a requeue of that run: the enqueued params carry `release.id == 3275` and `release.name == "Lollipop"`; a run created without a release requeues with `release is None`; a run row whose `release_id` is NULL but created through the ORM directly (the pre-migration shape) requeues with `release is None`.
- [ ] **Step 2: Run them to verify they fail**, **Step 3: Implement**, **Step 4:** `make test`, ruff, and `make test-integration` from the repo root (Docker is available; it applies every migration on Postgres 16; the target ignores pytest's exit code, so read the two summary lines). Expected: green.

```bash
git add db/migrations/053_ticket_issue_runs_release.sql reva/db/models.py reva/db/writers.py api/app/routes/v1/ticket_issues.py api/tests worker/tests
```

(Stage only the test files you changed; check `git status --short`.)

---

### Task 5: Change notes in the API and the TUI

**Files:**
- Create: `api/app/queries/change_notes.py`, `api/app/schemas/change_notes.py`, `api/app/routes/v1/change_notes.py`; Modify: `api/app/routes/v1/__init__.py` (register on the master router, beside `ticket_journeys`)
- Create: `api/tests/test_v1_change_notes.py`
- Modify: `tui/internal/api/types.go`, `iface.go`, `client.go`, `mock.go`; `tui/internal/ui/tickets.go`, `messages.go`, `app.go`; tests `tui/internal/api/client_test.go`, `tui/internal/ui/tickets_test.go`
- Modify: `tui/README.md` (the Tickets tab paragraph)

**API.** `GET /api/v1/change-notes?limit=50&offset=0` → `{"items": [...], "total": n}`, newest first (`created_at` desc, `id` desc), `limit` clamped to 200 with the same `clamp_limit` / `clamp_offset` helpers `list_ticket_issue_runs` uses. Item fields exactly: `id`, `repo_full_name`, `pr_number`, `pr_title`, `pr_url`, `odoo_instance_id`, `ticket_id`, `model_name`, `status`, `source`, `modules`, `submodules`, `error_message`, `estimated_cost_usd`, `created_at`, `completed_at`, `delivered_at`. `modules` / `submodules` are `list[str] | None` (None = never looked up). No `note_html`. Follow `api/app/routes/v1/ticket_issues.py::list_ticket_issue_runs` and its query and schema for structure, auth and naming.

API tests: empty → `{"items": [], "total": 0}`; two notes → newest first, all fields present, no `note_html` key; `limit` / `offset` page; unauthenticated → the same status the neighbouring master-key feeds return.

**TUI.**
- `types.go`: `ChangeNoteSummary` (the fields above; nullable ones as pointers, `Modules` / `Submodules` as `[]string`) and `ChangeNotePage{Items, Total}`.
- `iface.go` / `client.go` / `mock.go`: `ChangeNotes(limit int) (*ChangeNotePage, error)` → `/change-notes?limit=%d`. The mock returns demo data that includes one ticket with change notes only (a `project.task` id that no demo analysis or run uses, two notes, modules on one) plus one note for a ticket that also has a run.
- `tickets.go`: `load()` adds the third fetch; a `changeNotesLoadedMsg` (in `messages.go`, routed in `app.go` like `ticketIssueRunsLoadedMsg`) stores the notes grouped per `issueRunKey(model, ticket)`. `ticketRow` gains `notes []api.ChangeNoteSummary`. `rebuildRows` creates a row for a record that has notes only and counts the newest note's `CreatedAt` into `activity`. `repoKey` falls back to the newest note's `RepoFullName` when the row has neither a run nor an analysis URL. The ISSUES cell of a row without a run reads `N notes` (`1 note` for one) when the row has notes, `—` otherwise; rows with a run render as today. A feed error for change notes must not blank the tab: keep the rows from the other two feeds and show the error the way the tab shows a failed issue-runs feed.
- `enter` on a row: with issues → today's detail. Without issues but with notes → open the detail pane with no issue lines and load the journey with the newest note's `OdooInstanceID`, the row's model and ticket id (`detailKey` set the same way). Without both → today's "no GitHub issues for this ticket". The detail view must render with an empty issue list (no index panic; the journey block is what it shows).
- Filter (`/`) and folding work on notes-only rows like on any row.

TUI tests (`tickets_test.go`, in the style of the existing ones there): a notes-only record yields a row under its repo group with `2 notes` in the rendered line; a record with a run and notes renders its issues cell as before; `enter` on the notes-only row sets `detail` and returns a command (the journey load) and does not set the "no GitHub issues" status; the detail view renders without panic for that row; a change-notes feed error keeps the other rows. `client_test.go`: `ChangeNotes` hits `/change-notes?limit=100` and decodes a page with `modules: null` and `modules: ["cu_auth"]`.

- [ ] **Step 1: Write the failing tests (api, then tui)**, **Step 2: Run them to verify they fail**, **Step 3: Implement**, **Step 4:** `cd api && .venv/bin/python -m pytest tests/ -q | tail -1`, ruff, `cd tui && go build ./... && go vet ./... && go test ./...`, and `cd tui && go run . --demo` is NOT run (interactive). Expected: green; api baseline warnings only.

`tui/README.md`, Tickets tab: one sentence that a record with change notes only (a merged PR named it through its branch or title) has a row too, showing its note count, and that `enter` opens its journey.

```bash
git add api/app/queries/change_notes.py api/app/schemas/change_notes.py api/app/routes/v1/change_notes.py api/app/routes/v1/__init__.py api/tests/test_v1_change_notes.py tui
```

(`git add tui` stages only what changed under it; check `git status --short` for stray build output and do not stage binaries.)

---

### Task 6: Odoo header on a task (test only)

**Files (repo `/home/joseph/Projects/Cloudunify/Cloudunify`):**
- Modify: `custom_addons/cu_reva_ticket_analysis/tests/test_callback.py` (`TestRevaChangeSummaryCallback`)

One test, `test_header_on_a_task_follows_its_reva_issues`: create a `project.project` and a `project.task` in it (follow how other tests in the module create tasks; read `tests/` for the pattern); post a change summary for the task (`model_name: "project.task"`, its id, one note with its own PR number and repo) and assert the chatter note has `<strong>Changes merged</strong>` and not `ready for review/deploy`; create a `reva.github.issue` on the task (the FK field is what `task._reva_issue_fk()` returns), post a second summary with a different `note_html` and PR number (the dedup hash must differ) and assert that note is headed `Changes merged — ready for review/deploy`.

No production change, no version bump, no docs.

- [ ] **Step 1: Write the test.** **Step 2: Run** the Odoo test command below with `<TAGS>` = `/cu_reva_ticket_analysis:TestRevaChangeSummaryCallback,/cu_reva_ticket_analysis:TestTicketCallbackContracts,/cu_reva_connector:TestRevaContractsManifest`, output redirected to a log file in the cu_reva plan workspace. Expected: PASS at once (the behaviour is implemented); if it fails, the header rule has a bug on tasks: stop and report, do not change production code. Judge the run by `FAIL: Test…` / `ERROR: Test…` lines and the `odoo.tests.stats` counts (before this test the same three tags gave `cu_reva_connector: 4 tests` and `cu_reva_ticket_analysis: 35 tests`; now 4 and 36), not by the word ERROR: the log always carries rst `(ERROR/3)` lines and `odoo.http: Exception during request handling` tracebacks from tests that post bad payloads on purpose.

```bash
/home/joseph/Projects/Cloudunify/ast-odoo/.venv/bin/python /home/joseph/Projects/Cloudunify/ast-odoo/odoo/odoo-bin -d cu_reva_test --db_host=/run/postgresql --addons-path=/home/joseph/Projects/Cloudunify/Cloudunify/custom_addons,/home/joseph/Projects/Cloudunify/Cloudunify/3rd_party_addons,/home/joseph/Projects/Cloudunify/ast-odoo/enterprise,/home/joseph/Projects/Cloudunify/ast-odoo/odoo/addons -u cu_reva_ticket_analysis,cu_reva_connector --test-enable --test-tags <TAGS> --stop-after-init --http-port=8169 --log-level=warn --log-handler=odoo.tests.stats:INFO
```

- [ ] **Step 3:** `cd /home/joseph/Projects/Cloudunify/Cloudunify && /home/joseph/.local/bin/pre-commit run --files custom_addons/cu_reva_ticket_analysis/tests/test_callback.py` (ruff, ruff-format and pylint must pass; mypy and bandit fail on lines of other files that predate this work), then

```bash
cd /home/joseph/Projects/Cloudunify/Cloudunify && git add custom_addons/cu_reva_ticket_analysis/tests/test_callback.py
```

---

### Task 7: Docs and archive

**Files:**
- Modify: `HANDOFF.md` (top addendum), `README.md`, `docs/technical.md`
- Move: this plan and its spec into `docs/superpowers/plans/archive/` and `docs/superpowers/specs/archive/`

- [ ] **Step 1: HANDOFF.** In the top addendum ("Addendum 2026-09-29 — change summary lists the affected modules"), directly before its `**Deploy:**` paragraph, insert:

```markdown
**Follow-ups in the same change** (spec
`docs/superpowers/specs/archive/2026-09-30-change-summary-followups-design.md`,
plan `docs/superpowers/plans/archive/2026-09-30-change-summary-followups.md`):

- The merge job reuses the lists an earlier run stored for the PR and asks
  GitHub only on a first run.
- A branch-linked PR that touches no addon and moves no submodule gets no
  draft (`nothing_to_deploy`).
- The branch fallback (change notes and work status) resolves an unknown
  ticket to the instance the repo declares with `odoo_instance` in
  `.claude-review.yml`, not to the default instance. A repo naming an instance
  REVA does not know gets no ticket (ops event `unknown_repo_instance`).
- The scheduler fails change notes stuck in `pending` for longer than
  `REVA_BUDGET_WAIT_MAX_SECONDS` + 2 h (ops event `stale_pending_reaped`) and
  enqueues `deliver_change_notes` for their tickets.
- `GET /api/v1/change-notes` feeds the TUI Tickets tab: a record with change
  notes only has a row and a journey.
- A requeued create-issues run keeps its release (migration
  `053_ticket_issue_runs_release.sql`).
```

In the same addendum's `**Deploy:**` paragraph, "migration 052 at boot" becomes "migrations 052 and 053 at boot", and after "prompts v2.22)." add: " A new TUI binary shows the change-note rows."

In its "Not live-validated" paragraph, append: " Of the follow-ups: the config fetch for a repo's declared instance and the scheduler's `deliver_change_notes` enqueue ran against fakes only."

- [ ] **Step 2: README and technical guide.** `README.md`: in the section that documents the `.claude-review.yml` keys, the `odoo_instance` entry gains one sentence: it also decides which Odoo instance a ticket named only by a PR's branch or title belongs to. If the README has no entry for `odoo_instance`, add the sentence to the "Changes-merged notes" section's second paragraph instead and say in the report which you did. `docs/technical.md`, the "Lifecycle sync" bullet: append "A scheduler reaper fails change notes stuck in `pending` and re-triggers delivery for their tickets."

- [ ] **Step 3: Archive.**

```bash
mv docs/superpowers/specs/2026-09-30-change-summary-followups-design.md docs/superpowers/specs/archive/
mv docs/superpowers/plans/2026-09-30-change-summary-followups.md docs/superpowers/plans/archive/
```

In the moved plan the `**Spec:**` line points at the archive path; in the moved spec the `Status:` line becomes `Status: IMPLEMENTED`.

- [ ] **Step 4: Final check.**

```bash
git add HANDOFF.md README.md docs/technical.md docs/superpowers/specs docs/superpowers/plans
```

Run `make test`, ruff and `git status --short`. Expected: green; every line staged; nothing committed.
