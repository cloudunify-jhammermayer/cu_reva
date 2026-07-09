"""Ticket issue creation job orchestration (github-issues handoff).

run_ticket_issues is what RQ calls for each enqueued create-issues job. The
api enqueues it with rq.Retry(max=3, interval=[30, 120, 300]) — the backoff
the handoff's Contract 2 response table mandates for 5xx/network callback
failures.

Differences from the ticket-analysis runner worth knowing:
  - Completion is reported to Odoo via the issues-created callback (Contract 2),
    not a field write — Odoo renders the issue links itself.
  - Failures mark the run failed and best-effort send a "failed" callback:
    Odoo hides the Create Issues button while the record is pending and only a
    callback can unblock it. Transient errors first ride the RQ retries (row
    stays pending; the rerun resumes from the persisted plan); the failed
    callback goes out on the final attempt.
  - Re-runs must not duplicate issues. Resolution order:
      1. resume this run's own persisted plan (requeue),
      2. adopt the latest prior run's plan for the same record+repo from OUR
         DB (re-click after the timeout race or a partial failure — includes
         the not-yet-created remainder, immune to GitHub search-index lag),
      3. reconcile against the hidden ticket-level marker via GitHub search
         (fallback when the DB has no plan, e.g. after a DB reset),
      4. plan with Claude.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter

import structlog
from rq import get_current_job

from reva.db import writers
from reva.errors import PermanentError, TransientError
from reva.github_urls import parse_github_project_url, parse_github_repo_url
from reva.types import TicketIssueJobParams
from worker.runner import build_odoo_client, get_context, instance_budget_exceeded

logger = structlog.get_logger()

# Label applied to every ticket issue. REVA creates it per-repo if missing, so
# you can filter `label:reva-ticket` without setting anything up by hand.
_TICKET_ISSUE_LABEL = "reva-ticket"

# GitHub label per work-item type: name = the code itself (filter `label:CR`),
# ensured per-repo on demand. (color, description).
_TYPE_LABELS = {
    "BUG": ("d73a4a", "Bug fix"),
    "FEAT": ("a2eeef", "New feature"),
    "CR": ("0075ca", "Change request"),
    "CONF": ("bfd4f2", "Configuration change"),
    "DEV": ("7057ff", "Development task"),
    "MIG": ("fbca04", "Migration"),
    "SUP": ("008672", "Support"),
    "DOC": ("0e8a16", "Documentation"),
}
_FALLBACK_TYPE = "DEV"  # plans persisted before the type rollout carry no type

# Board field policy (spec table): reuse-by-name, create only what's missing,
# never rewrite existing options (destructive to customer boards).
_PLAN_DATE_LOOKUP = ("plan date", "target date")   # case-insensitive, DATE type
_PLAN_DATE_FIELD = "Plan date"
_PRIORITY_FIELD = "Priority"
_PRIORITY_BY_ODOO = {"0": "Low", "1": "Medium", "2": "High", "3": "Urgent"}
_PRIORITY_CREATE_OPTIONS = [
    {"name": "Low", "color": "GRAY", "description": ""},
    {"name": "Medium", "color": "BLUE", "description": ""},
    {"name": "High", "color": "YELLOW", "description": ""},
    {"name": "Urgent", "color": "RED", "description": ""},
]
_STATUS_TODO = "Todo"
_TLDR_MAX = 30
_BODY_MAX_CHARS = 900
_PLACEHOLDER_VALUES = {
    "",
    "placeholder",
    "todo",
    "tbd",
    "n/a",
    "na",
    "-",
}


def _retries_remaining() -> bool:
    """True when RQ will retry this job after the current attempt fails."""
    job = get_current_job()
    return bool(job is not None and getattr(job, "retries_left", None))


def _ticket_digest(
    owner: str, repo: str, model_name: str, ticket_id: int, basis: str
) -> str:
    """16-hex content-addressed digest of a ticket + planning basis (see
    _ticket_marker for why each field is in the key)."""
    key = f"{owner.lower()}/{repo.lower()}\x00{model_name}\x00{ticket_id}\x00{basis}"
    return hashlib.sha1(  # nosemgrep: python.lang.security.insecure-hash-algorithms.insecure-hash-algorithm-sha1
        key.encode(), usedforsecurity=False
    ).hexdigest()[:16]


def _ticket_marker(
    owner: str, repo: str, model_name: str, ticket_id: int, basis: str
) -> str:
    """Stable token in EVERY issue body for this ticket (children + parent).
    Frozen string: changing it orphans every existing ticket's issues.

    Ticket-level on purpose: issue titles are Claude-generated and change
    between plans, so a title-keyed marker (the audit pattern) would stop
    matching on every re-run and duplicate the whole issue set. Owner/repo are
    lower-cased because GitHub treats them case-insensitively while Odoo's
    github_url is free text — an admin "fixing" the URL's casing must not
    change the key. The basis digest IS part of the key: when the planning
    input changes, the old issues are deliberately not reconciled — a fresh
    set is created for the new content."""
    return "revaticket" + _ticket_digest(owner, repo, model_name, ticket_id, basis)


def _parent_marker(
    owner: str, repo: str, model_name: str, ticket_id: int, basis: str
) -> str:
    """Additional token in the PARENT body only — same digest, distinct prefix.
    Lets reconciliation (DB-wiped) tell the parent apart from its children via a
    second, ticket-specific search."""
    return "revaticketparent" + _ticket_digest(owner, repo, model_name, ticket_id, basis)


def _same_repo(github_url: str, owner: str, repo: str) -> bool:
    parsed = parse_github_repo_url(github_url)
    if parsed is None:
        return False
    return (parsed[0].lower(), parsed[1].lower()) == (owner.lower(), repo.lower())


def _item_type(params: TicketIssueJobParams, item: dict) -> str:
    """A typed request fixes every issue's type; else the planner's pick,
    falling back to DEV for pre-rollout persisted plans."""
    return params.issue_type or item.get("type") or _FALLBACK_TYPE


def _issue_title(params: TicketIssueJobParams, position: int, total: int,
                 title: str, issue_type: str) -> str:
    """GitHub issue title: '[CR] 2010 - <tldr>' plus ' (3/10)' when the request
    yields several issues. The ticket id makes every issue traceable to its
    ticket from the GitHub list alone; the planner is prompted for a ≤30-char
    tldr and the slice is the hard backstop."""
    seq = f" ({position}/{total})" if total >= 2 else ""
    return f"[{issue_type}] {params.ticket_id} - {title[:_TLDR_MAX].rstrip()}{seq}"


def _dominant_type(params: TicketIssueJobParams, issues: list[dict]) -> str:
    """Most common child type; tie → the first child's."""
    types = [_item_type(params, i) for i in issues]
    counts = Counter(types)
    best = max(counts.values())
    return next(t for t in types if counts[t] == best)


