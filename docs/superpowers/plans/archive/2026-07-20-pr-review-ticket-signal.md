# Ticket-Level PR Review Signal to Odoo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a PR links no REVA-created issue, extract the Odoo ticket ID from the PR (branch, then title) and send a ticket-level work-status signal (+ PR reference) to the ticket's Odoo instance — defaulting to the `is_default` instance for unknown tickets.

**Architecture:** Extend the existing `tickets.issue-work-status` contract with an optional ticket-level leg (no new endpoint). The fallback slots into `worker/worker/board_status_runner.py` at the point where no REVA ticket resolves from the PR's linked issues; extraction/resolution helpers live in `reva/ticket_links.py`; a new `is_default` column on `odoo_instances` (migration 041) designates the default instance. Spec: `docs/superpowers/specs/2026-07-20-pr-review-ticket-signal-design.md`.

**Tech Stack:** Python 3.14, Pydantic v2, SQLAlchemy 2 ORM, pytest (SQLite in-memory + MagicMock fakes — no Docker/network), plain-SQL idempotent migrations.

## Global Constraints

- Every Python test/lint command runs through the per-service venv: `cd /home/joseph/Projects/Cloudunify/cu_reva/worker && .venv/bin/python -m pytest tests/...` (venvs already exist — do NOT recreate them).
- The per-issue leg's wire payload must stay **byte-identical** to today: `{"ticket_id", "model_name", "issues"}` and nothing else (achieved via `model_dump(exclude_none=True)`).
- No new `RepoConfig` key — the fallback reuses the existing `work_status: bool = True` kill switch.
- No new TUI surface — degradations go through `writers.record_ops_event(...)` (component `"odoo_callback"`), which the TUI already shows.
- Migration conventions: numbered file `041_*.sql`, idempotent (`ADD COLUMN IF NOT EXISTS`, `CREATE UNIQUE INDEX IF NOT EXISTS`), matching ORM change in `reva/db/models.py` (tests build tables from the models).
- Any error a component catches and degrades around must both log AND record an ops event; extraction misses / closed PRs / kill-switch-off are normal lifecycle (debug log only, NO ops event).
- The branch type prefix (`cr`, `bug`, …) is a work-item type, never mapped to `model_name`. Fallback model constant: `"helpdesk.ticket"`.
- Contract change discipline: any edit to `reva/odoo_contracts.py` payloads requires `python -m reva.odoo_contracts generate` and committing the regenerated `contracts/` (enforced by `worker/tests/test_contracts_drift.py`).
- Commit messages end with: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
- Match existing style exactly (comment density, `# noqa: BLE001` annotations, docstring voice). Do not reformat adjacent code.

---

### Task 1: Contract + client extension (ticket-level leg on `tickets.issue-work-status`)

**Files:**
- Modify: `reva/odoo_contracts.py` (import line 19, `IssueWorkStatusPayload` lines 105–108, `CONTRACTS` entry lines 276–288)
- Modify: `reva/odoo_client.py:286-305` (`issue_work_status`)
- Modify: `worker/tests/test_odoo_contracts.py` (new tests + `ValidationError` import)
- Regenerate: `contracts/` (schema/sample/manifest — via CLI, never by hand)

**Interfaces:**
- Consumes: existing `PrRefPayload {number: int, title: str, url: str, repo: str}` and `IssueWorkStatusItem` (both already in `reva/odoo_contracts.py`).
- Produces: `IssueWorkStatusPayload` with optional `work_status: Literal["in_progress","in_review"] | None` and `pr: PrRefPayload | None`; `OdooCallbackClient.issue_work_status(ticket_id, model_name, issues, work_status=None, pr=None)`. Task 3 calls the client with `issues=[]`, `work_status=...`, `pr={...}`.

- [ ] **Step 1: Write the failing tests**

Append to `worker/tests/test_odoo_contracts.py` (and extend the top-of-file import block with `from pydantic import ValidationError`):

```python
def test_issue_work_status_rejects_payload_with_neither_leg():
    with pytest.raises(ValidationError):
        IssueWorkStatusPayload(ticket_id=1, model_name="helpdesk.ticket", issues=[])


def test_issue_work_status_per_issue_wire_shape_unchanged():
    # The pre-extension wire shape must stay byte-identical (spec 2026-07-20):
    # exclude_none drops the new optional fields on the per-issue leg.
    payload = IssueWorkStatusPayload(
        ticket_id=123,
        model_name="helpdesk.ticket",
        issues=[{"number": 42, "work_status": "in_progress"}],
    )
    assert payload.model_dump(exclude_none=True) == {
        "ticket_id": 123,
        "model_name": "helpdesk.ticket",
        "issues": [{"number": 42, "work_status": "in_progress"}],
    }


def test_issue_work_status_ticket_level_wire_shape():
    payload = IssueWorkStatusPayload(
        ticket_id=123,
        model_name="helpdesk.ticket",
        issues=[],
        work_status="in_review",
        pr={"number": 42, "title": "Fix rounding",
            "url": "https://github.com/acme/widgets/pull/42", "repo": "acme/widgets"},
    )
    assert payload.model_dump(exclude_none=True) == {
        "ticket_id": 123,
        "model_name": "helpdesk.ticket",
        "issues": [],
        "work_status": "in_review",
        "pr": {"number": 42, "title": "Fix rounding",
               "url": "https://github.com/acme/widgets/pull/42", "repo": "acme/widgets"},
    }
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/joseph/Projects/Cloudunify/cu_reva/worker && .venv/bin/python -m pytest tests/test_odoo_contracts.py -v -k issue_work_status`
Expected: the three new tests FAIL (`ValidationError` not raised; unexpected keyword `work_status`).

