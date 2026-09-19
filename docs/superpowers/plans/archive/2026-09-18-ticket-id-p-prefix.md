# `P`-prefixed Ticket Ids Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **State on 2026-09-18:** Tasks 1 and 2 are **already implemented in the working tree**
> (uncommitted) and their boxes are ticked. They stay in the plan in full so the change
> can be reviewed or re-derived step by step. **Task 3 (ship) is the open work.**

**Goal:** A PR that references an Odoo task as `P7624` (branch or title) sends the ticket-level work-status signal to `project.task` 7624 — and never to a helpdesk ticket with the same number.

**Architecture:** The PR→ticket fallback lives in `reva/ticket_links.py`: `extract_ticket_id` reads the id from the head branch or the title, `resolve_ticket_by_id` finds the Odoo instance and model for it. The extractor learns the `P` prefix and reports whether the model was stated explicitly (`strict`); the resolver gets a `strict_model` switch that turns its model guess into a DB filter. `worker/worker/board_status_runner.py` is the only caller of both and passes the flag through.

**Tech Stack:** Python 3.14, SQLAlchemy 2 (`select`), pytest, ruff. Per-service venv: `worker/.venv`.

**Spec:** `docs/superpowers/specs/archive/2026-09-18-ticket-id-p-prefix-design.md`

## Global Constraints

- cu_reva only. No Odoo change, no contract change, no DB migration.
- `H`-prefixed and bare-number references behave exactly as before. Only an explicit `P` is strict.
- `resolve_ticket_by_id`'s new switch is keyword-only and defaults to off.
- Unchanged on purpose: the 9-digit cap, rejection of id `0`, the version-dot guard (`[MIG] 17.0 …`), and "digits must follow the prefix directly" (`feat/portal` is not a ticket).
- **The support-answer-images work is in progress in the same working tree and has priority.** Never edit, stage or commit its files: `db/migrations/050_ticket_analysis_image_count.sql` (a **staged** rename from `048_…`), `docs/superpowers/plans/2026-08-12-support-answer-images-odoo.md`, `reva/db/models.py`, `reva/finding_verifier.py`, `worker/tests/test_strict_tools.py`, `docs/handoff-2026-09-01-release-notes.md`.
- Never use `git add -A`, `git add .` or a bare `git commit`: the index already holds the other work's staged rename.
- `uv.lock` in the repo root is a stray file (created by an accidental `uv run`). Never commit it.
- Run tests with the worker venv, from `worker/`: `.venv/bin/python -m pytest …`. Do **not** use `uv run` — it creates a root `.venv` and `uv.lock`.
- No `git commit` and no `git push` without Joseph's explicit go for that specific action.

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `reva/ticket_links.py` | PR→ticket extraction and resolution | regexes accept `P`; `extract_ticket_id` returns a third value; `resolve_ticket_by_id` gains `strict_model` |
| `worker/worker/board_status_runner.py` | work-status job, only caller of both helpers | unpack three values, pass `strict_model` |
| `worker/tests/test_ticket_links.py` | unit tests for both helpers | new tests; existing extractor expectations gain the third element |
| `worker/tests/test_board_status_runner.py` | job-level tests | one end-to-end test |

---

### Task 1: `resolve_ticket_by_id` — `strict_model` turns the model guess into a filter

Independent of Task 2: the switch defaults to off, so the suite stays green with this task alone.

**Files:**
- Modify: `reva/ticket_links.py` (`resolve_ticket_by_id`)
- Test: `worker/tests/test_ticket_links.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `resolve_ticket_by_id(db: Database, repo_full_name: str, ticket_id: int, default_model: str = "project.task", *, strict_model: bool = False) -> tuple[int, str] | None`. With `strict_model=True` only DB rows whose `model_name == default_model` may match; the default-instance rung and the `None` result are unchanged.

- [x] **Step 1: Write the failing tests**

Append after `test_resolve_by_id_unknown_ticket_honours_helpdesk_hint` in `worker/tests/test_ticket_links.py`. The helpers `_instance`, `_issue_run` (always `helpdesk.ticket`) and `_analysis` already exist at the top of the file.

```python
# --- strict_model: a `P` reference never lands on a helpdesk ticket ------------