def _format_issue_body(item: dict, params: TicketIssueJobParams, marker: str) -> str:
    """Render one planned issue as a GitHub issue body with the mandatory
    Odoo back-link (Contract 1: ticket_url) and the hidden dedup marker."""
    # .get: the retention purge strips bodies from old plans; a requeued
    # post-purge item still renders its criteria + back-link.
    lines = [_compact_issue_body(item.get("body", ""))]
    if item.get("acceptance_criteria"):
        lines += ["", "### Acceptance criteria", ""]
        lines += [f"- [ ] {criterion}" for criterion in item["acceptance_criteria"]]
    lines += [
        "",
        "---",
        f"**Odoo ticket:** [{params.name}]({params.ticket_url})",
        "",
        "<sub>🤖 Created by REVA from an Odoo ticket.</sub>",
        f"<!-- {marker} -->",
    ]
    return "\n".join(lines)


def _plain_text(value: str) -> str:
    """Small HTML-to-text fallback for REVA analysis snippets in issue bodies."""
    text = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", text).strip()


def _compact_issue_body(value: str) -> str:
    body = value.strip()
    if len(body) <= _BODY_MAX_CHARS:
        return body
    cutoff = body.rfind("\n\n", 0, _BODY_MAX_CHARS)
    if cutoff < 300:
        cutoff = body.rfind(". ", 0, _BODY_MAX_CHARS)
        if cutoff >= 300:
            cutoff += 1
    if cutoff < 300:
        cutoff = _BODY_MAX_CHARS
    return body[:cutoff].rstrip() + "\n\n_(Trimmed by REVA; see the Odoo ticket for full context.)_"