- [ ] **Step 3: Extend the payload model**

In `reva/odoo_contracts.py`, change line 19 from
`from pydantic import BaseModel, ConfigDict` to
`from pydantic import BaseModel, ConfigDict, model_validator`.

Replace the `IssueWorkStatusPayload` class (lines 105–108) with:

```python
class IssueWorkStatusPayload(BaseModel):
    """Two legs share this wire shape, never mixed in one call: the per-issue
    leg (issues non-empty — spec 2026-07-11) and the ticket-level leg
    (work_status + pr set, issues empty — the no-linked-issue PR fallback,
    spec 2026-07-20). Senders dump with exclude_none so the per-issue payload
    stays byte-identical to the pre-extension shape."""

    ticket_id: int
    model_name: str
    issues: list[IssueWorkStatusItem] = []
    work_status: Literal["in_progress", "in_review"] | None = None
    pr: PrRefPayload | None = None

    @model_validator(mode="after")
    def _one_leg_present(self) -> "IssueWorkStatusPayload":
        if not self.issues and self.work_status is None:
            raise ValueError(
                "either issues (per-issue leg) or work_status (ticket-level leg) is required"
            )
        return self
```

In the `CONTRACTS` list, extend the `tickets.issue-work-status` entry (lines 276–288) with an `extra_samples` kwarg after `sample={...}`:

```python
        extra_samples=[{
            "ticket_id": 123,
            "model_name": "helpdesk.ticket",
            "issues": [],
            "work_status": "in_review",
            "pr": {"number": 42, "title": "Fix rounding",
                   "url": "https://github.com/acme/widgets/pull/42",
                   "repo": "acme/widgets"},
        }],
```

- [ ] **Step 4: Extend the client method**

In `reva/odoo_client.py`, replace `issue_work_status` (lines 286–305) with:

```python
    def issue_work_status(
        self,
        ticket_id: int,
        model_name: str,
        issues: list[dict],
        work_status: str | None = None,
        pr: dict | None = None,
    ) -> None:
        """Post work-status hints to Odoo. Two legs, never mixed by callers:
        per-issue ({number, work_status} upserts against existing records,
        issues non-empty) or ticket-level (issues=[], work_status + pr set —
        the no-linked-issue PR fallback, spec 2026-07-20). A last-signal-wins
        display flag, not a state machine. exclude_none keeps the per-issue
        payload byte-identical to the pre-extension wire shape."""
        payload = IssueWorkStatusPayload(
            ticket_id=ticket_id,
            model_name=model_name,
            issues=issues,
            work_status=work_status,
            pr=pr,
        )
        self._post("/tickets/issue-work-status", payload.model_dump(exclude_none=True))
        logger.bind(ticket_id=ticket_id, model_name=model_name).info(
            "odoo_issue_work_status_ok"
        )
```

- [ ] **Step 5: Run the new tests to verify they pass**

Run: `cd /home/joseph/Projects/Cloudunify/cu_reva/worker && .venv/bin/python -m pytest tests/test_odoo_contracts.py -v`
Expected: ALL PASS (including the pre-existing sample-validation tests).

- [ ] **Step 6: Regenerate contracts and verify drift is green**

Run: `cd /home/joseph/Projects/Cloudunify/cu_reva && worker/.venv/bin/python -m reva.odoo_contracts generate`
Expected: `contracts/callbacks/tickets.issue-work-status.schema.json` changes, a new `tickets.issue-work-status.sample2.json` appears, `contracts/manifest.json` gets a new `contracts_version`.

Run: `cd /home/joseph/Projects/Cloudunify/cu_reva/worker && .venv/bin/python -m pytest tests/test_contracts_drift.py tests/test_contracts_generator.py -v`
Expected: PASS.

- [ ] **Step 7: Full worker suite + ruff**

Run: `cd /home/joseph/Projects/Cloudunify/cu_reva/worker && .venv/bin/python -m pytest tests/ -q`
Expected: PASS (no regressions — the fake in `test_board_status_runner.py` still matches the old positional call shape, which remains valid).
Run: `cd /home/joseph/Projects/Cloudunify/cu_reva && ruff check reva worker/worker`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
cd /home/joseph/Projects/Cloudunify/cu_reva
git add reva/odoo_contracts.py reva/odoo_client.py worker/tests/test_odoo_contracts.py contracts/
git commit -m "feat(contracts): ticket-level leg on tickets.issue-work-status (work_status + pr, spec 2026-07-20)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: `is_default` instance + ticket extraction + resolution ladder

**Files:**
- Create: `db/migrations/041_odoo_instance_default.sql`
- Modify: `reva/db/models.py` (`OdooInstance`, lines 743–770)
- Modify: `reva/db/writers.py:2617-2638` (`get_odoo_instance` dict)
- Modify: `reva/ticket_links.py` (new helpers + imports)
- Test: `worker/tests/test_ticket_links.py`

