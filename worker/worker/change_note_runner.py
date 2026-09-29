"""Merge change-note job."""

from __future__ import annotations

import structlog

from reva.change_note import build_note
from reva.db import writers
from reva.diff_utils import affected_modules, extract_file_paths, updated_submodules
from reva.errors import ProviderCreditExhausted, TransientError
from reva.ticket_links import (
    TicketRef,
    extract_ticket_id,
    parse_closing_refs,
    resolve_pr_tickets,
    resolve_ticket_by_id,
)
from worker.change_note_delivery import maybe_deliver_change_notes
from worker.repo_instance import UnknownRepoInstance, UnreadableRepoConfig, declared_instance_id
from worker.release_log_lookup import ReleaseLogLookupError, release_log_block
from worker.runner import budget_exceeded, build_odoo_client, defer_for_budget, get_context

logger = structlog.get_logger()


def _affected_modules(
    ctx, job_params: dict, owner: str, name: str, repo: str, pr_number: int
) -> tuple[list[str], list[str]] | None:
    """(modules, submodules) the PR touched, or None when the lookup failed
    (ops event recorded, the note is generated regardless). A renamed file
    counts for the module it left as well. TransientError -> RQ retry."""
    try:
        token = ctx.github.get_installation_token(job_params["installation_id"])
        files = ctx.github.get_changed_files(token, owner, name, pr_number)
        modules = affected_modules(
            path
            for item in files
            for path in (item.get("filename"), item.get("previous_filename"))
            if path
        )
        return modules, updated_submodules(files)
    except TransientError:
        raise
    except Exception as exc:  # noqa: BLE001 — degrade, stay visible
        logger.warning("change_note_modules_lookup_failed", repo=repo, pr=pr_number, exc_info=True)
        writers.record_ops_event(
            ctx.db, "change_note", "warning", "modules_lookup_failed",
            {"repo": repo, "pr": pr_number, "error": str(exc)[:300]},
        )
        return None


def _fallback_tickets(ctx, repo: str, pr_number: int, job_params: dict) -> list[TicketRef]:
    """The ticket the PR names through its branch or title, for a PR whose
    closing refs resolve to no REVA ticket. Same ladder as the work-status
    fallback in board_status_runner."""
    extracted = extract_ticket_id(job_params.get("head_ref"), job_params.get("pr_title"))
    if extracted is None:
        return []
    ticket_id, model_hint, strict_model = extracted
    try:
        instance_id = declared_instance_id(ctx, repo, job_params["installation_id"], logger)
    except UnknownRepoInstance as exc:
        logger.warning("change_note_unknown_repo_instance", ticket_id=ticket_id, instance=exc.args[0])
        writers.record_ops_event(
            ctx.db, "odoo_callback", "warning", "unknown_repo_instance",
            {"repo": repo, "pr": pr_number, "ticket_id": ticket_id, "instance": exc.args[0]},
        )
        return []
    except UnreadableRepoConfig:
        return []  # the helper recorded the ops event
    resolved = resolve_ticket_by_id(
        ctx.db, repo, ticket_id, model_hint, strict_model=strict_model, instance_id=instance_id
    )
    if resolved is None:
        logger.warning("change_note_no_default_instance", ticket_id=ticket_id)
        writers.record_ops_event(
            ctx.db, "odoo_callback", "warning", "no_default_instance",
            {"repo": repo, "pr": pr_number, "ticket_id": ticket_id},
        )
        return []
    return [TicketRef(
        odoo_instance_id=resolved[0], ticket_id=ticket_id, model_name=resolved[1], run_id=None
    )]