def _is_placeholder(value: object) -> bool:
    if not isinstance(value, str):
        return True
    normalized = re.sub(r"\s+", " ", value).strip().lower()
    return normalized in _PLACEHOLDER_VALUES


def _fallback_issue_title(params: TicketIssueJobParams) -> str:
    title = params.name.strip()
    return title if title and not _is_placeholder(title) else "Clarify ticket requirements"


def _fallback_issue_body(params: TicketIssueJobParams) -> str:
    sections = [
        "Create the implementation issue from the original Odoo ticket.",
        "",
        "### Ticket",
        params.name.strip() or f"{params.model_name} #{params.ticket_id}",
    ]
    if params.description.strip():
        sections += ["", "### Description", params.description.strip()]
    if params.analysis_html.strip():
        analysis = _plain_text(params.analysis_html)
        if analysis:
            sections += ["", "### REVA analysis", analysis]
    if params.description_docx is not None:
        sections += [
            "",
            "### Attachment",
            f"Original request included `{params.description_docx.filename}`. "
            "The file is not retained after planning; see the Odoo ticket.",
        ]
    return "\n".join(sections)


def _normalize_planned_issue(item, params: TicketIssueJobParams) -> dict:
    """Never let an empty/placeholder Claude plan create an empty GitHub issue."""
    title = item.title
    body = item.body
    if _is_placeholder(title):
        title = _fallback_issue_title(params)
    if _is_placeholder(body):
        body = _fallback_issue_body(params)
    return {
        "title": title,
        "body": body,
        "acceptance_criteria": item.acceptance_criteria,
        "type": params.issue_type or item.type,
        "number": None,
        "url": None,
        "state": None,
        "id": None,
        "attached": False,
    }


def _needs_parent_issue(parent: dict | None, issues: list[dict]) -> bool:
    """Create/maintain an epic only when it adds structure.

    A single planned issue is already the ticket's work item; adding a parent
    creates two near-identical GitHub issues. Multi-issue plans still get a
    parent, and later single-issue runs attach to an existing parent when one
    already exists for that ticket.
    """
    if parent is not None:
        return True
    if len(issues) < 2:
        return False
    return any(i.get("number") is None for i in issues)


def _parent_title(params: TicketIssueJobParams, dominant: str) -> str:
    """Parent ("epic") title: '[FEAT] 2010 - <ticket-name tldr>' — dominant
    child type, no (n/total) order marker."""
    return f"[{dominant}] {params.ticket_id} - {params.name[:_TLDR_MAX].rstrip()}"


def _format_parent_body(params: TicketIssueJobParams, marker: str, parent_marker: str) -> str:
    """Synthesized locally (no Claude): a back-link + both hidden markers. GitHub
    renders the sub-issue checklist itself, so we don't list children here."""
    return "\n".join([
        "Tracking issue for the linked Odoo ticket. "
        "Its work items are attached below as sub-issues.",
        "",
        "---",
        f"**Odoo ticket:** [{params.name}]({params.ticket_url})",
        "",
        "<sub>🤖 Created by REVA from an Odoo ticket.</sub>",
        f"<!-- {marker} -->",
        f"<!-- {parent_marker} -->",
    ])


def _send_failed_callback(ctx, params: TicketIssueJobParams, error: str, log) -> None:
    """Best-effort failure callback so Odoo leaves 'pending' and re-enables the
    Create Issues button. Must never mask the original error."""
    try:
        odoo = build_odoo_client(ctx, params.odoo_instance_id)
        odoo.issues_created(
            ticket_id=params.ticket_id,
            model_name=params.model_name,
            request_id=params.run_id,
            status="failed",
            issues=[],
            error=error[:200],
        )
    except Exception:
        log.warning("ticket_issues_failed_callback_error", exc_info=True)
        writers.record_ops_event(
            ctx.db,
            "odoo_callback",
            "error",
            "issues_failed_callback_error",
            {"run_id": params.run_id, "ticket_id": params.ticket_id},
        )