def test_resolve_by_id_strict_model_ignores_helpdesk_issue_run(db: Database) -> None:
    # Task and helpdesk ids are separate sequences: helpdesk ticket 210 is known
    # for this repo, but `P210` means project.task 210.
    with db.session() as s:
        s.add(_instance("prod", is_default=True))
        s.add(_issue_run(210, "acme/widgets", [{"number": 1}]))

    with db.session() as s:
        default_id = s.query(OdooInstance).filter_by(name="prod").one().id
    assert resolve_ticket_by_id(
        db, "acme/widgets", 210, "project.task", strict_model=True
    ) == (default_id, "project.task")


def test_resolve_by_id_strict_model_ignores_helpdesk_analysis(db: Database) -> None:
    with db.session() as s:
        s.add(_analysis(210, odoo_instance_id=3, model_name="project.task",
                        created=datetime(2026, 6, 1, tzinfo=timezone.utc)))
        s.add(_analysis(210, odoo_instance_id=4, model_name="helpdesk.ticket",
                        created=datetime(2026, 7, 1, tzinfo=timezone.utc)))

    assert resolve_ticket_by_id(
        db, "acme/widgets", 210, "project.task", strict_model=True
    ) == (3, "project.task")


def test_resolve_by_id_strict_model_without_default_is_none(db: Database) -> None:
    # Only a helpdesk row exists and there is no default instance: a strict
    # lookup reports unknown rather than borrowing the helpdesk ticket.
    with db.session() as s:
        s.add(_issue_run(210, "acme/widgets", [{"number": 1}]))

    assert resolve_ticket_by_id(
        db, "acme/widgets", 210, "project.task", strict_model=True
    ) is None
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `cd worker && .venv/bin/python -m pytest tests/test_ticket_links.py -q -k strict_model`
Expected: 3 failed — `TypeError: resolve_ticket_by_id() got an unexpected keyword argument 'strict_model'`.

- [x] **Step 3: Implement the filter**

In `reva/ticket_links.py`, replace the signature of `resolve_ticket_by_id`:

```python
def resolve_ticket_by_id(
    db: Database,
    repo_full_name: str,
    ticket_id: int,
    default_model: str = _PROJECT_MODEL,
    *,
    strict_model: bool = False,
) -> tuple[int, str] | None:
```

Extend the end of its docstring (after "…caller records the ops event)."):

```python
    `strict_model` (an explicit `P` reference) turns `default_model` from a guess
    into a filter: only DB rows of that model may match. Task and helpdesk ids
    are separate sequences, so without it `P210` would land on helpdesk ticket
    210 whenever that is the only 210 REVA has seen."""
```

Directly after `repo = repo_full_name.lower()` add:

```python
    run_model = [TicketIssueRun.model_name == default_model] if strict_model else []
    analysis_model = [TicketAnalysis.model_name == default_model] if strict_model else []
```

Add the filters to the two existing `.where(...)` calls:

```python
            .where(
                TicketIssueRun.repo_full_name == repo,
                TicketIssueRun.ticket_id == ticket_id,
                TicketIssueRun.odoo_instance_id.is_not(None),
                *run_model,
            )
```

```python
                .where(
                    TicketAnalysis.ticket_id == ticket_id,
                    TicketAnalysis.odoo_instance_id.is_not(None),
                    *analysis_model,
                )
```

Nothing else in the function changes; the default-instance rung still returns `(default_id, default_model)`.

- [x] **Step 4: Run the tests to verify they pass**

Run: `cd worker && .venv/bin/python -m pytest tests/test_ticket_links.py -q`
Expected: all passed, including every pre-existing `test_resolve_by_id_*` test (they never pass `strict_model`).

---

### Task 2: `extract_ticket_id` accepts `P` and reports `strict`; the runner passes it through

**Files:**
- Modify: `reva/ticket_links.py` (three regexes, `extract_ticket_id`)
- Modify: `worker/worker/board_status_runner.py:118-123`
- Test: `worker/tests/test_ticket_links.py`, `worker/tests/test_board_status_runner.py`