**Interfaces:**
- Consumes: ORM models `TicketIssueRun`, `TicketAnalysis`, `OdooInstance` (`reva/db/models.py`); `Database.session()`.
- Produces (Task 3 imports these from `reva.ticket_links`):
  - `extract_ticket_id(head_branch: str | None, pr_title: str | None) -> int | None`
  - `resolve_ticket_by_id(db: Database, repo_full_name: str, ticket_id: int) -> tuple[int, str] | None` — `(odoo_instance_id, model_name)`, `None` only when the ticket is unknown AND no active `is_default` instance exists.
  - `OdooInstance.is_default: bool` column.

- [ ] **Step 1: Write the migration**

Create `db/migrations/041_odoo_instance_default.sql`:

```sql
-- Default Odoo instance for the no-linked-issue PR fallback (spec 2026-07-20):
-- extracted ticket ids REVA has never seen resolve to this instance. At most
-- one default — enforced by the partial unique index. Setting the flag is a
-- manual deploy step (the migration cannot know which row):
--   UPDATE odoo_instances SET is_default = TRUE WHERE name = '<instance-name>';
-- Mirrors reva/db/models.py::OdooInstance.is_default.
ALTER TABLE odoo_instances ADD COLUMN IF NOT EXISTS is_default BOOLEAN NOT NULL DEFAULT FALSE;

CREATE UNIQUE INDEX IF NOT EXISTS uq_odoo_instances_default
    ON odoo_instances (is_default) WHERE is_default;
```

- [ ] **Step 2: Write the failing tests**

In `worker/tests/test_ticket_links.py`: extend the imports —

```python
from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from reva.db import Base, Database, create_engine_from_url
from reva.db.models import OdooInstance, TicketAnalysis, TicketIssueRun
from reva.ticket_links import (
    extract_ticket_id,
    parse_closing_refs,
    resolve_pr_tickets,
    resolve_ticket_by_id,
)
```

Give the existing `_issue_run` helper an optional timestamp parameter (add `created: datetime = datetime(2026, 6, 1, tzinfo=timezone.utc)` to its signature and change the last line of the constructor call to `created_at=created,`). Then append:

```python
def _analysis(
    ticket_id: int,
    *,
    odoo_instance_id: int | None = 1,
    model_name: str = "project.task",
    github_url: str | None = None,
    created: datetime = datetime(2026, 6, 1, tzinfo=timezone.utc),
) -> TicketAnalysis:
    return TicketAnalysis(
        ticket_id=ticket_id,
        model_name=model_name,
        field_name="x_reva_analysis",
        odoo_instance_id=odoo_instance_id,
        github_url=github_url,
        input_text="t",
        status="completed",
        created_at=created,
    )


def _instance(name: str, *, is_default: bool = False, active: bool = True) -> OdooInstance:
    return OdooInstance(
        name=name, key_hash=f"hash-{name}", key_prefix=f"rk_{name}",
        is_default=is_default, active=active,
    )


# --- extract_ticket_id (spec 2026-07-20) ---------------------------------------


@pytest.mark.parametrize("prefix", ["bug", "feat", "cr", "conf", "dev", "mig", "sup", "doc"])
def test_extract_from_branch_all_type_prefixes(prefix: str) -> None:
    assert extract_ticket_id(f"{prefix}/210", None) == 210


def test_extract_from_branch_is_case_insensitive() -> None:
    assert extract_ticket_id("CR/210", None) == 210


@pytest.mark.parametrize("branch", ["cr/210/extra", "feature/210", "cr/abc", "cr210", "", None])
def test_extract_rejects_non_matching_branches(branch: str | None) -> None:
    assert extract_ticket_id(branch, None) is None


def test_extract_from_title_tag_form() -> None:
    assert extract_ticket_id("feature/misc", "[CR] 210 - fix invoice rounding") == 210


def test_extract_from_title_tag_form_without_space() -> None:
    assert extract_ticket_id(None, "[cr]210 follow-up") == 210


def test_extract_from_title_slash_token() -> None:
    assert extract_ticket_id(None, "backport of cr/99 to 17.0") == 99


def test_extract_title_tag_beats_slash_token() -> None:
    assert extract_ticket_id(None, "[BUG] 5 supersedes cr/9") == 5


def test_extract_branch_beats_title() -> None:
    assert extract_ticket_id("dev/7", "[CR] 210 - unrelated") == 7


def test_extract_nothing_anywhere_is_none() -> None:
    assert extract_ticket_id("feature/misc", "chore: bump deps") is None


# --- resolve_ticket_by_id (spec 2026-07-20) ------------------------------------


def test_resolve_by_id_prefers_issue_runs_for_repo(db: Database) -> None:
    with db.session() as s:
        s.add(_issue_run(210, "acme/widgets", [{"number": 1}]))
        s.add(_analysis(210, model_name="project.task"))

    assert resolve_ticket_by_id(db, "ACME/Widgets", 210) == (1, "helpdesk.ticket")


def test_resolve_by_id_issue_runs_newest_wins(db: Database) -> None:
    with db.session() as s:
        s.add(_issue_run(210, "acme/widgets", [], odoo_instance_id=1,
                         created=datetime(2026, 6, 1, tzinfo=timezone.utc)))
        s.add(_issue_run(210, "acme/widgets", [], odoo_instance_id=2,
                         created=datetime(2026, 7, 1, tzinfo=timezone.utc)))

    assert resolve_ticket_by_id(db, "acme/widgets", 210) == (2, "helpdesk.ticket")


def test_resolve_by_id_other_repo_issue_run_is_ignored(db: Database) -> None:
    with db.session() as s:
        s.add(_issue_run(210, "other/repo", [{"number": 1}]))
        s.add(_analysis(210, model_name="project.task"))

    assert resolve_ticket_by_id(db, "acme/widgets", 210) == (1, "project.task")


def test_resolve_by_id_analyses_prefer_repo_match_over_newer(db: Database) -> None:
    with db.session() as s:
        s.add(_analysis(210, odoo_instance_id=3,
                        github_url="https://github.com/ACME/widgets",
                        created=datetime(2026, 5, 1, tzinfo=timezone.utc)))
        s.add(_analysis(210, odoo_instance_id=4, github_url=None,
                        created=datetime(2026, 7, 1, tzinfo=timezone.utc)))

    assert resolve_ticket_by_id(db, "acme/widgets", 210) == (3, "project.task")


def test_resolve_by_id_analyses_no_repo_match_newest_wins(db: Database) -> None:
    with db.session() as s:
        s.add(_analysis(210, odoo_instance_id=3,
                        created=datetime(2026, 5, 1, tzinfo=timezone.utc)))
        s.add(_analysis(210, odoo_instance_id=4,
                        created=datetime(2026, 7, 1, tzinfo=timezone.utc)))

    assert resolve_ticket_by_id(db, "acme/widgets", 210) == (4, "project.task")


def test_resolve_by_id_skips_instanceless_rows(db: Database) -> None:
    with db.session() as s:
        s.add(_analysis(210, odoo_instance_id=None))

    assert resolve_ticket_by_id(db, "acme/widgets", 210) is None


def test_resolve_by_id_unknown_ticket_uses_default_instance(db: Database) -> None:
    with db.session() as s:
        s.add(_instance("prod", is_default=True))

    with db.session() as s:
        default_id = s.query(OdooInstance).filter_by(name="prod").one().id
    assert resolve_ticket_by_id(db, "acme/widgets", 9999) == (default_id, "helpdesk.ticket")


def test_resolve_by_id_inactive_default_is_ignored(db: Database) -> None:
    with db.session() as s:
        s.add(_instance("prod", is_default=True, active=False))

    assert resolve_ticket_by_id(db, "acme/widgets", 9999) is None


def test_resolve_by_id_no_default_is_none(db: Database) -> None:
    with db.session() as s:
        s.add(_instance("prod"))

    assert resolve_ticket_by_id(db, "acme/widgets", 9999) is None


# --- is_default invariants (migration 041) --------------------------------------


def test_only_one_default_instance_allowed(db: Database) -> None:
    with db.session() as s:
        s.add(_instance("prod", is_default=True))
    with pytest.raises(IntegrityError):
        with db.session() as s:
            s.add(_instance("staging", is_default=True))


def test_multiple_non_default_instances_allowed(db: Database) -> None:
    with db.session() as s:
        s.add(_instance("prod"))
        s.add(_instance("staging"))

    assert resolve_ticket_by_id(db, "acme/widgets", 9999) is None
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd /home/joseph/Projects/Cloudunify/cu_reva/worker && .venv/bin/python -m pytest tests/test_ticket_links.py -v`
Expected: FAIL — `ImportError: cannot import name 'extract_ticket_id'` (and `OdooInstance` has no `is_default`).

- [ ] **Step 4: ORM model + writer dict**

In `reva/db/models.py`, inside `OdooInstance` after the `odoo_version` column (line 764), add:

```python
    # Default instance for the no-linked-issue PR fallback (migration 041):
    # extracted ticket ids REVA has never seen resolve here. At most one row
    # set — partial unique index below; setting it is a manual deploy step.
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
```

After the `updated_at` column (line 770), add the table args (the class currently has none):

```python
    __table_args__ = (
        # Partial UNIQUE index (migration 041): at most one default instance.
        Index(
            "uq_odoo_instances_default",
            "is_default",
            unique=True,
            postgresql_where=text("is_default"),
            sqlite_where=text("is_default"),
        ),
    )
```

(`Index`, `text`, and `Boolean` are already imported at the top of `models.py`.)

In `reva/db/writers.py`, in `get_odoo_instance`'s returned dict, add one line after `"active": row.active,`:

```python
            "is_default": row.is_default,
```

- [ ] **Step 5: Extraction + resolver helpers**

In `reva/ticket_links.py`: extend the model import (line 11) to
`from reva.db.models import OdooInstance, TicketAnalysis, TicketIssueRun`, then append after `resolve_pr_tickets`:

```python
_TICKET_BRANCH_RE = re.compile(r"^(?:bug|feat|cr|conf|dev|mig|sup|doc)/(\d+)$", re.IGNORECASE)
_TICKET_TITLE_TAG_RE = re.compile(r"\[(?:bug|feat|cr|conf|dev|mig|sup|doc)\]\s*(\d+)", re.IGNORECASE)
_TICKET_TITLE_TOKEN_RE = re.compile(r"\b(?:bug|feat|cr|conf|dev|mig|sup|doc)/(\d+)\b", re.IGNORECASE)

# Fallback model for extracted tickets REVA has never seen (spec 2026-07-20).
# The branch type prefix is a work-item type, not an Odoo model — never map it.
FALLBACK_MODEL_NAME = "helpdesk.ticket"


def extract_ticket_id(head_branch: str | None, pr_title: str | None) -> int | None:
    """Ticket id from the PR itself, for PRs with no linked REVA issue: the
    head branch (`cr/2010`, the convention ticket_issue_runner writes into
    issue bodies) first, then the PR title (`[CR] 2010 - …` tag form, then a
    `cr/2010` token). None = no recognisable reference — normal lifecycle."""
    match = _TICKET_BRANCH_RE.match((head_branch or "").strip())
    if match:
        return int(match.group(1))
    title = pr_title or ""
    match = _TICKET_TITLE_TAG_RE.search(title) or _TICKET_TITLE_TOKEN_RE.search(title)
    return int(match.group(1)) if match else None


def resolve_ticket_by_id(
    db: Database, repo_full_name: str, ticket_id: int
) -> tuple[int, str] | None:
    """(odoo_instance_id, model_name) for an extracted ticket id.

    Ladder (spec 2026-07-20): ticket_issue_runs by (repo, ticket_id) newest
    first → ticket_analyses whose github_url matches the repo → ticket_analyses
    by id alone, newest first → the active is_default instance with
    FALLBACK_MODEL_NAME. None only when the ticket is unknown to REVA AND no
    active default instance exists (caller records the ops event)."""
    repo = repo_full_name.lower()
    with db.session() as s:
        row = s.execute(
            select(TicketIssueRun.odoo_instance_id, TicketIssueRun.model_name)
            .where(
                TicketIssueRun.repo_full_name == repo,
                TicketIssueRun.ticket_id == ticket_id,
                TicketIssueRun.odoo_instance_id.is_not(None),
            )
            .order_by(TicketIssueRun.created_at.desc(), TicketIssueRun.id.desc())
            .limit(1)
        ).first()
        if row is None:
            candidates = s.execute(
                select(
                    TicketAnalysis.odoo_instance_id,
                    TicketAnalysis.model_name,
                    TicketAnalysis.github_url,
                )
                .where(
                    TicketAnalysis.ticket_id == ticket_id,
                    TicketAnalysis.odoo_instance_id.is_not(None),
                )
                .order_by(TicketAnalysis.created_at.desc(), TicketAnalysis.id.desc())
            ).all()
            needle = f"github.com/{repo}"
            row = next(
                (c for c in candidates if needle in (c.github_url or "").lower()),
                None,
            ) or (candidates[0] if candidates else None)
        if row is not None:
            return row.odoo_instance_id, row.model_name
        default_id = s.execute(
            select(OdooInstance.id)
            .where(OdooInstance.is_default.is_(True), OdooInstance.active.is_(True))
            .limit(1)
        ).scalar_one_or_none()
    return (default_id, FALLBACK_MODEL_NAME) if default_id is not None else None
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd /home/joseph/Projects/Cloudunify/cu_reva/worker && .venv/bin/python -m pytest tests/test_ticket_links.py -v`
Expected: ALL PASS.

- [ ] **Step 7: Cross-service suites + ruff (shared `reva/` changed)**

Run: `cd /home/joseph/Projects/Cloudunify/cu_reva && make test`
Expected: worker, api, and scheduler suites all PASS (the `odoo_instances` model change is visible to all three).
Run: `cd /home/joseph/Projects/Cloudunify/cu_reva && ruff check reva worker/worker api/app scheduler/scheduler`
Expected: clean.

Honest-gap note for the report: the raw-SQL partial index in migration 041 is exercised only on real Postgres (`make test-integration` or first staging boot); the SQLite suites exercise the ORM-declared twin.

- [ ] **Step 8: Commit**

```bash
cd /home/joseph/Projects/Cloudunify/cu_reva
git add db/migrations/041_odoo_instance_default.sql reva/db/models.py reva/db/writers.py reva/ticket_links.py worker/tests/test_ticket_links.py
git commit -m "feat(tickets): is_default instance + PR ticket-id extraction/resolution ladder (spec 2026-07-20)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Fallback leg in `board_status_runner`

**Files:**
- Modify: `worker/worker/board_status_runner.py` (docstring, `run_board_status_update` lines 75–110, `_update_work_status` lines 163–192, new `_send_ticket_signal`)
- Test: `worker/tests/test_board_status_runner.py` (fake + `_ctx` updates, one assertion update, new tests)

**Interfaces:**
- Consumes: `extract_ticket_id(head_branch, pr_title) -> int | None` and `resolve_ticket_by_id(db, repo, ticket_id) -> tuple[int, str] | None` from `reva.ticket_links` (Task 2); `OdooCallbackClient.issue_work_status(ticket_id, model_name, issues, work_status=None, pr=None)` (Task 1); existing `build_odoo_client`, `writers.record_ops_event`.
- Produces: new job return statuses `{"status": "ticket_signal_only"}` (fallback sent / suppressed, no linked issues for the board leg); ops events `ticket_signal_rejected` and `no_default_instance` (component `"odoo_callback"`).

- [ ] **Step 1: Update the test fixtures**

In `worker/tests/test_board_status_runner.py`:

Replace `FakeOdoo.issue_work_status` (lines 26–31) with:

```python
    def issue_work_status(self, ticket_id, model_name, issues, work_status=None, pr=None):
        self.calls.append(
            {"ticket_id": ticket_id, "model_name": model_name, "issues": issues,
             "work_status": work_status, "pr": pr}
        )
        if self.raise_exc:
            raise self.raise_exc