def run_ticket_issues(job_params: dict) -> dict:
    """RQ task entry point for ticket issue creation."""
    ctx = get_context()
    params = TicketIssueJobParams.model_validate(job_params)
    odoo = build_odoo_client(ctx, params.odoo_instance_id)

    log = logger.bind(
        run_id=params.run_id,
        ticket_id=params.ticket_id,
        model_name=params.model_name,
        github_url=params.github_url,
    )
    log.info("ticket_issues_start")

    try:
        issues = _plan_and_create(ctx, params, log)
    except TransientError as exc:
        log.warning("ticket_issues_transient_error", error=str(exc))
        if _retries_remaining():
            # Row stays pending; the RQ retry resumes from the persisted plan.
            raise
        writers.record_ticket_issue_run_failed(ctx.db, params.run_id, str(exc))
        _send_failed_callback(ctx, params, str(exc), log)
        raise
    except PermanentError as exc:
        log.error("ticket_issues_error", error=str(exc))
        writers.record_ticket_issue_run_failed(ctx.db, params.run_id, str(exc))
        _send_failed_callback(ctx, params, str(exc), log)
        raise
    except Exception as exc:
        log.exception("ticket_issues_unexpected_error")
        writers.record_ticket_issue_run_failed(ctx.db, params.run_id, str(exc))
        _send_failed_callback(ctx, params, str(exc), log)
        raise PermanentError(str(exc)) from exc

    # Persist completion before the callback so the result is never lost.
    writers.record_ticket_issue_run_completed(ctx.db, params.run_id, issues)

    # Odoo's issues-created handler REPLACES the record's issue list with this
    # payload — send the union across all of this record's runs so earlier
    # requests' issues survive (wizard + planner requests accumulate).
    union = writers.get_ticket_issue_union(
        ctx.db, params.odoo_instance_id, params.ticket_id, params.model_name
    )
    try:
        odoo.issues_created(
            ticket_id=params.ticket_id,
            model_name=params.model_name,
            request_id=params.run_id,
            status="created",
            issues=union,
        )
    except TransientError:
        # Contract 2: 5xx/network on the callback must be retried. Re-raise so
        # the RQ retry reruns this job — it short-circuits (everything has an
        # issue number) and just re-sends the callback.
        log.warning("ticket_issues_odoo_callback_transient", exc_info=True)
        raise
    except PermanentError:
        # Contract 2: 4xx is do-not-retry. A 409 means Odoo moved on (its 10s
        # timeout race, or the user re-clicked and a newer request_id rules) —
        # the issues exist and are persisted, so the run stays completed and
        # the re-click path re-links them.
        log.warning("ticket_issues_odoo_callback_permanent", exc_info=True)

    log.info("ticket_issues_done", issues=len(union))
    return {"status": "completed", "run_id": params.run_id, "issues": len(union)}