**Interfaces:**
- Consumes: `resolve_ticket_by_id(..., *, strict_model: bool = False)` from Task 1.
- Produces: `extract_ticket_id(head_branch: str | None, pr_title: str | None) -> tuple[int, str, bool] | None` — `(ticket_id, model_name, strict)`. `strict` is `True` only for an explicit `P`/`p` prefix. `model_name` is `"helpdesk.ticket"` for an `H`/`h` prefix, otherwise `"project.task"`.

- [x] **Step 1: Update the existing extractor expectations to the three-value result**

In `worker/tests/test_ticket_links.py`, inside the `extract_ticket_id` section only (between the `# --- extract_ticket_id (spec 2026-07-20)` and `# --- resolve_ticket_by_id (spec 2026-07-20)` headers), every expected tuple gains `False`:

- `(210, "project.task")` → `(210, "project.task", False)` (also `99`, `5`, `7`; including the multi-line one in `test_extract_from_title_tag_form`)
- `(1213, "helpdesk.ticket")` → `(1213, "helpdesk.ticket", False)` (four `H` tests)

Do **not** touch tuples in the `resolve_ticket_by_id` section — those are `(instance_id, model_name)` and stay two values.

- [x] **Step 2: Write the failing `P` tests**

Insert after `test_extract_bare_h_without_digits_is_not_a_ticket`:

```python
# --- P-prefix → project.task, strict (Odoo's `P7624` display ref) ----------------
# Third element: only an explicit `P` is strict — a bare number and `H` stay hints.


def test_extract_p_prefixed_branch_is_project_task() -> None:
    assert extract_ticket_id("feat/P7624", None) == (7624, "project.task", True)


def test_extract_p_prefix_is_case_insensitive() -> None:
    assert extract_ticket_id("feat/p7624", None) == (7624, "project.task", True)


def test_extract_p_prefixed_title_tag_is_project_task() -> None:
    assert extract_ticket_id("stage", "[CONF] P7624 promote bootstrap to production") == (
        7624,
        "project.task",
        True,
    )


def test_extract_p_prefixed_title_token_is_project_task() -> None:
    assert extract_ticket_id(None, "backport of conf/P7624 to 17.0") == (7624, "project.task", True)


def test_extract_bare_p_without_digits_is_not_a_ticket() -> None:
    assert extract_ticket_id("feat/portal", None) is None
```

In `worker/tests/test_board_status_runner.py`, insert before `test_fallback_unknown_ticket_no_default_records_ops_event`. `_seed_board_issue(db, ticket_id=97)` seeds a `helpdesk.ticket` issue run for repo `acme/widgets`; `_ctx`, `_params` and the `odoo` fixture already exist in the file.

```python
def test_fallback_p_prefixed_ticket_never_lands_on_helpdesk(db, odoo):
    # Helpdesk ticket 97 is known for this repo; `P97` is project.task 97 — a
    # different record in a separate id sequence.
    _seed_board_issue(db, ticket_id=97)
    with db.session() as s:
        s.add(OdooInstance(name="prod", key_hash="h1", key_prefix="rk_1",
                           is_default=True))
    _ctx(db, pr_body="plain refactor", head_ref="feat/P97")
    out = run_board_status_update(_params("pr_active"))
    assert out == {"status": "ticket_signal_only"}
    assert odoo.calls[0]["ticket_id"] == 97
    assert odoo.calls[0]["model_name"] == "project.task"
```

- [x] **Step 3: Run the tests to verify they fail**

Run: `cd worker && .venv/bin/python -m pytest tests/test_ticket_links.py tests/test_board_status_runner.py -q`
Expected: the updated extractor tests fail on `(210, 'project.task') != (210, 'project.task', False)`, the four `P` tests fail with `None`, and the runner test fails. `test_extract_bare_p_without_digits_is_not_a_ticket` is a guard and passes before and after.

- [x] **Step 4: Implement**

In `reva/ticket_links.py`, the three regexes replace `(h)?` with `([hp])?` (group 1 = prefix letter, group 2 stays the digits), and the comment above them names the rule:

```python
# An optional leading `h` marks a helpdesk ticket (Odoo's `H1213` display-ref
# convention); a bare number or a leading `p` (`P7624`) is a project task. An
# explicit `p` is additionally STRICT: it may only ever resolve to a
# project.task (see resolve_ticket_by_id). The type prefix (bug/feat/cr/…) is a
# work-item type, not an Odoo model — never mapped.
_TICKET_BRANCH_RE = re.compile(
    r"^(?:bug|feat|cr|conf|dev|mig|sup|doc)/([hp])?(\d{1,9})$", re.IGNORECASE
)
```

```python
_TICKET_TITLE_TAG_RE = re.compile(
    r"\[(?:bug|feat|cr|conf|dev|mig|sup|doc)\]\s*([hp])?(\d{1,9})(?!\.\d)\b", re.IGNORECASE
)
_TICKET_TITLE_TOKEN_RE = re.compile(
    r"\b(?:bug|feat|cr|conf|dev|mig|sup|doc)/([hp])?(\d{1,9})(?!\.\d)\b", re.IGNORECASE
)
```

`extract_ticket_id` — new signature, docstring and both return sites:

```python
def extract_ticket_id(
    head_branch: str | None, pr_title: str | None
) -> tuple[int, str, bool] | None:
    """(ticket_id, model_name, strict) from the PR itself, for PRs with no linked REVA
    issue: the head branch (`cr/2010`, the convention ticket_issue_runner writes
    into issue bodies) first, then the PR title (`[CR] 2010 - …` tag form, then a
    `cr/2010` token). A bare or `P`-prefixed number (`P7624`) is a `project.task`;
    an `H`-prefixed id (`H1213`) is a `helpdesk.ticket`. `strict` is True only for an
    explicit `P`: the author named the model, so the lookup must not land on a
    helpdesk ticket with the same number. None = no recognisable reference — normal
    lifecycle. Ids are bounded to 9 digits and 0 is rejected (never a real id)."""
    match = _TICKET_BRANCH_RE.match((head_branch or "").strip())
    if match:
        value = int(match.group(2))
        if value != 0:
            prefix = (match.group(1) or "").lower()
            return value, _HELPDESK_MODEL if prefix == "h" else _PROJECT_MODEL, prefix == "p"
    title = pr_title or ""
    match = _TICKET_TITLE_TAG_RE.search(title) or _TICKET_TITLE_TOKEN_RE.search(title)
    if match:
        value = int(match.group(2))
        if value != 0:
            prefix = (match.group(1) or "").lower()
            return value, _HELPDESK_MODEL if prefix == "h" else _PROJECT_MODEL, prefix == "p"
    return None
```

In `worker/worker/board_status_runner.py`, inside `run_board_status_update`:

```python
            ticket_id, model_hint, strict_model = extracted
            resolved = resolve_ticket_by_id(
                ctx.db, repo, ticket_id, model_hint, strict_model=strict_model
            )
```

- [x] **Step 5: Run the tests and the linter**

Run: `cd worker && .venv/bin/python -m pytest tests/test_ticket_links.py tests/test_board_status_runner.py -q`
Expected: 94 passed.

