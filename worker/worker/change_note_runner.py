"""Merge change-note job."""

from __future__ import annotations

import structlog

from reva.change_note import build_note
from reva.db import writers
from reva.diff_utils import extract_file_paths
from reva.errors import TransientError
from reva.ticket_links import parse_closing_refs, resolve_pr_tickets
from worker.change_note_delivery import maybe_deliver_change_notes
from worker.release_log_lookup import ReleaseLogLookupError, release_log_block
from worker.runner import budget_exceeded, build_odoo_client, get_context

logger = structlog.get_logger()


def run_change_note(job_params: dict) -> dict:
    ctx = get_context()
    repo = job_params["repo_full_name"].lower()
    pr_number = job_params["pr_number"]
    refs = parse_closing_refs(job_params.get("pr_body"))
    tickets = resolve_pr_tickets(ctx.db, repo, refs)
    if not tickets:
        return {"status": "no_tickets"}

    owner, name = repo.split("/", 1)
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
            run_row = writers.get_ticket_issue_run(ctx.db, ref.run_id) or {}
            try:
                token = ctx.github.get_installation_token(job_params["installation_id"])
                diff = ctx.github.get_pull_request_diff(token, owner, name, pr_number)
                note_html, cost = build_note(
                    ctx.claude,
                    ctx.prompts_dir,
                    run_row.get("name", ""),
                    pr,
                    diff,
                    sorted(extract_file_paths(diff)),
                )
            except TransientError:
                raise
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