```

Replace `_ctx` (lines 79–92) with (adds `head_ref`/`pr_title` params; defaults chosen so no existing test changes behavior):

```python
def _ctx(db, *, pr_body="Closes #50", config_yaml=None, closing_numbers=None,
         project=_PROJECT, head_ref="feature/misc", pr_title="chore: misc"):
    github = MagicMock()
    github.get_installation_token.return_value = "tok"
    github.get_pull_request.return_value = {
        "body": pr_body, "head": {"sha": "abc", "ref": head_ref},
        "title": pr_title, "html_url": "https://github.com/acme/widgets/pull/42",
    }
    github.get_file_content.return_value = config_yaml
    github.get_closing_issue_numbers.return_value = closing_numbers or []
    github.get_project.return_value = project
    ctx = WorkerContext(
        db=db, claude=MagicMock(), runner=None, github=github,  # type: ignore[arg-type]
        reviewer=None, auditor=None, ticket_analyzer=None, verifier=None,  # type: ignore[arg-type]
    )
    set_context(ctx)
    return ctx
```

Update the one full-dict assertion in `test_pr_active_sends_in_progress_work_status` (lines 262–269) to include the new recorded keys:

```python
    assert odoo.calls == [{
        "ticket_id": 97, "model_name": "helpdesk.ticket",
        "issues": [{"number": 50, "work_status": "in_progress"}],
        "work_status": None, "pr": None,
    }]
```

- [ ] **Step 2: Write the failing tests**

Append to `worker/tests/test_board_status_runner.py` (also add `from reva.db.models import OdooInstance` to the imports at line 12):

```python
# --- Ticket-level fallback (spec 2026-07-20) -----------------------------------


_PR_REF = {"number": 42, "title": "chore: misc",
           "url": "https://github.com/acme/widgets/pull/42", "repo": "acme/widgets"}


def test_branch_fallback_sends_ticket_level_signal(db, odoo):
    # Ticket 97 is known from an issue run, but this PR links none of its issues.
    _seed_board_issue(db, ticket_id=97)
    _ctx(db, pr_body="plain refactor", head_ref="cr/97")
    out = run_board_status_update(_params("pr_active"))
    assert out == {"status": "ticket_signal_only"}
    assert odoo.calls == [{
        "ticket_id": 97, "model_name": "helpdesk.ticket", "issues": [],
        "work_status": "in_progress", "pr": _PR_REF,
    }]


def test_review_done_fallback_sends_in_review(db, odoo):
    _seed_board_issue(db, ticket_id=97)
    _ctx(db, pr_body="plain refactor", head_ref="cr/97")
    run_board_status_update(_params("review_done"))
    assert odoo.calls[0]["work_status"] == "in_review"
    assert odoo.calls[0]["issues"] == []


def test_title_fallback_when_branch_unparseable(db, odoo):
    _seed_board_issue(db, ticket_id=97)
    _ctx(db, pr_body="plain refactor", head_ref="feature/stuff",
         pr_title="[CR] 97 - fix rounding")
    run_board_status_update(_params("pr_active"))
    assert odoo.calls[0]["ticket_id"] == 97


def test_fallback_suppressed_when_issues_resolve(db, odoo):
    # Linked issues win: only the per-issue leg fires, never both.
    _seed_board_issue(db, ticket_id=97)
    _ctx(db, pr_body="Closes #50", head_ref="cr/97")
    run_board_status_update(_params("pr_active"))
    assert len(odoo.calls) == 1
    assert odoo.calls[0]["issues"] == [{"number": 50, "work_status": "in_progress"}]
    assert odoo.calls[0]["work_status"] is None


def test_no_refs_and_no_extraction_stays_no_refs(db, odoo):
    _seed_board_issue(db)
    ctx = _ctx(db, pr_body="plain refactor")
    out = run_board_status_update(_params("pr_active"))
    assert out == {"status": "no_refs"}
    ctx.github.get_file_content.assert_not_called()  # config fetch still skipped
    assert odoo.calls == []


def test_fallback_unknown_ticket_uses_default_instance(db, odoo):
    with db.session() as s:
        s.add(OdooInstance(name="prod", key_hash="h1", key_prefix="rk_1",
                           is_default=True))
    _ctx(db, pr_body="plain refactor", head_ref="cr/2010")
    out = run_board_status_update(_params("pr_active"))
    assert out == {"status": "ticket_signal_only"}
    assert odoo.calls[0]["ticket_id"] == 2010
    assert odoo.calls[0]["model_name"] == "helpdesk.ticket"


def test_fallback_unknown_ticket_no_default_records_ops_event(db, odoo):
    _ctx(db, pr_body="plain refactor", head_ref="cr/2010")
    out = run_board_status_update(_params("pr_active"))
    assert out == {"status": "no_refs"}
    assert odoo.calls == []
    events = _ops_events(db, limit=10)
    assert any(e["event"] == "no_default_instance" and e["component"] == "odoo_callback"
               for e in events)


