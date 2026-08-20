# Issue Reassignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Odoo tell REVA that a GitHub issue now belongs to a different Odoo record, so every later callback for that issue addresses the new record.

**Architecture:** A small override table, `ticket_issue_reassignments`, keyed on `(odoo_instance_id, repo_full_name, number)`. The issue plans in `ticket_issue_runs.issues` are never rewritten. Five existing functions that answer "which Odoo record owns this issue?" consult the override through two shared helpers. A new instance-gated `POST /api/v1/reassign-issue` writes the row synchronously.

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy 2.x ORM, Pydantic v2, pytest, plain-SQL migrations applied at startup.

**Spec:** `docs/superpowers/specs/2026-08-20-issue-reassignment-design.md`

## Global Constraints

- **The endpoint must never return 404.** Odoo's wizard reads `404`/`501` as "REVA has not shipped this yet" and commits the move with a warning note that would be false. Unknown issue → `200`. Malformed body or unparseable `repo` → `422`.
- **`from` is advisory.** It is logged and recorded in the ops event; a mismatch is never an error. A repeated call and a call whose `from` is stale but whose `to` is right both succeed.
- Migration files are **idempotent** (`CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`) and use `id BIGSERIAL PRIMARY KEY` — matching the existing files, **not** `GENERATED … IDENTITY`.
- Every new table needs a matching ORM model in `reva/db/models.py`. Tests build tables from the models via `create_all`, **not** from the SQL, so a missing model makes the table invisible to every test.
- `repo_full_name` is always stored and compared **lowercased** `owner/repo`, matching `ticket_issue_runs.repo_full_name`.
- Degradations must be visible: anything caught and degraded around both logs **and** calls `writers.record_ops_event(...)`. A silent `except: log-and-continue` is a review-blocking defect.
- Definition of done for the whole plan: `make test` green (worker, api, scheduler — a change to shared `reva/` affects all three), `ruff check reva worker/worker api/app scheduler/scheduler` clean. Ruff lives in `worker/.venv/bin/ruff`.
- Per-service test commands: `worker/.venv/bin/python -m pytest worker/tests/...`, `api/.venv/bin/python -m pytest api/tests/...`.

---

### Task 1: The table, the model, and the two lookup helpers

Nothing resolves overrides yet — this task only makes it possible to store one and read it back. Every later task consumes these two helpers.

**Files:**
- Create: `db/migrations/047_ticket_issue_reassignments.sql`
- Modify: `reva/db/models.py` (add `TicketIssueReassignment` after the `TicketIssueRun` class, before the `# ------ change_notes` divider at line ~580)
- Modify: `reva/db/writers.py` (add the three functions next to `update_ticket_issue_state`, ~line 2183)
- Test: `worker/tests/test_issue_reassignment_writers.py` (new file)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `writers.record_issue_reassignment(db, *, odoo_instance_id: int, repo_full_name: str, number: int, ticket_id: int, model_name: str) -> None` — upserts one row. Lowercases `repo_full_name`.
  - `writers.clear_issue_reassignment(db, *, odoo_instance_id: int, repo_full_name: str, number: int) -> None` — deletes the row for that key if present; a no-op otherwise.
  - `writers.issue_owner_overrides(db, odoo_instance_id: int | None, repo_full_name: str, numbers: list[int]) -> dict[int, tuple[int, str]]` — `{number: (ticket_id, model_name)}` for the numbers that have one. Empty dict when `odoo_instance_id` is `None` or `numbers` is empty.
  - `writers.issues_moved_onto(db, odoo_instance_id: int | None, ticket_id: int, model_name: str) -> list[tuple[str, int]]` — `[(repo_full_name, number)]` moved onto this record. Empty list when `odoo_instance_id` is `None`.

- [ ] **Step 1: Write the migration**

Create `db/migrations/047_ticket_issue_reassignments.sql`:

```sql
-- Operator correction of which Odoo record owns a REVA-created GitHub issue
-- (spec 2026-08-20-issue-reassignment-design). REVA's ticket<->issue mapping is
-- otherwise implicit in ticket_issue_runs.issues, and a create-issues run fired
-- from the wrong record leaves no way to fix it: Odoo's handler replaces the
-- record's whole issue set from REVA's union, so moving the reva.github.issue
-- row alone is undone by the next callback.
--
-- One row per moved issue. Absence is the normal case; the runs stay untouched,
-- so deleting a row undoes the move.
-- Mirrors reva/db/models.py::TicketIssueReassignment.
CREATE TABLE IF NOT EXISTS ticket_issue_reassignments (
    id BIGSERIAL PRIMARY KEY,
    odoo_instance_id BIGINT NOT NULL REFERENCES odoo_instances(id),
    repo_full_name TEXT NOT NULL,
    number INTEGER NOT NULL,
    ticket_id BIGINT NOT NULL,
    model_name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The per-issue direction: "who owns issue N of this repo?"
CREATE UNIQUE INDEX IF NOT EXISTS uq_ticket_issue_reassignments
    ON ticket_issue_reassignments (odoo_instance_id, repo_full_name, number);

-- The per-record direction: "what moved ONTO this record?" — needed because a
-- target record may have no ticket_issue_runs row of its own at all.
CREATE INDEX IF NOT EXISTS idx_ticket_issue_reassignments_record
    ON ticket_issue_reassignments (odoo_instance_id, ticket_id, model_name);
```

- [ ] **Step 2: Add the ORM model**

In `reva/db/models.py`, immediately after the `TicketIssueRun` class ends (just before the `# ------------------------------------------------------------- change_notes` divider):

```python
class TicketIssueReassignment(Base):
    """Mirrors db/migrations/047_ticket_issue_reassignments.sql — an operator
    correction of which Odoo record owns a REVA-created issue (spec
    2026-08-20).

    Absence is the normal case: ownership is otherwise implicit in
    `ticket_issue_runs.issues`, and those rows are never rewritten by a move, so
    deleting one of these rows undoes it. `odoo_instance_id` is NOT NULL even
    though the runs table allows NULL for legacy rows — the endpoint that writes
    this is instance-key gated, so every row it can receive has one.
    """

    __tablename__ = "ticket_issue_reassignments"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    odoo_instance_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("odoo_instances.id"), nullable=False
    )
    # Lowercased "owner/repo", matching TicketIssueRun.repo_full_name.
    repo_full_name: Mapped[str] = mapped_column(Text, nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    ticket_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    model_name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index(
            "uq_ticket_issue_reassignments",
            "odoo_instance_id",
            "repo_full_name",
            "number",
            unique=True,
        ),
        Index(
            "idx_ticket_issue_reassignments_record",
            "odoo_instance_id",
            "ticket_id",
            "model_name",
        ),
    )
```

- [ ] **Step 3: Write the failing tests**

Create `worker/tests/test_issue_reassignment_writers.py`:

```python
"""Writer-level tests for the issue-ownership override table (real SQLite)."""
from __future__ import annotations

import pytest

from reva.db import Base, Database, create_engine_from_url, writers


@pytest.fixture
def db() -> Database:
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def _instance(db: Database) -> int:
    return writers.create_odoo_instance(
        db, name="acme", callback_url="", callback_api_key_enc="enc",
    )


def test_records_and_reads_back_an_override(db):
    iid = _instance(db)
    writers.record_issue_reassignment(
        db, odoo_instance_id=iid, repo_full_name="acme/widgets", number=42,
        ticket_id=5678, model_name="helpdesk.ticket",
    )
    assert writers.issue_owner_overrides(db, iid, "acme/widgets", [42]) == {
        42: (5678, "helpdesk.ticket")
    }


def test_repo_name_is_matched_case_insensitively(db):
    # ticket_issue_runs.repo_full_name is stored lowercased; a caller passing
    # GitHub's original casing must still match.
    iid = _instance(db)
    writers.record_issue_reassignment(
        db, odoo_instance_id=iid, repo_full_name="Acme/Widgets", number=42,
        ticket_id=5678, model_name="helpdesk.ticket",
    )
    assert writers.issue_owner_overrides(db, iid, "acme/widgets", [42]) == {
        42: (5678, "helpdesk.ticket")
    }


def test_recording_twice_updates_rather_than_duplicating(db):
    iid = _instance(db)
    for ticket_id in (5678, 9999):
        writers.record_issue_reassignment(
            db, odoo_instance_id=iid, repo_full_name="acme/widgets", number=42,
            ticket_id=ticket_id, model_name="project.task",
        )
    assert writers.issue_owner_overrides(db, iid, "acme/widgets", [42]) == {
        42: (9999, "project.task")
    }


def test_clear_removes_the_override(db):
    iid = _instance(db)
    writers.record_issue_reassignment(
        db, odoo_instance_id=iid, repo_full_name="acme/widgets", number=42,
        ticket_id=5678, model_name="helpdesk.ticket",
    )
    writers.clear_issue_reassignment(
        db, odoo_instance_id=iid, repo_full_name="acme/widgets", number=42,
    )
    assert writers.issue_owner_overrides(db, iid, "acme/widgets", [42]) == {}


def test_clear_is_a_noop_when_there_is_nothing_to_clear(db):
    iid = _instance(db)
    writers.clear_issue_reassignment(
        db, odoo_instance_id=iid, repo_full_name="acme/widgets", number=42,
    )  # no raise


def test_overrides_are_scoped_to_the_instance(db):
    one, two = _instance(db), writers.create_odoo_instance(
        db, name="other", callback_url="", callback_api_key_enc="enc",
    )
    writers.record_issue_reassignment(
        db, odoo_instance_id=one, repo_full_name="acme/widgets", number=42,
        ticket_id=5678, model_name="helpdesk.ticket",
    )
    assert writers.issue_owner_overrides(db, two, "acme/widgets", [42]) == {}


def test_legacy_null_instance_resolves_no_overrides(db):
    # Pre-multi-instance runs carry a NULL odoo_instance_id. They can never
    # have an override (the endpoint is instance-gated), and passing None must
    # not match every row.
    iid = _instance(db)
    writers.record_issue_reassignment(
        db, odoo_instance_id=iid, repo_full_name="acme/widgets", number=42,
        ticket_id=5678, model_name="helpdesk.ticket",
    )
    assert writers.issue_owner_overrides(db, None, "acme/widgets", [42]) == {}
    assert writers.issues_moved_onto(db, None, 5678, "helpdesk.ticket") == []


def test_issues_moved_onto_lists_the_target_side(db):
    iid = _instance(db)
    writers.record_issue_reassignment(
        db, odoo_instance_id=iid, repo_full_name="acme/widgets", number=42,
        ticket_id=5678, model_name="helpdesk.ticket",
    )
    writers.record_issue_reassignment(
        db, odoo_instance_id=iid, repo_full_name="acme/widgets", number=43,
        ticket_id=1234, model_name="project.task",
    )
    assert writers.issues_moved_onto(db, iid, 5678, "helpdesk.ticket") == [
        ("acme/widgets", 42)
    ]
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `worker/.venv/bin/python -m pytest worker/tests/test_issue_reassignment_writers.py -q`
Expected: every test FAILS with `AttributeError: module 'reva.db.writers' has no attribute 'record_issue_reassignment'`. If instead you see a `no such table` error, the ORM model in Step 2 is missing or misnamed.

- [ ] **Step 5: Write the writers**

In `reva/db/writers.py`, directly above `def update_ticket_issue_state(` (~line 2183). Add `TicketIssueReassignment` to the model imports at the top of the file:

```python
# ---------------------------------------------- issue-ownership overrides
# Which Odoo record owns a REVA-created issue is otherwise implicit in
# ticket_issue_runs.issues. These four functions are the ONLY way that implicit
# answer gets corrected — every query that resolves an owner must consult them,
# or a moved issue silently bounces back to the record it was moved off.


def record_issue_reassignment(
    db: Database,
    *,
    odoo_instance_id: int,
    repo_full_name: str,
    number: int,
    ticket_id: int,
    model_name: str,
) -> None:
    """Upsert the override for one issue. Last call wins — an issue moved twice
    ends up owned by the last target, and `from` never enters the key."""
    repo = repo_full_name.lower()
    with db.session() as s:
        row = s.execute(
            select(TicketIssueReassignment).where(
                TicketIssueReassignment.odoo_instance_id == odoo_instance_id,
                TicketIssueReassignment.repo_full_name == repo,
                TicketIssueReassignment.number == number,
            )
        ).scalar_one_or_none()
        if row is None:
            s.add(TicketIssueReassignment(
                odoo_instance_id=odoo_instance_id,
                repo_full_name=repo,
                number=number,
                ticket_id=ticket_id,
                model_name=model_name,
            ))
            return
        row.ticket_id = ticket_id
        row.model_name = model_name


def clear_issue_reassignment(
    db: Database, *, odoo_instance_id: int, repo_full_name: str, number: int
) -> None:
    """Drop the override, restoring the runs' own answer. Used when an issue is
    moved back to its natural owner — writing an identity override instead
    would leave a row that means nothing and has to be read past forever."""
    repo = repo_full_name.lower()
    with db.session() as s:
        s.query(TicketIssueReassignment).filter_by(
            odoo_instance_id=odoo_instance_id, repo_full_name=repo, number=number
        ).delete()


def issue_owner_overrides(
    db: Database,
    odoo_instance_id: int | None,
    repo_full_name: str,
    numbers: list[int],
) -> dict[int, tuple[int, str]]:
    """{number: (ticket_id, model_name)} for the overridden numbers only.

    A NULL instance is a pre-multi-instance run, which can never carry an
    override (the endpoint that writes them is instance-gated) — it resolves to
    nothing rather than matching every row.
    """
    if odoo_instance_id is None or not numbers:
        return {}
    repo = repo_full_name.lower()
    with db.session() as s:
        rows = s.execute(
            select(
                TicketIssueReassignment.number,
                TicketIssueReassignment.ticket_id,
                TicketIssueReassignment.model_name,
            ).where(
                TicketIssueReassignment.odoo_instance_id == odoo_instance_id,
                TicketIssueReassignment.repo_full_name == repo,
                TicketIssueReassignment.number.in_(numbers),
            )
        ).all()
        return {r.number: (r.ticket_id, r.model_name) for r in rows}


def issues_moved_onto(
    db: Database, odoo_instance_id: int | None, ticket_id: int, model_name: str
) -> list[tuple[str, int]]:
    """[(repo_full_name, number)] moved ONTO this record.

    The direction that cannot be derived from the record's own runs: a target
    may have no ticket_issue_runs row at all, which is exactly the case a naive
    implementation drops.
    """
    if odoo_instance_id is None:
        return []
    with db.session() as s:
        rows = s.execute(
            select(
                TicketIssueReassignment.repo_full_name,
                TicketIssueReassignment.number,
            ).where(
                TicketIssueReassignment.odoo_instance_id == odoo_instance_id,
                TicketIssueReassignment.ticket_id == ticket_id,
                TicketIssueReassignment.model_name == model_name,
            ).order_by(
                TicketIssueReassignment.repo_full_name,
                TicketIssueReassignment.number,
            )
        ).all()
        return [(r.repo_full_name, r.number) for r in rows]
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `worker/.venv/bin/python -m pytest worker/tests/test_issue_reassignment_writers.py -q`
Expected: PASS, 8 tests.

- [ ] **Step 7: Confirm nothing else broke**

Run: `worker/.venv/bin/python -m pytest worker/tests/ -q && api/.venv/bin/python -m pytest api/tests/ -q`
Expected: both suites pass. A new table with no readers cannot change behaviour; a failure here means the ORM model was inserted in the wrong place or shadows an existing name.

- [ ] **Step 8: Commit**

```bash
git add db/migrations/047_ticket_issue_reassignments.sql reva/db/models.py reva/db/writers.py worker/tests/test_issue_reassignment_writers.py
git commit -m "feat(reassign): issue-ownership override table and lookup helpers

Storage only — nothing resolves overrides yet. The runs' issue plans are never
rewritten by a move, so deleting a row undoes it."
```

---

### Task 2: The union honours overrides (both directions)

`get_ticket_issue_union` is the snapshot Odoo receives, and Odoo **replaces** the record's whole issue set from it. Until this task, a move is invisible to every callback.

**Files:**
- Modify: `reva/db/writers.py` (`get_ticket_issue_union`, ~line 2305)
- Test: `worker/tests/test_issue_reassignment_writers.py`

**Interfaces:**
- Consumes: `writers.issue_owner_overrides`, `writers.issues_moved_onto` (Task 1).
- Produces: `get_ticket_issue_union` keeps its exact signature `(db, odoo_instance_id, ticket_id, model_name) -> list[dict]` and item keys (`number`, `title`, `url`, `state`, `plan_date`, `complete_date`, `estimate_hours`). Only which numbers appear changes.

- [ ] **Step 1: Write the failing tests**

Append to `worker/tests/test_issue_reassignment_writers.py`:

```python
def _run_with_issues(db: Database, instance_id: int, ticket_id: int,
                     model_name: str, numbers: list[int]) -> int:
    """A completed create-issues run owning `numbers` on acme/widgets."""
    from reva.types import TicketIssueJobParams

    run_id = writers.record_ticket_issue_run_created(db, TicketIssueJobParams(
        run_id=0, odoo_instance_id=instance_id, ticket_id=ticket_id,
        model_name=model_name, github_url="https://github.com/acme/widgets",
        name="Ticket name", description="d", analysis_html="",
        priority="1", ticket_url="https://odoo.example/web#id=1",
    ))
    writers.update_ticket_issue_progress(db, run_id, [
        {"title": f"Issue {n}", "number": n,
         "url": f"https://github.com/acme/widgets/issues/{n}", "state": "open"}
        for n in numbers
    ])
    return run_id


def test_union_drops_an_issue_moved_away(db):
    iid = _instance(db)
    _run_with_issues(db, iid, 1234, "project.task", [42, 43])
    writers.record_issue_reassignment(
        db, odoo_instance_id=iid, repo_full_name="acme/widgets", number=42,
        ticket_id=5678, model_name="helpdesk.ticket",
    )
    union = writers.get_ticket_issue_union(db, iid, 1234, "project.task")
    assert [i["number"] for i in union] == [43]


def test_union_adds_an_issue_moved_on(db):
    iid = _instance(db)
    _run_with_issues(db, iid, 1234, "project.task", [42, 43])
    writers.record_issue_reassignment(
        db, odoo_instance_id=iid, repo_full_name="acme/widgets", number=42,
        ticket_id=5678, model_name="helpdesk.ticket",
    )
    union = writers.get_ticket_issue_union(db, iid, 5678, "helpdesk.ticket")
    assert [i["number"] for i in union] == [42]
    # The item travels intact — Odoo re-renders its links from this payload.
    assert union[0]["title"] == "Issue 42"
    assert union[0]["url"] == "https://github.com/acme/widgets/issues/42"
    assert union[0]["state"] == "open"


def test_union_for_a_target_with_no_runs_at_all(db):
    """The case a naive implementation drops: the record the issue moved onto
    has never had a create-issues run, so there is nothing to read it off."""
    iid = _instance(db)
    _run_with_issues(db, iid, 1234, "project.task", [42])
    writers.record_issue_reassignment(
        db, odoo_instance_id=iid, repo_full_name="acme/widgets", number=42,
        ticket_id=5678, model_name="helpdesk.ticket",
    )
    union = writers.get_ticket_issue_union(db, iid, 5678, "helpdesk.ticket")
    assert [i["number"] for i in union] == [42]


def test_union_reflects_state_written_after_the_move(db):
    """State sync writes into the SOURCE's run row, because that is where the
    issue plan still lives. The target's union must show it."""
    iid = _instance(db)
    _run_with_issues(db, iid, 1234, "project.task", [42])
    writers.record_issue_reassignment(
        db, odoo_instance_id=iid, repo_full_name="acme/widgets", number=42,
        ticket_id=5678, model_name="helpdesk.ticket",
    )
    writers.update_ticket_issue_state(db, "acme", "widgets", 42, "closed",
                                      "2026-08-20T10:00:00Z")
    union = writers.get_ticket_issue_union(db, iid, 5678, "helpdesk.ticket")
    assert union[0]["state"] == "closed"
    assert union[0]["complete_date"] == "2026-08-20"


def test_union_is_unchanged_when_nothing_was_moved(db):
    iid = _instance(db)
    _run_with_issues(db, iid, 1234, "project.task", [42, 43])
    union = writers.get_ticket_issue_union(db, iid, 1234, "project.task")
    assert [i["number"] for i in union] == [42, 43]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `worker/.venv/bin/python -m pytest worker/tests/test_issue_reassignment_writers.py -q -k union`
Expected: `test_union_drops_an_issue_moved_away` fails with `[42, 43] != [43]`; the two "moved on" tests fail with `[] != [42]`; `test_union_is_unchanged_when_nothing_was_moved` PASSES already (it is the regression guard, not a new behaviour).

- [ ] **Step 3: Implement both directions**

Replace the body of `get_ticket_issue_union` in `reva/db/writers.py`. Keep the existing docstring's first paragraph and add the override paragraph:

```python
def get_ticket_issue_union(
    db: Database, odoo_instance_id: int | None, ticket_id: int, model_name: str
) -> list[dict]:
    """Union of created issues across ALL runs for this record, deduped by
    issue number (newest run wins title/url/state), sorted by number.

    The Odoo issues-created handler replaces the record's whole issue list
    with the payload — sending only the completing run's issues would wipe
    what earlier requests created (wizard + planner requests accumulate).
    Parents are excluded (parent_issue column, never in `issues`).

    Reassignment (spec 2026-08-20) moves numbers between records without
    touching any run: numbers moved AWAY are dropped here, and numbers moved
    ONTO this record are pulled in from whichever run still holds their plan.
    That second direction is why this cannot be a pure per-record query — the
    target may have no run of its own at all.
    """
    from sqlalchemy.orm import load_only

    with db.session() as s:
        rows = s.execute(
            select(TicketIssueRun)
            .where(
                TicketIssueRun.ticket_id == ticket_id,
                TicketIssueRun.model_name == model_name,
                _instance_filter(odoo_instance_id),
                TicketIssueRun.issues.is_not(None),
            )
            .options(load_only(TicketIssueRun.issues, TicketIssueRun.created_at))
            .order_by(TicketIssueRun.created_at.desc(), TicketIssueRun.id.desc())
        ).scalars().all()
        seen: dict[int, dict] = {}
        for row in rows:  # newest first — first occurrence of a number wins
            for item in row.issues or []:
                n = item.get("number")
                if n is None or n in seen:
                    continue
                seen[n] = _union_item(item)

    # Drop what moved away. Computed after the loop so the repo key comes from
    # the runs themselves rather than being threaded through the query.
    if seen:
        moved_away = _overrides_away(db, odoo_instance_id, ticket_id, model_name,
                                    list(seen))
        for number in moved_away:
            seen.pop(number, None)

    # Pull in what moved on, from whichever run still holds the plan.
    for repo_full_name, number in issues_moved_onto(
        db, odoo_instance_id, ticket_id, model_name
    ):
        if number in seen:
            continue
        item = _issue_item_from_runs(db, repo_full_name, number)
        if item is not None:
            seen[number] = item

    return sorted(seen.values(), key=lambda i: i["number"])
```

Add the three helpers directly above it:

```python
def _union_item(item: dict) -> dict:
    """Project a stored plan item onto the documented union shape."""
    return {
        "number": item.get("number"),
        "title": item.get("title", ""),
        "url": item.get("url"),
        "state": item.get("state") or "open",
        "plan_date": item.get("plan_date"),
        "complete_date": item.get("complete_date"),
        "estimate_hours": item.get("estimate_hours"),
    }


def _overrides_away(
    db: Database,
    odoo_instance_id: int | None,
    ticket_id: int,
    model_name: str,
    numbers: list[int],
) -> set[int]:
    """Of `numbers` (all carried by this record's runs), those an override has
    moved to a DIFFERENT record. An override pointing back at this record is
    not a move and must not drop the issue."""
    if odoo_instance_id is None:
        return set()
    repos = _repos_for_record(db, odoo_instance_id, ticket_id, model_name)
    moved: set[int] = set()
    for repo in repos:
        for number, owner in issue_owner_overrides(
            db, odoo_instance_id, repo, numbers
        ).items():
            if owner != (ticket_id, model_name):
                moved.add(number)
    return moved


def _repos_for_record(
    db: Database, odoo_instance_id: int | None, ticket_id: int, model_name: str
) -> list[str]:
    """Distinct lowercased repos this record has runs for."""
    with db.session() as s:
        rows = s.execute(
            select(TicketIssueRun.repo_full_name)
            .where(
                TicketIssueRun.ticket_id == ticket_id,
                TicketIssueRun.model_name == model_name,
                _instance_filter(odoo_instance_id),
                TicketIssueRun.repo_full_name.is_not(None),
            )
            .distinct()
        ).all()
        return [r.repo_full_name for r in rows]


def _issue_item_from_runs(
    db: Database, repo_full_name: str, number: int
) -> dict | None:
    """The newest run's copy of issue `number` on `repo_full_name`, in union
    shape. None when no run carries it — a reassignment may name an issue REVA
    does not know yet, and that must not fabricate an entry."""
    from sqlalchemy.orm import load_only

    with db.session() as s:
        rows = s.execute(
            select(TicketIssueRun)
            .where(
                TicketIssueRun.repo_full_name == repo_full_name.lower(),
                TicketIssueRun.issues.is_not(None),
            )
            .options(load_only(TicketIssueRun.issues, TicketIssueRun.created_at))
            .order_by(TicketIssueRun.created_at.desc(), TicketIssueRun.id.desc())
        ).scalars().all()
        for row in rows:
            for item in row.issues or []:
                if item.get("number") == number:
                    return _union_item(item)
    return None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `worker/.venv/bin/python -m pytest worker/tests/test_issue_reassignment_writers.py -q`
Expected: PASS, 13 tests.

- [ ] **Step 5: Run the suites that consume the union**

Run: `worker/.venv/bin/python -m pytest worker/tests/ -q && api/.venv/bin/python -m pytest api/tests/ -q`
Expected: both pass. `get_ticket_issue_union` feeds the issues-created callback, the issue-state snapshot, work-status intersection, the `/update-issue-estimate` 404 gate, `list_ready_tickets` and the ticket journey, so a regression shows up here.

- [ ] **Step 6: Commit**

```bash
git add reva/db/writers.py worker/tests/test_issue_reassignment_writers.py
git commit -m "feat(reassign): union honours issue-ownership overrides both ways

Drops numbers moved away, pulls in numbers moved on from whichever run still
holds the plan — the target may have no run of its own."
```

---

### Task 3: Callbacks address the new owner

Three remaining resolution sites. Grouped because they share one behaviour ("who do we call about issue N?") and a reviewer would accept or reject them together.

**Files:**
- Modify: `reva/db/writers.py` (`update_ticket_issue_state` ~line 2183, `update_ticket_issue_estimate` ~line 2245, `list_ready_tickets` ~line 2392)
- Modify: `reva/ticket_links.py` (`resolve_pr_tickets`, line 36)
- Test: `worker/tests/test_issue_reassignment_writers.py`
- Test: `worker/tests/test_ticket_links.py` (existing file — check it exists with `ls worker/tests/test_ticket_links.py`; if it does not, create it with the same imports as `test_issue_reassignment_writers.py`)

**Interfaces:**
- Consumes: `writers.issue_owner_overrides`, `writers.issues_moved_onto` (Task 1); `get_ticket_issue_union` (Task 2).
- Produces: all four functions keep their existing signatures and return shapes. `update_ticket_issue_state` still returns `[{"ticket_id", "model_name", "odoo_instance_id", "issues"}]`; `resolve_pr_tickets` still returns `[TicketRef(odoo_instance_id, ticket_id, model_name, run_id)]`.

- [ ] **Step 1: Write the failing tests**

Append to `worker/tests/test_issue_reassignment_writers.py`:

```python
def test_state_sync_notifies_the_target_not_the_source(db):
    """The whole point: the issue closes, and the record that hears about it is
    the one it was moved to."""
    iid = _instance(db)
    _run_with_issues(db, iid, 1234, "project.task", [42])
    writers.record_issue_reassignment(
        db, odoo_instance_id=iid, repo_full_name="acme/widgets", number=42,
        ticket_id=5678, model_name="helpdesk.ticket",
    )
    affected = writers.update_ticket_issue_state(
        db, "acme", "widgets", 42, "closed", "2026-08-20T10:00:00Z"
    )
    assert [(a["ticket_id"], a["model_name"]) for a in affected] == [
        (5678, "helpdesk.ticket")
    ]


def test_state_sync_still_writes_state_into_the_source_run(db):
    """State is a fact about the issue, and the plan still lives on the source's
    run — the write must not follow the notification."""
    iid = _instance(db)
    run_id = _run_with_issues(db, iid, 1234, "project.task", [42])
    writers.record_issue_reassignment(
        db, odoo_instance_id=iid, repo_full_name="acme/widgets", number=42,
        ticket_id=5678, model_name="helpdesk.ticket",
    )
    writers.update_ticket_issue_state(db, "acme", "widgets", 42, "closed", None)
    stored = writers.get_ticket_issue_run(db, run_id)["issues"]
    assert stored[0]["state"] == "closed"


def test_state_sync_unaffected_without_an_override(db):
    iid = _instance(db)
    _run_with_issues(db, iid, 1234, "project.task", [42])
    affected = writers.update_ticket_issue_state(db, "acme", "widgets", 42, "closed", None)
    assert [(a["ticket_id"], a["model_name"]) for a in affected] == [
        (1234, "project.task")
    ]


def test_estimate_addressed_at_the_target_reaches_the_source_run(db):
    """Odoo sends the estimate from the record the issue now sits on, but the
    run holding the issue belongs to the record it came from."""
    iid = _instance(db)
    run_id = _run_with_issues(db, iid, 1234, "project.task", [42])
    writers.record_issue_reassignment(
        db, odoo_instance_id=iid, repo_full_name="acme/widgets", number=42,
        ticket_id=5678, model_name="helpdesk.ticket",
    )
    target = writers.update_ticket_issue_estimate(
        db, iid, 5678, "helpdesk.ticket", 42, 3.5
    )
    assert target is not None
    stored = writers.get_ticket_issue_run(db, run_id)["issues"]
    assert stored[0]["estimate_hours"] == 3.5


def test_a_record_whose_only_issues_arrived_by_move_can_be_ready(db):
    iid = _instance(db)
    _run_with_issues(db, iid, 1234, "project.task", [42])
    writers.record_issue_reassignment(
        db, odoo_instance_id=iid, repo_full_name="acme/widgets", number=42,
        ticket_id=5678, model_name="helpdesk.ticket",
    )
    writers.update_ticket_issue_state(db, "acme", "widgets", 42, "closed", None)

    ready = writers.list_ready_tickets(db, limit=10)
    keys = {(t["ticket_id"], t["model_name"]) for t in ready}
    assert (5678, "helpdesk.ticket") in keys
    # The source has no issues left at all, so it is not "ready" — it is empty.
    assert (1234, "project.task") not in keys
```

Append to `worker/tests/test_ticket_links.py`:

```python
def test_resolve_pr_tickets_follows_a_reassignment():
    """A PR closing a moved issue must resolve to the record that owns it now,
    or the change-summary and work-status callbacks land on the wrong ticket."""
    from reva.db import Base, Database, create_engine_from_url, writers
    from reva.ticket_links import resolve_pr_tickets
    from reva.types import TicketIssueJobParams

    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Database(engine)
    iid = writers.create_odoo_instance(
        db, name="acme", callback_url="", callback_api_key_enc="enc",
    )
    run_id = writers.record_ticket_issue_run_created(db, TicketIssueJobParams(
        run_id=0, odoo_instance_id=iid, ticket_id=1234, model_name="project.task",
        github_url="https://github.com/acme/widgets", name="Ticket name",
        description="d", analysis_html="", priority="1",
        ticket_url="https://odoo.example/web#id=1",
    ))
    writers.update_ticket_issue_progress(db, run_id, [
        {"title": "Issue 42", "number": 42,
         "url": "https://github.com/acme/widgets/issues/42", "state": "open"},
    ])
    writers.record_issue_reassignment(
        db, odoo_instance_id=iid, repo_full_name="acme/widgets", number=42,
        ticket_id=5678, model_name="helpdesk.ticket",
    )

    refs = resolve_pr_tickets(db, "acme/widgets", [42])
    assert [(r.ticket_id, r.model_name) for r in refs] == [(5678, "helpdesk.ticket")]
    # run_id still points at the run holding the plan — change_note_runner reads
    # the ticket name off it.
    assert refs[0].run_id == run_id
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `worker/.venv/bin/python -m pytest worker/tests/test_issue_reassignment_writers.py worker/tests/test_ticket_links.py -q`
Expected: the five new writer tests and the ticket-links test FAIL, each reporting the **source** record where the target was expected. `test_state_sync_still_writes_state_into_the_source_run` and `test_state_sync_unaffected_without_an_override` PASS already — they are regression guards.

- [ ] **Step 3: Redirect the affected set in `update_ticket_issue_state`**

In `reva/db/writers.py`, replace the closing `return list(affected.values())` of `update_ticket_issue_state` with the block below. The local `target` (`f"{owner.lower()}/{repo.lower()}"`) already exists earlier in the function; the override result is bound to `override_owner` so it does not shadow the function's `owner` parameter.

```python
    # Reassignment (spec 2026-08-20): the per-issue state writes above are
    # unchanged — state is a fact about the issue, and the plan lives on
    # whichever run created it. Only WHO WE TELL changes. The source is
    # deliberately not notified: its union no longer carries the issue, so
    # nothing about it changed.
    redirected: dict[tuple[int, str], dict] = {}
    for record in affected.values():
        override_owner = issue_owner_overrides(
            db, record["odoo_instance_id"], target, [number]
        ).get(number)
        ticket_id, model_name = override_owner or (
            record["ticket_id"], record["model_name"]
        )
        redirected.setdefault((ticket_id, model_name), {
            "ticket_id": ticket_id,
            "model_name": model_name,
            "odoo_instance_id": record["odoo_instance_id"],
            "issues": record["issues"],
        })
    return list(redirected.values())
```

The caller (`worker/worker/ticket_issue_runner.py::sync_ticket_issue_state`) re-fetches the snapshot via `get_ticket_issue_union` per affected record, so the stale `issues` value carried here never reaches Odoo.

- [ ] **Step 4: Widen the estimate writer's run filter**

In `update_ticket_issue_estimate`, replace the `.where(...)` clause of the run query so a moved-on issue reaches the run that holds it. Insert before the `with db.session() as s:` block:

```python
    # Reassignment (spec 2026-08-20): Odoo addresses the estimate at the record
    # the issue sits on NOW, but the run holding the issue still belongs to the
    # record it came from. Widen the search to that run's record for issues an
    # override moved onto this one.
    owners: list[tuple[int, str]] = [(ticket_id, model_name)]
    for repo_full_name, moved_number in issues_moved_onto(
        db, odoo_instance_id, ticket_id, model_name
    ):
        if moved_number != number:
            continue
        source = natural_issue_owner(db, repo_full_name, number)
        if source is not None and source not in owners:
            owners.append(source)
```

and change the query's record filter from

```python
                TicketIssueRun.ticket_id == ticket_id,
                TicketIssueRun.model_name == model_name,
```

to

```python
                or_(*[
                    and_(TicketIssueRun.ticket_id == t, TicketIssueRun.model_name == m)
                    for t, m in owners
                ]),
```

`or_` and `and_` need importing from `sqlalchemy` at the top of `writers.py` alongside the existing `select` (check first — several are likely imported already). A tuple `IN` would be terser but behaves inconsistently across SQLite versions, and the tests run on SQLite.

Add the helper next to `_issue_item_from_runs`. It is public because Task 4's
endpoint needs it to tell "unknown issue" from "known issue moved":

```python
def natural_issue_owner(
    db: Database, repo_full_name: str, number: int
) -> tuple[int, str] | None:
    """(ticket_id, model_name) of the newest run carrying `number` — the issue's
    owner BEFORE any override. None when no run carries it, which is how the
    reassign endpoint tells "unknown issue" from "known issue moved"."""
    from sqlalchemy.orm import load_only

    with db.session() as s:
        rows = s.execute(
            select(TicketIssueRun)
            .where(
                TicketIssueRun.repo_full_name == repo_full_name.lower(),
                TicketIssueRun.issues.is_not(None),
            )
            .options(load_only(
                TicketIssueRun.ticket_id,
                TicketIssueRun.model_name,
                TicketIssueRun.issues,
                TicketIssueRun.created_at,
            ))
            .order_by(TicketIssueRun.created_at.desc(), TicketIssueRun.id.desc())
        ).scalars().all()
        for row in rows:
            if any(i.get("number") == number for i in (row.issues or [])):
                return row.ticket_id, row.model_name
    return None
```

- [ ] **Step 5: Add override targets to the ready-ticket candidate set**

In `list_ready_tickets`, after the existing candidate loop closes and before the `ready: list[dict] = []` line:

```python
    # Reassignment (spec 2026-08-20): candidates come from run rows, so a record
    # whose only issues arrived by a move would never be considered ready.
    with db.session() as s:
        moved = s.execute(
            select(
                TicketIssueReassignment.odoo_instance_id,
                TicketIssueReassignment.ticket_id,
                TicketIssueReassignment.model_name,
                TicketIssueReassignment.repo_full_name,
            ).distinct()
        ).all()
    for row in moved:
        key = (row.odoo_instance_id, row.ticket_id, row.model_name)
        candidates.setdefault(key, {
            "odoo_instance_id": row.odoo_instance_id,
            "ticket_id": row.ticket_id,
            "model_name": row.model_name,
            "repo_full_name": row.repo_full_name,
            "name": "",
        })
```

The existing `_issues_all_closed` guard already rejects a record whose union is empty, so a source record stripped of its last issue drops out on its own.

- [ ] **Step 6: Redirect `resolve_pr_tickets`**

In `reva/ticket_links.py`, replace the body of the `for row in rows:` loop:

```python
        for row in rows:
            if row.odoo_instance_id is None:
                continue
            numbers = {item.get("number") for item in (row.issues or [])}
            matched = numbers.intersection(wanted)
            if not matched:
                continue
            # Reassignment (spec 2026-08-20): an issue an operator moved is
            # owned by the override's record, not the run's. The run_id stays
            # the run that holds the plan — change_note_runner reads the ticket
            # name off it.
            overrides = writers.issue_owner_overrides(
                db, row.odoo_instance_id, repo, sorted(n for n in matched if n)
            )
            for number in sorted(n for n in matched if n):
                ticket_id, model_name = overrides.get(
                    number, (row.ticket_id, row.model_name)
                )
                key = (row.odoo_instance_id, ticket_id, model_name)
                out.setdefault(key, TicketRef(
                    odoo_instance_id=row.odoo_instance_id,
                    ticket_id=ticket_id,
                    model_name=model_name,
                    run_id=row.id,
                ))
    return list(out.values())
```

Add `from reva.db import writers` to the imports at the top of `reva/ticket_links.py`. If that creates a circular import (`writers` imports from `ticket_links`), import inside the function instead — check with `worker/.venv/bin/python -c "import reva.ticket_links"`.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `worker/.venv/bin/python -m pytest worker/tests/test_issue_reassignment_writers.py worker/tests/test_ticket_links.py -q`
Expected: PASS.

- [ ] **Step 8: Run every suite**

Run: `make test`
Expected: worker, api and scheduler all green. These four functions sit under the issue-state webhook, the change-summary path, the board work-status path, the estimate mirror and the weekly report, so a regression surfaces broadly.

- [ ] **Step 9: Commit**

```bash
git add reva/db/writers.py reva/ticket_links.py worker/tests/test_issue_reassignment_writers.py worker/tests/test_ticket_links.py
git commit -m "feat(reassign): callbacks, estimates and ready-state follow the override

update_ticket_issue_state redirects who is notified without moving where state
is written; the estimate writer reaches the source's run; ready candidates
include move targets; resolve_pr_tickets returns the new owner keeping the run
that holds the plan."
```

---

### Task 4: The endpoint

**Files:**
- Modify: `api/app/schemas/ticket_issues.py` (add after `IssueEstimateAccepted`, ~line 100)
- Modify: `api/app/routes/v1/ticket_issues.py` (add after `update_issue_estimate`, ~line 243; extend the module docstring)
- Modify: `reva/odoo_contracts.py` (`CONTRACTS` list and `_inbound_models()`)
- Modify: `api/tests/test_contracts_inbound.py` (`_MODELS`)
- Test: `api/tests/test_v1_reassign_issue.py` (new file)

**Interfaces:**
- Consumes: `writers.record_issue_reassignment`, `writers.clear_issue_reassignment` (Task 1); `writers.natural_issue_owner(db, repo_full_name, number) -> tuple[int, str] | None` (Task 3).
- Produces: `POST /api/v1/reassign-issue` returning `ReassignIssueAccepted{status: str}` with `status` in `{"reassigned", "cleared", "unknown_issue"}`.

- [ ] **Step 1: Add the schemas**

In `api/app/schemas/ticket_issues.py`, after `IssueEstimateAccepted`:

```python
class RecordRef(BaseModel):
    """One Odoo record in a reassignment. `model_name` is not constrained to a
    Literal: REVA stores it as text and Odoo owns the model list."""

    ticket_id: int
    model_name: str = Field(
        description='Odoo model name, e.g. "helpdesk.ticket" or "project.task"'
    )


class ReassignIssueRequest(BaseModel):
    """An operator moved a REVA-created GitHub issue to a different Odoo record
    (spec 2026-08-20). `from` is advisory — it is recorded for the ops log, but
    a mismatch is never an error: the Odoo wizard retries a move that already
    happened, and 409-ing there breaks exactly the case the retry exists for."""

    number: int = Field(description="GitHub issue number being moved")
    repo: str = Field(description="Repository URL, https://github.com/{owner}/{repo}")
    from_record: RecordRef = Field(
        alias="from", description="Where the issue sat before the move"
    )
    to: RecordRef = Field(description="The record that owns it now")


class ReassignIssueAccepted(BaseModel):
    """200 body. `status` is diagnostic only — Odoo checks the status code.

    reassigned    — override written
    cleared       — target is the natural owner; any override was removed
    unknown_issue — no run carries the number; the override was still written
    """

    status: str
```

`from_record` uses `alias="from"` and the model does **not** set `populate_by_name`, so the wire name `from` is the only accepted spelling — which is what Odoo sends.

- [ ] **Step 2: Write the failing tests**

Create `api/tests/test_v1_reassign_issue.py`:

```python
"""Tests for POST /api/v1/reassign-issue (spec 2026-08-20).

The load-bearing rule: this route NEVER returns 404. Odoo's Move-to wizard
reads 404/501 as "REVA has not shipped this yet" and commits the move with a
warning note that would be false.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from app.dependencies import get_db, get_github_client, get_settings
from app.main import app
from app.settings import Settings
from reva.db import Base, Database, create_engine_from_url, writers
from reva.db.models import OpsEvent
from reva.types import TicketIssueJobParams

REPO = "https://github.com/acme/widgets"

PAYLOAD = {
    "number": 42,
    "repo": REPO,
    "from": {"ticket_id": 1234, "model_name": "project.task"},
    "to": {"ticket_id": 5678, "model_name": "helpdesk.ticket"},
}


@dataclass
class FakeQueue:
    enqueued: list = field(default_factory=list)

    def enqueue(self, func_path, params, **kwargs):
        self.enqueued.append((func_path, params, kwargs))
        return type("J", (), {"id": "rq:job:fake-1"})()


@dataclass
class FakeGitHub:
    installation_id: int = 99

    def get_repo_installation_id(self, owner: str, repo: str) -> int:
        return self.installation_id


@pytest.fixture()
def client_db(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("REVA_SECRET_KEY", Fernet.generate_key().decode())
    engine = create_engine_from_url(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = Database(engine)
    settings = Settings(
        database_url="sqlite:///:memory:", github_app_id=1,
        github_webhook_secret="x", github_private_key="x",
        redis_url="redis://localhost:6379/0",
    )
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_github_client] = lambda: FakeGitHub()
    prev = getattr(app.state, "rq_queue", None)
    app.state.rq_queue = FakeQueue()
    tc = TestClient(app)
    key = tc.post("/api/v1/odoo-instances", json={
        "name": "test", "callback_url": "", "callback_api_key": "",
    }).json()["api_key"]
    yield tc, db, {"Authorization": f"Bearer {key}"}
    app.state.rq_queue = prev
    app.dependency_overrides.clear()


def _seed_issue(db: Database, ticket_id: int = 1234,
                model_name: str = "project.task") -> None:
    """A completed run owning issue #42 on acme/widgets for instance 1."""
    run_id = writers.record_ticket_issue_run_created(db, TicketIssueJobParams(
        run_id=0, odoo_instance_id=1, ticket_id=ticket_id, model_name=model_name,
        github_url=REPO, name="Ticket name", description="d", analysis_html="",
        priority="1", ticket_url="https://odoo.example/web#id=1",
    ))
    writers.update_ticket_issue_progress(db, run_id, [
        {"title": "Issue 42", "number": 42,
         "url": "https://github.com/acme/widgets/issues/42", "state": "open"},
    ])


def _ops(db: Database) -> list[str]:
    with db.session() as s:
        return [e.event for e in s.query(OpsEvent).all()]


def test_move_is_accepted_and_redirects_the_union(client_db):
    client, db, headers = client_db
    _seed_issue(db)

    r = client.post("/api/v1/reassign-issue", json=PAYLOAD, headers=headers)

    assert r.status_code == 200
    assert r.json()["status"] == "reassigned"
    assert writers.get_ticket_issue_union(db, 1, 1234, "project.task") == []
    moved = writers.get_ticket_issue_union(db, 1, 5678, "helpdesk.ticket")
    assert [i["number"] for i in moved] == [42]


def test_repeating_the_same_move_is_a_noop_200(client_db):
    client, db, headers = client_db
    _seed_issue(db)
    client.post("/api/v1/reassign-issue", json=PAYLOAD, headers=headers)

    r = client.post("/api/v1/reassign-issue", json=PAYLOAD, headers=headers)

    assert r.status_code == 200
    moved = writers.get_ticket_issue_union(db, 1, 5678, "helpdesk.ticket")
    assert [i["number"] for i in moved] == [42]


def test_stale_from_still_succeeds(client_db):
    """The Odoo wizard retries a move that already happened; `from` is advisory
    and must never 409."""
    client, db, headers = client_db
    _seed_issue(db)
    client.post("/api/v1/reassign-issue", json=PAYLOAD, headers=headers)

    stale = {**PAYLOAD, "from": {"ticket_id": 9999, "model_name": "project.task"}}
    r = client.post("/api/v1/reassign-issue", json=stale, headers=headers)

    assert r.status_code == 200


def test_moving_back_to_the_natural_owner_clears_the_override(client_db):
    client, db, headers = client_db
    _seed_issue(db)
    client.post("/api/v1/reassign-issue", json=PAYLOAD, headers=headers)

    back = {
        "number": 42, "repo": REPO,
        "from": {"ticket_id": 5678, "model_name": "helpdesk.ticket"},
        "to": {"ticket_id": 1234, "model_name": "project.task"},
    }
    r = client.post("/api/v1/reassign-issue", json=back, headers=headers)

    assert r.status_code == 200
    assert r.json()["status"] == "cleared"
    union = writers.get_ticket_issue_union(db, 1, 1234, "project.task")
    assert [i["number"] for i in union] == [42]


def test_unknown_issue_is_200_not_404(client_db):
    """404 is reserved for a REVA that lacks the route entirely — returning it
    here makes Odoo post a warning note that is simply false."""
    client, db, headers = client_db  # no run seeded

    r = client.post("/api/v1/reassign-issue", json=PAYLOAD, headers=headers)

    assert r.status_code == 200
    assert r.json()["status"] == "unknown_issue"


def test_unknown_issue_records_a_warning_ops_event(client_db):
    client, db, headers = client_db

    client.post("/api/v1/reassign-issue", json=PAYLOAD, headers=headers)

    assert "reassign_unknown_issue" in _ops(db)


def test_accepted_move_records_an_ops_event(client_db):
    client, db, headers = client_db
    _seed_issue(db)

    client.post("/api/v1/reassign-issue", json=PAYLOAD, headers=headers)

    assert "issue_reassigned" in _ops(db)


def test_unparseable_repo_is_422(client_db):
    client, db, headers = client_db
    _seed_issue(db)

    r = client.post(
        "/api/v1/reassign-issue",
        json={**PAYLOAD, "repo": "not-a-url"},
        headers=headers,
    )

    assert r.status_code == 422


def test_missing_from_is_422(client_db):
    client, db, headers = client_db
    payload = {k: v for k, v in PAYLOAD.items() if k != "from"}

    r = client.post("/api/v1/reassign-issue", json=payload, headers=headers)

    assert r.status_code == 422


def test_estimate_gate_accepts_the_new_owner_after_a_move(client_db):
    """/update-issue-estimate 404s an issue the record does not own. After a
    move the target owns it, so the gate must let it through — and the source
    must stop being accepted for it."""
    client, db, headers = client_db
    _seed_issue(db)
    client.post("/api/v1/reassign-issue", json=PAYLOAD, headers=headers)

    accepted = client.post(
        "/api/v1/update-issue-estimate",
        json={"ticket_id": 5678, "model_name": "helpdesk.ticket",
              "number": 42, "estimate_hours": 3.5},
        headers=headers,
    )
    assert accepted.status_code == 202

    rejected = client.post(
        "/api/v1/update-issue-estimate",
        json={"ticket_id": 1234, "model_name": "project.task",
              "number": 42, "estimate_hours": 3.5},
        headers=headers,
    )
    assert rejected.status_code == 404


def test_route_requires_an_instance_key(client_db):
    client, db, headers = client_db
    _seed_issue(db)

    gated = Settings(
        database_url="sqlite:///:memory:", github_app_id=1,
        github_webhook_secret="x", github_private_key="x",
        redis_url="redis://localhost:6379/0", api_key="master-key",
    )
    app.dependency_overrides[get_settings] = lambda: gated
    r = client.post(
        "/api/v1/reassign-issue", json=PAYLOAD,
        headers={"Authorization": "Bearer master-key"},
    )
    assert r.status_code in (401, 403)
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `api/.venv/bin/python -m pytest api/tests/test_v1_reassign_issue.py -q`
Expected: every test FAILS with `404` from FastAPI — the route does not exist. That 404 is FastAPI's, not the handler's; the whole point of these tests is that once the route exists it never produces one.

- [ ] **Step 4: Write the handler**

In `api/app/routes/v1/ticket_issues.py`, extend the module docstring with:

```
POST /api/v1/reassign-issue                      — move an issue to another Odoo record
```

Add `ReassignIssueAccepted` and `ReassignIssueRequest` to the `app.schemas.ticket_issues` import block, then add after `update_issue_estimate`:

```python
@create_router.post(
    "/reassign-issue",
    status_code=status.HTTP_200_OK,
    response_model=ReassignIssueAccepted,
)
def reassign_issue(
    body: ReassignIssueRequest,
    db: Database = Depends(get_db),
    instance: ResolvedOdooInstance = Depends(require_odoo_instance),
) -> dict:
    """Record that a REVA-created issue now belongs to a different Odoo record.

    **This route must never return 404.** Odoo's Move-to wizard treats 404/501
    as "REVA has not shipped this endpoint yet" and commits the move anyway,
    with a warning note saying REVA may re-link the issue. Returning 404 for an
    issue we simply do not know would make that note a lie. An unknown issue is
    a 200 that still stores the override — the mapping does not require the
    issue to exist yet — plus an ops event, because a typo'd number would
    otherwise be persisted silently.

    `from` is advisory. The wizard retries a move that already happened, so a
    stale `from` must succeed rather than 409.
    """
    parsed = parse_github_repo_url(body.repo)
    if parsed is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="repo must be an https://github.com/{owner}/{repo} URL",
        )
    repo_full_name = f"{parsed[0]}/{parsed[1]}".lower()

    natural = writers.natural_issue_owner(db, repo_full_name, body.number)
    detail = {
        "number": body.number,
        "repo": repo_full_name,
        "from": [body.from_record.ticket_id, body.from_record.model_name],
        "to": [body.to.ticket_id, body.to.model_name],
    }

    if natural == (body.to.ticket_id, body.to.model_name):
        # Moving back to where the runs already say it belongs. Storing an
        # identity override would leave a row that means nothing forever.
        writers.clear_issue_reassignment(
            db, odoo_instance_id=instance.id,
            repo_full_name=repo_full_name, number=body.number,
        )
        writers.record_ops_event(
            db, "ticket_issues", "info", "issue_reassigned",
            {**detail, "result": "cleared"},
        )
        logger.info("issue_reassignment_cleared", **detail)
        return {"status": "cleared"}

    writers.record_issue_reassignment(
        db,
        odoo_instance_id=instance.id,
        repo_full_name=repo_full_name,
        number=body.number,
        ticket_id=body.to.ticket_id,
        model_name=body.to.model_name,
    )
    if natural is None:
        # Stored anyway so a move that lands before the issue does still
        # redirects — but visible, because a typo'd number looks identical.
        writers.record_ops_event(
            db, "ticket_issues", "warning", "reassign_unknown_issue", detail,
        )
        logger.warning("issue_reassignment_unknown_issue", **detail)
        return {"status": "unknown_issue"}

    writers.record_ops_event(
        db, "ticket_issues", "info", "issue_reassigned",
        {**detail, "result": "reassigned"},
    )
    logger.info("issue_reassigned", **detail)
    return {"status": "reassigned"}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `api/.venv/bin/python -m pytest api/tests/test_v1_reassign_issue.py -q`
Expected: PASS, 11 tests.

- [ ] **Step 6: Register the contract**

In `reva/odoo_contracts.py`, add to `_inbound_models()`:

```python
        "reassign-issue": ReassignIssueRequest,
```

and its import alongside the others in that function:

```python
    from app.schemas.ticket_issues import (
        CreateIssuesRequest,
        ReassignIssueRequest,
        UpdateIssueEstimateRequest,
    )
```

Add to `CONTRACTS`, after the `update-issue-estimate` entry:

```python
    Contract(
        name="reassign-issue",
        direction="odoo->reva",
        method="POST",
        path="/api/v1/reassign-issue",
        auth="bearer:instance-inbound-key",
        sample={
            "number": 42,
            "repo": "https://github.com/acme/widgets",
            "from": {"ticket_id": 1234, "model_name": "project.task"},
            "to": {"ticket_id": 5678, "model_name": "helpdesk.ticket"},
        },
    ),
```

In `api/tests/test_contracts_inbound.py`, add the import and the `_MODELS` entry:

```python
from app.schemas.ticket_issues import (
    CreateIssuesRequest,
    ReassignIssueRequest,
    UpdateIssueEstimateRequest,
)
...
    "reassign-issue": ReassignIssueRequest,
```

`test_all_inbound_request_contracts_covered` asserts exact set equality, so the api suite is red until all three edits land together.

- [ ] **Step 7: Regenerate the contracts**

Run: `python -m reva.odoo_contracts generate`
Expected: prints a new `contracts_version` and writes `contracts/inbound/reassign-issue.{schema,sample}.json` plus an updated `contracts/manifest.json`.

Then run: `api/.venv/bin/python -m pytest api/tests/test_contracts_inbound.py -q && worker/.venv/bin/python -m pytest worker/tests/test_odoo_contracts.py worker/tests/test_contracts_generator.py -q`
Expected: PASS. Check the generated sample spells the key `from`, not `from_record` — if it shows `from_record`, the `alias` on the schema field is missing.

- [ ] **Step 8: Full verification**

Run: `make test`
Then: `worker/.venv/bin/ruff check reva worker/worker api/app scheduler/scheduler`
Expected: all three suites green, ruff clean.

- [ ] **Step 9: Commit**

```bash
git add api/app/schemas/ticket_issues.py api/app/routes/v1/ticket_issues.py reva/odoo_contracts.py reva/db/writers.py api/tests/test_v1_reassign_issue.py api/tests/test_contracts_inbound.py contracts/
git commit -m "feat(reassign): POST /api/v1/reassign-issue

Instance-gated, synchronous, and it never returns 404: Odoo's Move-to wizard
reads 404 as 'not shipped yet' and commits the move with a warning that would
be false. Unknown issue is a 200 that still stores the override, plus a warning
ops event so a typo'd number is not silent. Moving back to the natural owner
clears the row rather than writing an identity override."
```

---

### Task 5: Documentation and handoff

**Files:**
- Modify: `docs/superpowers/specs/2026-08-20-issue-reassignment-design.md` (status line)
- Modify: `HANDOFF.md` (the 2026-08-20 addendum)
- Move: spec and plan into their `archive/` subfolders
- Modify: `docs/github-issue-creation.md` (document the new endpoint alongside the other issue contracts)

- [ ] **Step 1: Document the endpoint where the other issue contracts live**

Read `docs/github-issue-creation.md` first and match its existing heading depth. Add:

```markdown
## Reassigning an issue — `POST /api/v1/reassign-issue`

An issue lands on whichever record the create-issues request named. When that
was the wrong record, moving the `reva.github.issue` row in Odoo is not enough:
Odoo replaces a record's whole issue set from REVA's union on the next
callback, so the move is undone. This endpoint corrects REVA's side.

```json
{
  "number": 42,
  "repo": "https://github.com/org/repo",
  "from": {"ticket_id": 1234, "model_name": "project.task"},
  "to":   {"ticket_id": 5678, "model_name": "helpdesk.ticket"}
}
```

Instance-key gated, synchronous, always `200` on a well-formed body:

| `status` | Meaning |
| --- | --- |
| `reassigned` | Override written; later callbacks address `to`. |
| `cleared` | `to` is already the natural owner; any override was removed. |
| `unknown_issue` | No run carries the number. The override is still written (a move may land before the issue does) and a warning ops event is recorded. |

**This route never returns 404.** Odoo's Move-to wizard reads `404`/`501` as
"REVA has not shipped this yet" and commits the move with a warning note saying
REVA may re-link the issue — which would be false. A malformed body or an
unparseable `repo` is `422`.

`from` is advisory: it is recorded in the ops event but never enforced. The
wizard retries a move that already happened, and rejecting a stale `from` would
break exactly that retry.

Storage is `ticket_issue_reassignments`; the issue plans in
`ticket_issue_runs.issues` are never rewritten, so deleting a row undoes a move.
**Any new query that resolves which record owns an issue must consult
`writers.issue_owner_overrides` / `writers.issues_moved_onto`** — forgetting
re-creates the bug this endpoint exists to fix.
```

- [ ] **Step 2: Update the spec status line**

Change `**Status: 📐 DESIGNED (2026-08-20). Not implemented.**` to `**Status: ✅ IMPLEMENTED (2026-08-20). Not deployed.**`

- [ ] **Step 3: Update HANDOFF**

In the "Addendum 2026-08-20" section, change the request-1 paragraph from "SPECCED, not implemented" to implemented, and record what is owed: the contract re-sync (`scripts/sync_contracts.sh <odoo-repo>` into `Cloudunify/reva_contracts/` plus a `contracts_version` pin bump in `cu_reva_connector/tests/test_contracts.py`, using the version `python -m reva.odoo_contracts generate` printed in Task 4) and that neither side is deployed.

State the coverage honestly: unit tests only. The migration's raw SQL is **not** exercised — tests build tables from the ORM models, so `047` is validated only by `make test-integration` against real Postgres or by the first staging boot. No live Odoo call was made.

- [ ] **Step 4: Archive the spec and plan**

```bash
git mv docs/superpowers/specs/2026-08-20-issue-reassignment-design.md docs/superpowers/specs/archive/
git mv docs/superpowers/plans/2026-08-20-issue-reassignment.md docs/superpowers/plans/archive/
```

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "docs: record the issue-reassignment endpoint and archive its spec/plan"
```

---

## Verification checklist

- [ ] `make test` green — worker, api and scheduler
- [ ] `worker/.venv/bin/ruff check reva worker/worker api/app scheduler/scheduler` clean
- [ ] `contracts/` regenerated and committed; `contracts/inbound/reassign-issue.sample.json` spells the key `from`
- [ ] The route returns no `404` on any path — re-read the handler and confirm
- [ ] Coverage stated honestly in HANDOFF: migration 047's SQL is unit-test-invisible

## Not in scope

Carried from the spec, deliberately:

- Reassigning the epic (`ticket_issue_runs.parent_issue`) — excluded from every Odoo payload by design, no Odoo row to move.
- Cross-instance moves — the key and the endpoint are both instance-scoped.
- A TUI surface beyond the ops event.
- The change-note prompt for a moved-on issue still carries the source ticket's name (`change_note_runner` reads it off `ref.run_id`). Prompt colour, not a callback address.
- A mistyped `cr/1234` branch still signals the wrong record; that path has no issue to reassign.