def sync_ticket_issue_state(job_params: dict) -> dict:
    """RQ task: a GitHub `issues` webhook reported one of our issues closed or
    reopened. Record the per-issue state on every run carrying it, then notify
    Odoo per affected record so the rendered links show what's done.

    params keys: owner, repo, number, state ("closed" | "open").
    """
    ctx = get_context()
    try:
        owner = job_params["owner"]
        repo = job_params["repo"]
        number = job_params["number"]
        state = job_params["state"]
    except KeyError as exc:
        raise PermanentError(f"sync_ticket_issue_state: missing param {exc}") from exc
    # Optional (added later): absent in jobs queued before the deploy → None.
    closed_at = job_params.get("closed_at")

    log = logger.bind(owner=owner, repo=repo, number=number, state=state)

    affected = writers.update_ticket_issue_state(ctx.db, owner, repo, number, state, closed_at)
    if not affected:
        # Labeled like ours but not in our DB (manually labeled, or a wiped DB).
        log.info("ticket_issue_state_no_match")
        return {"status": "no_match"}

    notified = 0
    for record in affected:
        snapshot = writers.get_ticket_issue_union(
            ctx.db, record["odoo_instance_id"], record["ticket_id"], record["model_name"]
        )
        try:
            odoo = build_odoo_client(ctx, record["odoo_instance_id"])
            odoo.issue_state(
                ticket_id=record["ticket_id"],
                model_name=record["model_name"],
                number=number,
                state=state,
                issues=snapshot,
            )
            notified += 1
            if state == "closed" and snapshot and all(
                item.get("state") == "closed" for item in snapshot
            ):
                try:
                    odoo.tickets_ready(
                        ticket_id=record["ticket_id"],
                        model_name=record["model_name"],
                        issues=snapshot,
                    )
                    log.info("ticket_ready_sent", ticket_id=record["ticket_id"])
                except TransientError:
                    raise
                except PermanentError:
                    log.warning(
                        "tickets_ready_permanent",
                        ticket_id=record["ticket_id"],
                        exc_info=True,
                    )
                    writers.record_ops_event(
                        ctx.db,
                        "odoo_callback",
                        "warning",
                        "tickets_ready_rejected",
                        {"ticket_id": record["ticket_id"]},
                    )
        except TransientError:
            # Odoo down: re-raise so the RQ retry re-syncs (the DB update is
            # idempotent and re-running re-sends the remaining records).
            log.warning("ticket_issue_state_odoo_transient", exc_info=True)
            raise
        except PermanentError:
            # e.g. 409: the record's links are not in 'created' (re-run in
            # flight, or failed) — the DB state is recorded; Odoo catches up
            # with the next issues-created callback.
            log.warning("ticket_issue_state_odoo_permanent",
                        ticket_id=record["ticket_id"], exc_info=True)

    log.info("ticket_issue_state_done", records=len(affected), notified=notified)
    return {"status": "completed", "records": len(affected), "notified": notified}


def _board_context(ctx, token: str, params: TicketIssueJobParams, log) -> dict | None:
    """Resolve the target board once per run: project id + the field/option ids
    the projection loop needs. Returns None (after an ops event) when the URL
    doesn't parse — the route validates, so this guards requeued legacy rows.
    Raises on GraphQL errors; the caller's fail-soft wrapper owns those."""
    parsed = parse_github_project_url(params.github_project_url or "")
    if parsed is None:
        log.warning("ticket_issues_project_url_invalid", url=params.github_project_url)
        writers.record_ops_event(ctx.db, "github", "warning", "project_url_invalid",
                                 {"run_id": params.run_id})
        return None
    owner_type, owner, number = parsed
    project = ctx.github.get_project(token, owner_type, owner, number)
    fields = project["fields"]

    def _find(names: tuple[str, ...], data_type: str) -> dict | None:
        for name in names:
            for f in fields:
                if f["name"].lower() == name.lower() and f["dataType"] == data_type:
                    return f
        return None

    def _option_id(fld: dict, option_name: str, purpose: str) -> str | None:
        for opt in fld.get("options") or []:
            if opt["name"].lower() == option_name.lower():
                return opt["id"]
        # Never mutate an existing field's options — skip, visibly.
        log.warning("ticket_issues_project_field_unmatched",
                    field=fld["name"], wanted=option_name)
        writers.record_ops_event(ctx.db, "github", "warning", "project_field_unmatched",
                                 {"run_id": params.run_id, "field": fld["name"],
                                  "wanted": option_name, "purpose": purpose})
        return None

    date_field = _find(_PLAN_DATE_LOOKUP, "DATE")
    if date_field is None and params.plan_date is not None:
        date_field = ctx.github.create_project_field(
            token, project["id"], _PLAN_DATE_FIELD, "DATE")

    status_field = _find(("Status",), "SINGLE_SELECT")
    todo_id = _option_id(status_field, _STATUS_TODO, "status") if status_field else None

    priority_field = _find((_PRIORITY_FIELD,), "SINGLE_SELECT")
    if priority_field is None:
        priority_field = ctx.github.create_project_field(
            token, project["id"], _PRIORITY_FIELD, "SINGLE_SELECT",
            options=_PRIORITY_CREATE_OPTIONS)
    wanted = _PRIORITY_BY_ODOO.get(params.priority, "Medium")
    priority_id = _option_id(priority_field, wanted, "priority")

    return {
        "project_id": project["id"],
        "date_field_id": date_field["id"] if date_field else None,
        "status": (status_field["id"], todo_id) if todo_id else None,
        "priority": (priority_field["id"], priority_id) if priority_id else None,
    }