def test_fallback_respects_work_status_kill_switch(db, odoo):
    _seed_board_issue(db, ticket_id=97)
    _ctx(db, pr_body="plain refactor", head_ref="cr/97",
         config_yaml="work_status: false\n")
    out = run_board_status_update(_params("pr_active"))
    assert out == {"status": "ticket_signal_only"}
    assert odoo.calls == []


def test_fallback_permanent_error_records_ops_event(db, odoo):
    _seed_board_issue(db, ticket_id=97)
    _ctx(db, pr_body="plain refactor", head_ref="cr/97")
    odoo.raise_exc = PermanentError("Odoo 404: no such ticket")
    out = run_board_status_update(_params("pr_active"))
    assert out == {"status": "ticket_signal_only"}
    events = _ops_events(db, limit=10)
    assert any(e["event"] == "ticket_signal_rejected" and e["component"] == "odoo_callback"
               for e in events)


def test_fallback_transient_error_reraises_for_rq_retry(db, odoo):
    _seed_board_issue(db, ticket_id=97)
    _ctx(db, pr_body="plain refactor", head_ref="cr/97")
    odoo.raise_exc = TransientError("Odoo 503")
    with pytest.raises(TransientError):
        run_board_status_update(_params("pr_active"))


def test_fallback_on_closed_pr_is_pr_closed_noop(db, odoo):
    ctx = _ctx(db, pr_body="plain refactor", head_ref="cr/97")
    ctx.github.get_pull_request.return_value = {
        "body": "plain refactor", "state": "closed", "merged": True,
        "head": {"sha": "abc", "ref": "cr/97"}, "title": "t",
        "html_url": "https://github.com/acme/widgets/pull/42",
    }
    out = run_board_status_update(_params("review_done"))
    assert out == {"status": "pr_closed"}
    assert odoo.calls == []
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd /home/joseph/Projects/Cloudunify/cu_reva/worker && .venv/bin/python -m pytest tests/test_board_status_runner.py -v`
Expected: the new fallback tests FAIL (`{"status": "no_refs"}` returned, `odoo.calls == []`); every pre-existing test PASSES (fixture changes are behavior-neutral).

- [ ] **Step 4: Implement the fallback leg**

In `worker/worker/board_status_runner.py`:

Extend the import (line 30) to:
```python
from reva.ticket_links import (
    extract_ticket_id,
    parse_closing_refs,
    resolve_pr_tickets,
    resolve_ticket_by_id,
)
```

Append to the module docstring (after the "Odoo leg" bullet, before the "Both switches" paragraph):

```
  - Ticket-level fallback (spec 2026-07-20): when NO REVA ticket resolves via
    linked issues, extract the ticket id from the PR (head branch `cr/2010`,
    then title) and send one ticket-level work-status + PR ref to the ticket's
    instance (REVA DB lookup, else the is_default instance). Same
    RepoConfig.work_status gate as the per-issue leg.
```

Replace lines 91–101 (from `if not refs:` through the `_update_work_status(...)` call) with:

```python
    ticket_refs = resolve_pr_tickets(ctx.db, repo, refs) if refs else []

    # Ticket-level fallback (spec 2026-07-20): no REVA ticket resolves via the
    # linked issues — covers "no refs at all" and "refs that aren't
    # REVA-created issues". (instance_id, ticket_id, model_name) or None.
    fallback: tuple[int, int, str] | None = None
    if not ticket_refs:
        extracted = extract_ticket_id(
            (pr.get("head") or {}).get("ref"), pr.get("title")
        )
        if extracted is not None:
            resolved = resolve_ticket_by_id(ctx.db, repo, extracted)
            if resolved is None:
                # Unknown ticket and no active default instance: the fallback
                # is configured off at the data level — visible, not silent.
                log.warning("ticket_signal_no_default_instance", ticket_id=extracted)
                writers.record_ops_event(
                    ctx.db, "odoo_callback", "warning", "no_default_instance",
                    {"repo": repo, "pr": pr_number, "ticket_id": extracted},
                )
            else:
                fallback = (resolved[0], extracted, resolved[1])

    if not refs and fallback is None:
        # No linked issues and no extractable ticket → neither leg has anything
        # to do; skip the config fetch entirely (pre-fallback semantics).
        return {"status": "no_refs"}

    # ONE config fetch serves both kill switches (spec 2026-07-11).
    board_enabled, work_enabled = _repo_flags(ctx, token, owner, name, pr, log)

    # --- Odoo work-status leg (board-independent) ---
    if work_enabled and work_status is not None:
        if ticket_refs:
            _update_work_status(ctx, repo, refs, ticket_refs, work_status, log)
        elif fallback is not None:
            _send_ticket_signal(ctx, repo, pr_number, pr, fallback, work_status, log)
```

After the `if not board_enabled:` block (lines 104–106), add the board-leg guard:

```python
    if not refs:
        # Fallback-only run: nothing for the board leg to look up.
        return {"status": "ticket_signal_only"}
```

Change `_update_work_status` to take the pre-resolved refs — signature and loop head only (body otherwise unchanged, docstring's "resolves tickets the way change_note_runner does" sentence moves to the caller comment above):

```python
def _update_work_status(
    ctx, repo: str, refs: list[int], ticket_refs: list, work_status: str, log
) -> None:
    """Send per-issue work-status hints to Odoo for the REVA-created issues this
    PR links, one callback per resolved ticket. Only the issues linked by THIS
    PR are sent (intersection with the ticket's union) — Odoo upserts by number
    against existing records."""
    for ref in ticket_refs:
