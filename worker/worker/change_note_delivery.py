"""Batched change-note delivery on the ticket-ready convergence (spec 2026-07-11).

Change notes are still generated per merged PR (change_note_runner), but delivery
is deferred until BOTH conditions hold:

  1. the ticket is ready — its union of REVA-created issues is non-empty and all
     closed (same test the ready sender uses in ticket_issue_runner), and
  2. every change_notes row for the ticket is terminal — no note is still
     generating (status 'pending').

Whichever event converges last (the ready transition, or the last note finishing)
ships ONE consolidated tickets.change-summary callback. This resolves the race
where the merge that closes the final issue fires the ready signal while that
same PR's note is still being generated in a parallel job: the ready flag is
never delayed, and the summary follows as soon as the last note lands.
"""

from __future__ import annotations

import structlog

from reva.db import writers
from reva.errors import PermanentError, TransientError
from reva.odoo_client import OdooCallbackClient

logger = structlog.get_logger()


def maybe_deliver_change_notes(
    ctx,
    odoo: OdooCallbackClient,
    odoo_instance_id: int,
    ticket_id: int,
    model_name: str,
    log=None,
) -> bool:
    """Deliver the consolidated summary iff the convergent condition holds.

    Returns True when a batch was sent (and its rows stamped delivered). A
    PermanentError from Odoo logs + records a change_summary_rejected ops event
    and leaves the rows undelivered (retried on the next ready/note event); a
    TransientError re-raises for RQ retry (idempotent — the same undelivered
    rows re-send, delivered_at is stamped only on a 200)."""
    log = log or logger
    union = writers.get_ticket_issue_union(ctx.db, odoo_instance_id, ticket_id, model_name)
    if not (union and all(item.get("state") == "closed" for item in union)):
        return False
    if writers.has_pending_change_notes(ctx.db, odoo_instance_id, ticket_id, model_name):
        return False
    notes = writers.get_undelivered_change_notes(ctx.db, odoo_instance_id, ticket_id, model_name)
    if not notes:
        return False

    payload = [
        {
            "pr": {
                "number": note["pr_number"],
                "title": note["pr_title"] or "",
                "url": note["pr_url"] or "",
                "repo": note["repo_full_name"],
            },
            "note_html": note["note_html"],
        }
        for note in notes
    ]
    try:
        odoo.change_summary(ticket_id=ticket_id, model_name=model_name, notes=payload)
    except TransientError:
        raise
    except PermanentError:
        log.warning("change_summary_rejected", ticket_id=ticket_id, exc_info=True)
        writers.record_ops_event(
            ctx.db,
            "odoo_callback",
            "warning",
            "change_summary_rejected",
            {"ticket_id": ticket_id},
        )
        return False
    writers.mark_change_notes_delivered(ctx.db, [note["id"] for note in notes])
    log.info("change_summary_delivered", ticket_id=ticket_id, notes=len(notes))
    return True