def _project_step(ctx, token, owner, repo, params, issues, parent, log) -> None:
    """Add the epic + children to the requested Projects v2 board and stamp
    Plan date / Status=Todo / Priority. Fail-soft by spec decision 5: the board
    is a bonus — any failure logs + ops-events and the run completes. The
    persisted project_item_id is the only guard against re-setting fields on a
    card a developer already moved, so it is written after each item."""
    try:
        board = _board_context(ctx, token, params, log)
        if board is None:
            return

        def _node_id(item: dict) -> str | None:
            if item.get("node_id"):
                return item["node_id"]
            if item.get("number") is None:
                return None
            # Pre-feature item: backfill the GraphQL id via REST.
            fetched = ctx.github.get_issue(token, owner, repo, item["number"])
            return (fetched or {}).get("node_id")  # None: deleted → skip

        def _place(item: dict) -> str | None:
            node = _node_id(item)
            if node is None:
                return None
            item_id = ctx.github.add_issue_to_project(token, board["project_id"], node)
            if board["date_field_id"] and params.plan_date is not None:
                ctx.github.set_project_item_date(
                    token, board["project_id"], item_id, board["date_field_id"],
                    params.plan_date.isoformat())
            for pair in (board["status"], board["priority"]):
                if pair:
                    ctx.github.set_project_item_option(
                        token, board["project_id"], item_id, pair[0], pair[1])
            item["node_id"] = node
            return item_id

        if parent is not None and not parent.get("project_item_id"):
            item_id = _place(parent)
            if item_id:
                parent["project_item_id"] = item_id
                writers.set_ticket_issue_parent(ctx.db, params.run_id, parent)
                log.info("ticket_issue_projected", issue=parent.get("number"))
        for idx, item in enumerate(issues):
            if item.get("project_item_id"):
                continue
            item_id = _place(item)
            if item_id:
                issues[idx] = {**item, "project_item_id": item_id}
                writers.update_ticket_issue_progress(ctx.db, params.run_id, issues)
                log.info("ticket_issue_projected", issue=item["number"])
    except Exception:
        log.warning("ticket_issues_project_step_failed", exc_info=True)
        writers.record_ops_event(
            ctx.db, "github", "warning", "project_step_failed",
            {"run_id": params.run_id, "ticket_id": params.ticket_id,
             "project_url": params.github_project_url})