```

(The rest of the function body stays exactly as it is today.)

Add the new sender after `_update_work_status`:

```python
def _send_ticket_signal(
    ctx, repo: str, pr_number: int, pr: dict,
    fallback: tuple[int, int, str], work_status: str, log,
) -> None:
    """Ticket-level signal for a PR with no linked REVA issue (spec
    2026-07-20): same last-signal-wins semantics as the per-issue leg,
    addressed at the ticket itself, with the PR ref for Odoo's chatter."""
    instance_id, ticket_id, model_name = fallback
    try:
        odoo = build_odoo_client(ctx, instance_id)
        odoo.issue_work_status(
            ticket_id=ticket_id,
            model_name=model_name,
            issues=[],
            work_status=work_status,
            pr={
                "number": pr_number,
                "title": pr.get("title") or "",
                "url": pr.get("html_url") or "",
                "repo": repo,
            },
        )
    except TransientError:
        raise  # idempotent last-signal-wins in Odoo — RQ retries the whole job
    except PermanentError:
        log.warning("ticket_signal_rejected", ticket_id=ticket_id, exc_info=True)
        writers.record_ops_event(
            ctx.db, "odoo_callback", "warning", "ticket_signal_rejected",
            {"repo": repo, "pr": pr_number, "ticket_id": ticket_id},
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /home/joseph/Projects/Cloudunify/cu_reva/worker && .venv/bin/python -m pytest tests/test_board_status_runner.py -v`
Expected: ALL PASS (new and pre-existing).

- [ ] **Step 6: Full worker suite + ruff**

Run: `cd /home/joseph/Projects/Cloudunify/cu_reva/worker && .venv/bin/python -m pytest tests/ -q`
Expected: PASS.
Run: `cd /home/joseph/Projects/Cloudunify/cu_reva && ruff check reva worker/worker`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
cd /home/joseph/Projects/Cloudunify/cu_reva
git add worker/worker/board_status_runner.py worker/tests/test_board_status_runner.py
git commit -m "feat(tickets): ticket-level PR signal fallback when no REVA issue is linked (spec 2026-07-20)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Cross-service verification, spec status, ast-odoo contracts sync

**Files:**
- Modify: `docs/superpowers/specs/2026-07-20-pr-review-ticket-signal-design.md` (Status header line only)
- Sync (other repo, leave uncommitted): regenerated `contracts/` → the contracts dir inside `/home/joseph/Projects/Cloudunify/ast-odoo`

**Interfaces:**
- Consumes: everything Tasks 1–3 committed.
- Produces: green `make test` + ruff across all three services; spec marked implemented; ast-odoo working tree carrying the fresh contracts (their commit rides their next wave, per house convention).

- [ ] **Step 1: Full verification**

Run: `cd /home/joseph/Projects/Cloudunify/cu_reva && make test`
Expected: worker, api, scheduler suites all PASS.
Run: `cd /home/joseph/Projects/Cloudunify/cu_reva && ruff check reva worker/worker api/app scheduler/scheduler`
Expected: clean.
Run: `cd /home/joseph/Projects/Cloudunify/cu_reva/worker && .venv/bin/python -m pytest tests/test_contracts_drift.py -q`
Expected: PASS (contracts/ current).

- [ ] **Step 2: Mark the spec implemented**

In `docs/superpowers/specs/2026-07-20-pr-review-ticket-signal-design.md`, change the line
`- **Status:** approved (design), not yet planned` to
`- **Status:** implemented 2026-07-20 (REVA side; ast-odoo controller + prod is_default flag pending)`.

- [ ] **Step 3: Sync contracts to ast-odoo (working tree only — do NOT commit there)**

Locate the committed contracts mirror inside `/home/joseph/Projects/Cloudunify/ast-odoo` (find the directory containing a `manifest.json` with a `contracts_version` key, e.g. `grep -rl "contracts_version" /home/joseph/Projects/Cloudunify/ast-odoo --include=manifest.json`). **Before syncing**, note the OLD hash from that mirror's `manifest.json` (`contracts_version` field). Then copy the regenerated tree over it:

```bash
rsync -a --delete /home/joseph/Projects/Cloudunify/cu_reva/contracts/ <ast-odoo contracts dir>/
```

Then `git -C /home/joseph/Projects/Cloudunify/ast-odoo grep -l "<OLD hash>"` — for any file still pinning the old hash (tests/config), replace it with the new `contracts_version` from `cu_reva/contracts/manifest.json`. If the mirror directory cannot be identified unambiguously, STOP and report instead of guessing — do not create a new directory in ast-odoo.

- [ ] **Step 4: Commit (cu_reva only)**

```bash
cd /home/joseph/Projects/Cloudunify/cu_reva
git add docs/superpowers/specs/2026-07-20-pr-review-ticket-signal-design.md
git commit -m "docs(specs): mark PR-review ticket-signal spec implemented (REVA side)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

- [ ] **Step 5: Final report**

Report honestly: unit-tested only (SQLite + fakes — no live Odoo callback, no real Postgres); migration 041's raw SQL and partial index untested until `make test-integration`/staging; ast-odoo controller change + the manual prod step (`UPDATE odoo_instances SET is_default = TRUE WHERE name = '<instance-name>';`) still owed.
