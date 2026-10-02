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


@pytest.mark.parametrize(
    "prefix", ["bug", "fix", "feat", "cr", "conf", "dev", "mig", "sup", "doc"]
)
def test_extract_from_branch_all_type_prefixes(prefix: str) -> None:
    assert extract_ticket_id(f"{prefix}/210", None) == (210, "project.task", False)


def test_extract_fix_from_title_tag_when_branch_has_a_slug() -> None:
    assert extract_ticket_id(
        "fix/8070-outstanding-analytic", "[FIX] 8070 Kostenstelle der Rechnung (cu_skonto)"
    ) == (8070, "project.task", False)


def test_extract_fix_from_title_slash_token() -> None:
    assert extract_ticket_id(None, "backport of fix/99 to 17.0") == (99, "project.task", False)


def test_extract_from_branch_is_case_insensitive() -> None:
    assert extract_ticket_id("CR/210", None) == (210, "project.task", False)


@pytest.mark.parametrize("branch", ["cr/210/extra", "feature/210", "cr/abc", "cr210", "", None])
def test_extract_rejects_non_matching_branches(branch: str | None) -> None:
    assert extract_ticket_id(branch, None) is None


def test_extract_from_title_tag_form() -> None:
    assert extract_ticket_id("feature/misc", "[CR] 210 - fix invoice rounding") == (
        210, "project.task", False
    )


def test_extract_from_title_tag_form_without_space() -> None:
    assert extract_ticket_id(None, "[cr]210 follow-up") == (210, "project.task", False)


def test_extract_from_title_slash_token() -> None:
    assert extract_ticket_id(None, "backport of cr/99 to 17.0") == (99, "project.task", False)


def test_extract_title_tag_beats_slash_token() -> None:
    assert extract_ticket_id(None, "[BUG] 5 supersedes cr/9") == (5, "project.task", False)


def test_extract_branch_beats_title() -> None:
    assert extract_ticket_id("dev/7", "[CR] 210 - unrelated") == (7, "project.task", False)


def test_extract_nothing_anywhere_is_none() -> None:
    assert extract_ticket_id("feature/misc", "chore: bump deps") is None


def test_extract_rejects_overlong_ticket_ids() -> None:
    # >9 digits would overflow the DB integer bind (final-review finding).
    assert extract_ticket_id("cr/99999999999999999999", None) is None
    assert extract_ticket_id(None, "[CR] 99999999999999999999 - big") is None


def test_extract_rejects_ticket_id_zero() -> None:
    assert extract_ticket_id("cr/0", None) is None
    assert extract_ticket_id("cr/0", "[CR] 210 - real one") == (210, "project.task", False)


def test_extract_title_number_followed_by_version_dot_is_not_a_ticket() -> None:
    assert extract_ticket_id(None, "[MIG] 17.0 upgrade to Odoo 17.0") is None


# --- H-prefix → helpdesk.ticket (bare number stays project.task) ---------------


def test_extract_h_prefixed_branch_is_helpdesk() -> None:
    assert extract_ticket_id("sup/H1213", None) == (1213, "helpdesk.ticket", False)


def test_extract_h_prefix_is_case_insensitive() -> None:
    assert extract_ticket_id("sup/h1213", None) == (1213, "helpdesk.ticket", False)


def test_extract_h_prefixed_title_tag_is_helpdesk() -> None:
    assert extract_ticket_id(None, "[SUP] H1213 - portal login") == (1213, "helpdesk.ticket", False)


def test_extract_h_prefixed_title_token_is_helpdesk() -> None:
    assert extract_ticket_id(None, "backport of sup/H1213 to 17.0") == (1213, "helpdesk.ticket", False)


def test_extract_bare_h_without_digits_is_not_a_ticket() -> None:
    assert extract_ticket_id("feat/hotfix", None) is None


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
    # Unknown ticket, no model hint → the bare-id default is project.task.
    assert resolve_ticket_by_id(db, "acme/widgets", 9999) == (default_id, "project.task")


def _two_instances(db: Database) -> tuple[int, int]:
    with db.session() as s:
        s.add(_instance("prod", is_default=True))
        s.add(_instance("customer"))
    with db.session() as s:
        ids = {i.name: i.id for i in s.query(OdooInstance).all()}
    return ids["prod"], ids["customer"]


def test_declared_instance_is_the_last_rung(db: Database) -> None:
    _, customer = _two_instances(db)

    assert resolve_ticket_by_id(
        db, "acme/widgets", 9999, instance_id=customer
    ) == (customer, "project.task")


def test_declared_instance_filters_the_analysis_rungs(db: Database) -> None:
    prod, customer = _two_instances(db)
    with db.session() as s:
        s.add(_analysis(210, odoo_instance_id=prod, model_name="helpdesk.ticket"))

    # Only another instance knows the ticket: ignored, the guess applies.
    assert resolve_ticket_by_id(
        db, "acme/widgets", 210, instance_id=customer
    ) == (customer, "project.task")

    with db.session() as s:
        s.add(_analysis(
            210, odoo_instance_id=customer, model_name="helpdesk.ticket",
            created=datetime(2026, 5, 1, tzinfo=timezone.utc),
        ))

    assert resolve_ticket_by_id(
        db, "acme/widgets", 210, instance_id=customer
    ) == (customer, "helpdesk.ticket")


def test_a_run_of_the_repo_still_wins_over_the_declared_instance(db: Database) -> None:
    prod, customer = _two_instances(db)
    with db.session() as s:
        s.add(_issue_run(210, "acme/widgets", [{"number": 1}], odoo_instance_id=prod))

    assert resolve_ticket_by_id(
        db, "acme/widgets", 210, instance_id=customer
    ) == (prod, "helpdesk.ticket")


def test_without_a_declared_instance_the_ladder_is_unchanged(db: Database) -> None:
    prod, _ = _two_instances(db)

    assert resolve_ticket_by_id(
        db, "acme/widgets", 9999, instance_id=None
    ) == (prod, "project.task")


def test_resolve_by_id_unknown_ticket_honours_helpdesk_hint(db: Database) -> None:
    with db.session() as s:
        s.add(_instance("prod", is_default=True))

    with db.session() as s:
        default_id = s.query(OdooInstance).filter_by(name="prod").one().id
    # An H-prefixed extraction passes helpdesk.ticket as the fallback model.
    assert resolve_ticket_by_id(db, "acme/widgets", 9999, "helpdesk.ticket") == (
        default_id, "helpdesk.ticket"
    )


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
        db, name="acme", key_hash="h-acme", key_prefix="reva_odoo_ac",
        callback_url="", callback_api_key_enc="enc",
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
