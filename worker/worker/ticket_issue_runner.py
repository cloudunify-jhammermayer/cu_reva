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
from collections import Counter

import structlog
from rq import get_current_job

from reva.db import writers
from reva.errors import PermanentError, TransientError
from reva.github_urls import parse_github_repo_url
from reva.types import TicketIssueJobParams
from worker.runner import build_odoo_client, get_context

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
_TLDR_MAX = 30


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
    lines = [item.get("body", "")]
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

    log = logger.bind(owner=owner, repo=repo, number=number, state=state)

    affected = writers.update_ticket_issue_state(ctx.db, owner, repo, number, state)
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
        need_parent = parent is not None or any(
            i.get("number") is None for i in issues
        )
        done = all(i.get("number") is not None for i in issues)
        if need_parent:
            done = done and parent is not None and all(i.get("attached") for i in issues)
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
                          "title": h["title"], "state": h.get("state", "open")}
                writers.set_ticket_issue_parent(ctx.db, params.run_id, parent)
            issues = [dict(c) for c in children]
            writers.update_ticket_issue_progress(ctx.db, params.run_id, issues)
            log.info("ticket_issues_reconciled", existing=len(existing), children=len(children))
        else:
            response, plan = ctx.ticket_issue_planner.plan_with_response(params)
            issues = [
                {
                    "title": item.title,
                    "body": item.body,
                    "acceptance_criteria": item.acceptance_criteria,
                    "type": params.issue_type or item.type,
                    "number": None,
                    "url": None,
                    "state": None,
                    "id": None,
                    "attached": False,
                }
                for item in plan.issues
            ]
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
    need_parent = parent is not None or any(
        i.get("number") is None for i in issues
    )

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
        created = ctx.github.create_issue(
            token, owner, repo,
            title=parent_t,
            body=_format_parent_body(params, marker, parent_marker),
            labels=[_TICKET_ISSUE_LABEL, dominant],
        )
        parent = {"number": created["number"], "id": created["id"],
                  "url": created["url"], "title": parent_t, "state": "open"}
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
        created = ctx.github.create_issue(
            token, owner, repo,
            title=title,
            body=_format_issue_body(item, params, marker),
            labels=[_TICKET_ISSUE_LABEL, issue_type],
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

    return issues