Run: `cd worker && .venv/bin/python -m pytest tests/ -q`
Expected: 1727 passed, 15 skipped (count as of 2026-09-18, includes the other in-progress work's tests).

Run (repo root): `worker/.venv/bin/python -m ruff check reva/ticket_links.py worker/worker/board_status_runner.py worker/tests/test_ticket_links.py worker/tests/test_board_status_runner.py`
Expected: `All checks passed!`

---

### Task 3: Ship — own commit, archive, deploy the worker, verify

Every step here is outward-facing or history-writing. **Stop and get Joseph's explicit go before Step 3 (commit), Step 5 (push) and Step 6 (deploy).**

**Files:**
- Move: `docs/superpowers/specs/2026-09-18-ticket-id-p-prefix-design.md` → `docs/superpowers/specs/archive/`
- Move: `docs/superpowers/plans/2026-09-18-ticket-id-p-prefix.md` → `docs/superpowers/plans/archive/`

**Interfaces:**
- Consumes: the working-tree state produced by Tasks 1 and 2.
- Produces: one commit on `main` containing exactly six paths; the worker running that commit in production.

- [x] **Step 1: Re-run the gate on the current tree**

The other work keeps moving in the same tree, so re-verify right before committing:

```bash
cd worker && .venv/bin/python -m pytest tests/test_ticket_links.py tests/test_board_status_runner.py -q
```

Expected: all passed.

- [x] **Step 2: Archive spec and plan (repo rule: archive as part of the shipping change)**

Both files are untracked, so a plain move is enough:

```bash
mv docs/superpowers/specs/2026-09-18-ticket-id-p-prefix-design.md docs/superpowers/specs/archive/
mv docs/superpowers/plans/2026-09-18-ticket-id-p-prefix.md docs/superpowers/plans/archive/
```

Then, in the archived spec:
- `Status` line → `implemented 2026-09-18, shipped <commit date>` and drop the "Move this file to `archive/`…" sentence.
- `Context` line: the parent spec is now a sibling — change `` `archive/2026-07-20-pr-review-ticket-signal-design.md` `` to `` `2026-07-20-pr-review-ticket-signal-design.md` ``.

In the archived plan: change the `**Spec:**` path to `docs/superpowers/specs/archive/2026-09-18-ticket-id-p-prefix-design.md` and tick the Task 3 boxes as they complete.

- [x] **Step 3: Commit exactly six paths — nothing else (needs Joseph's go)**

The index already holds the other work's staged rename (`048_…` → `050_…`). `git commit --only <paths>` commits the named paths and leaves that staged rename untouched and staged.

```bash
git add docs/superpowers/specs/archive/2026-09-18-ticket-id-p-prefix-design.md \
        docs/superpowers/plans/archive/2026-09-18-ticket-id-p-prefix.md
git commit --only \
  reva/ticket_links.py \
  worker/worker/board_status_runner.py \
  worker/tests/test_ticket_links.py \
  worker/tests/test_board_status_runner.py \
  docs/superpowers/specs/archive/2026-09-18-ticket-id-p-prefix-design.md \
  docs/superpowers/plans/archive/2026-09-18-ticket-id-p-prefix.md \
  -m "feat(ticket-signal): 0000 - accept P-prefixed task ids, strict to project.task"
```

- [x] **Step 4: Verify the commit and the untouched other work**

```bash
git show --stat --format='%h %s' HEAD
git status --short
```

Expected: `git show` lists exactly the six paths above. `git status` still shows the other work exactly as before — `R  db/migrations/048_… -> 050_…` (still staged), ` M` on `reva/db/models.py`, `reva/finding_verifier.py`, `worker/tests/test_strict_tools.py`, the support-answer-images plan, plus the untracked `docs/handoff-2026-09-01-release-notes.md` and `uv.lock`. If `git show` lists anything else: `git reset --soft HEAD~1` and redo Step 3.

- [ ] **Step 5: Push (needs Joseph's explicit go for this push)**

```bash
git push origin main
```

- [ ] **Step 6: Deploy on the production host (needs Joseph's go)**

`scripts/deploy.sh` pulls `origin main` and rebuilds the stack; only the worker's behaviour changes.

```bash
make deploy
make logs-worker   # confirm the worker came back up
```

- [ ] **Step 7: Verify in production with the next `P<id>` PR**

Nothing is sent retroactively — `hackmair-platzhirsch` PR #1 and #2 are merged and the runner skips merged/closed PRs. On the next **open** PR whose branch or title carries `P<id>`:

1. After REVA's review finishes, the worker log shows `odoo_issue_work_status_ok` for that PR and **not** `ticket_signal_no_ticket_ref`.
2. In Odoo, task `<id>` shows the chatter line "REVA reviewed PR #… (…)" and the **Reviewed** badge.
3. One review time entry exists on the task — only if a REVA timesheet employee is configured on the project or in the system parameters (unset = booking off, silently).
4. No helpdesk ticket with the same number received a chatter line.

If the log shows `ticket_signal_no_default_instance` instead (also an `odoo_callback` / `no_default_instance` ops event, TUI Failures tab): the task was never analysed by REVA and no active `is_default` Odoo instance exists. That is a data fix, not a code issue — `odoo_instances.is_default` has no API or TUI surface, so it is set in SQL (`make psql`), at most one row.
