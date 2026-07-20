"""PR closing-reference to Odoo-ticket resolution."""

from __future__ import annotations

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


@pytest.fixture()
def db() -> Database:
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def _issue_run(
    ticket_id: int,
    repo_full_name: str,
    issues: list[dict],
    *,
    odoo_instance_id: int = 1,
    created: datetime = datetime(2026, 6, 1, tzinfo=timezone.utc),
) -> TicketIssueRun:
    return TicketIssueRun(
        ticket_id=ticket_id,
        model_name="helpdesk.ticket",
        odoo_instance_id=odoo_instance_id,
        github_url=f"https://github.com/{repo_full_name}",
        repo_full_name=repo_full_name,
        name=f"Ticket {ticket_id}",
        description="ticket",
        analysis_html="<p>analysis</p>",
        priority="1",
        ticket_url=f"https://odoo.example/tickets/{ticket_id}",
        status="completed",
        issues=issues,
        created_at=created,
    )


def test_parse_closing_refs_dedups_same_repo_refs() -> None:
    body = "Closes #10, fixes #11, resolved #10, see owner/repo#12"

    assert parse_closing_refs(body) == [10, 11]


def test_resolve_pr_tickets_matches_repo_and_dedups_ticket(db: Database) -> None:
    with db.session() as s:
        s.add(_issue_run(123, "acme/widgets", [{"number": 10}, {"number": 11}]))
        s.add(_issue_run(123, "acme/widgets", [{"number": 12}]))
        s.add(_issue_run(456, "other/widgets", [{"number": 10}]))

    refs = resolve_pr_tickets(db, "ACME/Widgets", [10, 12])

    assert [(r.odoo_instance_id, r.ticket_id, r.model_name) for r in refs] == [
        (1, 123, "helpdesk.ticket")
    ]


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


def test_resolve_by_id_analyses_prefix_collision_is_not_a_repo_match(db: Database) -> None:
    # acme/widgets-legacy must not win the repo-match tier for acme/widgets.
    with db.session() as s:
        s.add(_analysis(210, odoo_instance_id=3,
                        github_url="https://github.com/acme/widgets-legacy",
                        created=datetime(2026, 7, 1, tzinfo=timezone.utc)))
        s.add(_analysis(210, odoo_instance_id=4,
                        github_url="https://github.com/acme/widgets",
                        created=datetime(2026, 5, 1, tzinfo=timezone.utc)))

    assert resolve_ticket_by_id(db, "acme/widgets", 210) == (4, "project.task")


def test_resolve_by_id_analyses_git_suffix_still_matches(db: Database) -> None:
    with db.session() as s:
        s.add(_analysis(210, odoo_instance_id=5,
                        github_url="https://github.com/Acme/Widgets.git"))

    assert resolve_ticket_by_id(db, "acme/widgets", 210) == (5, "project.task")


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