def run_change_note(job_params: dict) -> dict:
    ctx = get_context()
    repo = job_params["repo_full_name"].lower()
    pr_number = job_params["pr_number"]
    refs = parse_closing_refs(job_params.get("pr_body"))
    tickets = resolve_pr_tickets(ctx.db, repo, refs)
    if not tickets:
        tickets = _fallback_tickets(ctx, repo, pr_number, job_params)
    if not tickets:
        return {"status": "no_tickets"}

    owner, name = repo.split("/", 1)
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
        and not writers.has_change_notes_for_pr(ctx.db, repo, pr_number)
    ):
        # Branch-linked PR that touches no addon and moves no submodule: there
        # is nothing to deploy, so no draft and no chatter note. A run that
        # already wrote a row must finish it, so the exit is for a first run only.
        logger.info("change_note_nothing_to_deploy", repo=repo, pr=pr_number)
        return {"status": "nothing_to_deploy"}
    pr = {
        "number": pr_number,
        "title": job_params.get("pr_title", ""),
        "url": job_params.get("pr_url", ""),
        "repo": repo,
        "body": job_params.get("pr_body", ""),
    }
    delivered = 0
    for ref in tickets:
        note_id, row = writers.get_or_create_change_note(
            ctx.db, repo, pr_number, ref.ticket_id, ref.odoo_instance_id, ref.model_name,
            pr_title=pr["title"], pr_url=pr["url"],
        )
        if touched is not None and row["modules"] is None:
            writers.record_change_note_modules(ctx.db, note_id, *touched)
        odoo = build_odoo_client(ctx, ref.odoo_instance_id)
        if not (row["status"] == "completed" and row["note_html"] is not None):
            # The ticket's own release-log entry beats a drafted note: zero cost,
            # written by the developer, re-read at delivery time.
            try:
                block = release_log_block(ctx, repo, ref.ticket_id, logger)  # TransientError -> RQ retry
            except ReleaseLogLookupError:
                block = None  # Claude path, as for an uncovered ticket (ops event recorded by the lookup)
            if block is not None:
                writers.record_change_note_completed(ctx.db, note_id, "", 0.0, source="release-log")
                if maybe_deliver_change_notes(
                    ctx, odoo, ref.odoo_instance_id, ref.ticket_id, ref.model_name, logger
                ):
                    delivered += 1
                continue
            spent = budget_exceeded(ctx)
            if spent is not None:
                waiting = defer_for_budget(
                    ctx, "worker.change_note_tasks.run_change_note", job_params,
                    kind="change_note", spent=spent, log=logger,
                )
                if waiting is not None:
                    # Notes stay pending; delivery converges once the deferred
                    # run has drafted them.
                    return waiting
                writers.record_change_note_failed(
                    ctx.db, note_id, "skipped_budget", f"budget reached (~${spent:.0f})"
                )
                # A skipped note is terminal — it never blocks delivery of the
                # ticket's other notes, so still test the convergent condition.
                if maybe_deliver_change_notes(
                    ctx, odoo, ref.odoo_instance_id, ref.ticket_id, ref.model_name, logger
                ):
                    delivered += 1
                continue
            ticket_name = (
                (writers.get_ticket_issue_run(ctx.db, ref.run_id) or {}).get("name", "")
                if ref.run_id is not None
                else writers.get_ticket_name(
                    ctx.db, ref.odoo_instance_id, ref.ticket_id, ref.model_name
                )
            )
            try:
                token = ctx.github.get_installation_token(job_params["installation_id"])
                diff = ctx.github.get_pull_request_diff(token, owner, name, pr_number)
                note_html, cost = build_note(
                    ctx.claude,
                    ctx.prompts_dir,
                    ticket_name,
                    pr,
                    diff,
                    sorted(extract_file_paths(diff)),
                )
            except TransientError:
                raise
            except ProviderCreditExhausted as exc:
                logger.error("provider_credit_exhausted", error=str(exc))
                waiting = defer_for_budget(
                    ctx, "worker.change_note_tasks.run_change_note", job_params,
                    kind="change_note", spent=0.0, log=logger, reason="provider_credit",
                )
                if waiting is not None:
                    # Notes stay pending; delivery converges once the deferred
                    # run has drafted them.
                    return waiting
                writers.record_change_note_failed(
                    ctx.db, note_id, "skipped_budget", "Anthropic credit balance too low"
                )
                # A skipped note is terminal — it never blocks delivery of the
                # ticket's other notes, so still test the convergent condition.
                if maybe_deliver_change_notes(
                    ctx, odoo, ref.odoo_instance_id, ref.ticket_id, ref.model_name, logger
                ):
                    delivered += 1
                continue
            except Exception as exc:
                writers.record_change_note_failed(ctx.db, note_id, "failed", str(exc))
                writers.record_ops_event(
                    ctx.db,
                    "change_note",
                    "error",
                    "build_failed",
                    {"repo": repo, "pr": pr_number, "error": str(exc)[:300]},
                )
                if maybe_deliver_change_notes(
                    ctx, odoo, ref.odoo_instance_id, ref.ticket_id, ref.model_name, logger
                ):
                    delivered += 1
                continue
            writers.record_change_note_completed(ctx.db, note_id, note_html, cost)
            writers.record_claude_spend(ctx.db, "change_note", cost)
        # Generation stays at merge; delivery waits for the convergent condition
        # (ticket ready AND every note terminal). Delivery of the whole batch is
        # deferred to maybe_deliver_change_notes — no per-PR change_note here.
        if maybe_deliver_change_notes(
            ctx, odoo, ref.odoo_instance_id, ref.ticket_id, ref.model_name, logger
        ):
            delivered += 1
    return {"status": "completed", "delivered": delivered}


def run_change_note_delivery(job_params: dict) -> dict:
    """Deliver a ticket's completed notes if nothing blocks them any more.
    Enqueued by the scheduler after it reaps a stuck pending note."""
    ctx = get_context()
    odoo_instance_id = job_params["odoo_instance_id"]
    ticket_id = job_params["ticket_id"]
    model_name = job_params["model_name"]
    odoo = build_odoo_client(ctx, odoo_instance_id)
    delivered = maybe_deliver_change_notes(ctx, odoo, odoo_instance_id, ticket_id, model_name)
    return {"status": "completed", "delivered": 1 if delivered else 0}