def _plan_and_create(ctx, params: TicketIssueJobParams, log) -> list[dict]:
    """Resolve the issue list for this run (resume → adopt → reconcile → plan),
    then create whatever is missing. Returns the final issues state."""
    parsed = parse_github_repo_url(params.github_url)
    if parsed is None:  # route validates; guards requeued rows and manual enqueues
        raise PermanentError(f"invalid github_url: {params.github_url!r}")
    owner, repo = parsed

    row = writers.get_ticket_issue_run(ctx.db, params.run_id)
    # Computed once at row creation and stored (the doc isn't kept), so the
    # marker is stable across this run and its requeues.
    basis = (row or {}).get("planning_basis") or "text:none"
    issues = (row or {}).get("issues") or None
    parent = (row or {}).get("parent_issue") or None

    if issues is None:
        prior = writers.get_latest_ticket_issue_plan(
            ctx.db, params.ticket_id, params.model_name, exclude_run_id=params.run_id
        )
        if (
            prior is not None
            and _same_repo(prior["github_url"], owner, repo)
            and prior.get("planning_basis") == basis
        ):
            # Re-click after the timeout race or a partial failure with
            # UNCHANGED inputs: the prior run's list is authoritative (it
            # includes the not-yet-created remainder, which GitHub's marker
            # search can't know about). A changed docx/description has a
            # different basis and falls through to a fresh plan instead.
            issues = [dict(item) for item in prior["issues"]]
            writers.update_ticket_issue_progress(ctx.db, params.run_id, issues)
            log.info("ticket_issues_plan_adopted", from_run=prior["id"])

    if parent is None:
        # One epic per ticket: adopt the parent an earlier run created in this
        # repo so new issues (wizard requests, re-plans over changed text)
        # attach to the existing epic instead of spawning a second one.
        prior_parent = writers.get_latest_ticket_issue_parent(
            ctx.db, params.odoo_instance_id, params.ticket_id, params.model_name,
            f"{owner.lower()}/{repo.lower()}", exclude_run_id=params.run_id,
        )
        if prior_parent is not None:
            parent = prior_parent
            writers.set_ticket_issue_parent(ctx.db, params.run_id, parent)
            log.info("ticket_issue_parent_adopted", issue=parent.get("number"))

    if issues is not None:
        need_parent = _needs_parent_issue(parent, issues)
        done = all(i.get("number") is not None for i in issues)
        if need_parent:
            done = done and parent is not None and all(i.get("attached") for i in issues)
        if done and params.github_project_url:
            # Projection-aware: a fail-soft board miss (or a pre-feature run
            # meeting its first project URL) must still reach the project step.
            done = all(i.get("project_item_id") for i in issues) and (
                parent is None or parent.get("project_item_id"))
        if done:
            # Nothing to create/attach (callback resend / fully-created adoption):
            # skip the GitHub round-trips entirely.
            return issues

    marker = _ticket_marker(owner, repo, params.model_name, params.ticket_id, basis)
    parent_marker = _parent_marker(owner, repo, params.model_name, params.ticket_id, basis)
    installation_id = ctx.github.get_repo_installation_id(owner, repo)
    token = ctx.github.get_installation_token(installation_id)

    if issues is None:
        existing = ctx.github.find_issues_with_marker(token, owner, repo, marker)
        if existing:
            # DB-wiped reconcile: the marker matches parent + children. Split the
            # parent out (Task 4) so it never reaches the Odoo payload.
            # Assumes the parent's revaticketparent marker is indexed by GitHub
            # search by the time we reconcile; if it lags, the parent is treated
            # as a child (accepted — narrow DB-loss window, consistent with the
            # existing GitHub search-lag handling elsewhere).
            parent_hits = ctx.github.find_issues_with_marker(token, owner, repo, parent_marker)
            parent_numbers = {h["number"] for h in parent_hits}
            children = [e for e in existing if e["number"] not in parent_numbers]
            if parent_hits and parent is None:
                h = parent_hits[0]
                parent = {"number": h["number"], "id": h["id"], "url": h["url"],
                          "title": h["title"], "state": h.get("state", "open"),
                          "node_id": h.get("node_id")}
                writers.set_ticket_issue_parent(ctx.db, params.run_id, parent)
            issues = [dict(c) for c in children]
            writers.update_ticket_issue_progress(ctx.db, params.run_id, issues)
            log.info("ticket_issues_reconciled", existing=len(existing), children=len(children))
        else:
            spent = instance_budget_exceeded(ctx, params.odoo_instance_id)
            if spent is not None:
                error = (
                    f"Odoo instance daily budget reached (~${spent:.2f} in 24h); "
                    f"issue planning declined."
                )
                log.warning("ticket_issues_instance_over_budget", spent_usd=round(spent, 2))
                writers.record_ticket_issue_run_failed(ctx.db, params.run_id, error)
                _send_failed_callback(ctx, params, error, log)
                raise PermanentError(error)
            response, plan = ctx.ticket_issue_planner.plan_with_response(params)
            issues = [_normalize_planned_issue(item, params) for item in plan.issues]
            # Plan + spend persist BEFORE any GitHub call: a partial failure must
            # resume from this plan, never re-plan (titles would drift and the
            # issue set would duplicate).
            cost = writers.record_ticket_issue_plan(ctx.db, params.run_id, issues, response)
            writers.record_claude_spend(ctx.db, "ticket_issues", cost)

    # Every ticket gets an epic: whenever anything is still to create, adopt the
    # ticket's existing parent or create one — including single-issue (typed
    # wizard) requests. Pre-feature runs reconciled with all issues already
    # created and no parent stay flat — we don't backfill epics (spec scope),
    # and attaching them is impossible anyway (no GitHub id on legacy items).
    need_parent = _needs_parent_issue(parent, issues)

    ctx.github.ensure_label(
        token, owner, repo, _TICKET_ISSUE_LABEL,
        description="Issues created from Odoo tickets by REVA",
    )

    pending_types = {_item_type(params, i) for i in issues if i.get("number") is None}
    if need_parent and parent is None:
        pending_types.add(_dominant_type(params, issues))
    for type_code in sorted(pending_types):
        color, desc = _TYPE_LABELS.get(type_code, ("ededed", ""))
        ctx.github.ensure_label(token, owner, repo, type_code, color=color, description=desc)

    # 1) parent first, so children can be attached to it
    if need_parent and parent is None:
        dominant = _dominant_type(params, issues)
        parent_t = _parent_title(params, dominant)
        created = _create_issue_with_assignee(
            ctx,
            token, owner, repo,
            title=parent_t,
            body=_format_parent_body(params, marker, parent_marker),
            labels=[_TICKET_ISSUE_LABEL, dominant],
            assignees=[params.github_username] if params.github_username else None,
        )
        parent = {"number": created["number"], "id": created["id"],
                  "url": created["url"], "title": parent_t, "state": "open",
                  "node_id": created.get("node_id")}
        writers.set_ticket_issue_parent(ctx.db, params.run_id, parent)
        log.info("ticket_issue_parent_created", issue=created["number"])

    # 2) children
    for idx, item in enumerate(issues):
        if item.get("number") is not None:
            continue
        # The full GitHub title (ticket id + order) is what gets stored and
        # sent to Odoo, so all surfaces show the same name as GitHub.
        issue_type = _item_type(params, item)
        title = _issue_title(params, idx + 1, len(issues), item["title"], issue_type)
        created = _create_issue_with_assignee(
            ctx,
            token, owner, repo,
            title=title,
            body=_format_issue_body(item, params, marker),
            labels=[_TICKET_ISSUE_LABEL, issue_type],
            assignees=[params.github_username] if params.github_username else None,
        )
        # Keep only what resume, Contract 2, and state tracking need; the body
        # lives on GitHub now, and dropping it keeps Claude-rendered customer
        # text out of the retained JSON (the retention purge keeps `issues`).
        issues[idx] = {
            "title": title,
            "type": issue_type,
            "number": created["number"],
            "id": created["id"],
            "url": created["url"],
            "state": "open",
            "attached": False,
            # Per-issue plan_date echo (spec decision 6); None when unset.
            "plan_date": params.plan_date.isoformat() if params.plan_date else None,
            # GraphQL id for Projects v2; internal only (never sent to Odoo).
            "node_id": created.get("node_id"),
        }
        writers.update_ticket_issue_progress(ctx.db, params.run_id, issues)
        log.info("ticket_issue_created", issue=created["number"], title=title)

    # 3) attach each child to the parent (idempotent; 422 swallowed in client)
    if need_parent and parent is not None:
        for idx, item in enumerate(issues):
            if item.get("attached") or item.get("id") is None:
                continue
            ctx.github.add_sub_issue(token, owner, repo, parent["number"], item["id"])
            issues[idx] = {**item, "attached": True}
            writers.update_ticket_issue_progress(ctx.db, params.run_id, issues)
            log.info("ticket_issue_attached", issue=item["number"], parent=parent["number"])

    # 4) board projection (fail-soft; spec 2026-07-09)
    if params.github_project_url:
        _project_step(ctx, token, owner, repo, params, issues, parent, log)

    return issues


def _create_issue_with_assignee(
    ctx,
    token: str,
    owner: str,
    repo: str,
    title: str,
    body: str,
    labels: list[str],
    assignees: list[str] | None,
) -> dict:
    try:
        return ctx.github.create_issue(
            token, owner, repo, title=title, body=body,
            labels=labels, assignees=assignees,
        )
    except PermanentError as exc:
        if assignees and "422" in str(exc):
            writers.record_ops_event(
                ctx.db,
                "github",
                "warning",
                "assignee_rejected",
                {"username": assignees[0], "repo": f"{owner}/{repo}"},
            )
            return ctx.github.create_issue(
                token, owner, repo, title=title, body=body, labels=labels
            )
        raise
