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

When any note in the batch has source "release-log", the ticket's release-log
entry is re-read here (rather than reused from generation time) so a later
edit to docs/releases/<name>.md is what ships, and sent once as the separate
`release_log` block alongside the per-PR notes, which then all carry an empty
`note_html` (the entry replaces every drafted note in the batch, not just its
own PR's). A permanent GitHub error during that re-read holds the whole batch
for the next ready/note event rather than shipping the PR links without it.
"""

from __future__ import annotations

import structlog

from reva.db import writers
from reva.errors import PermanentError, TransientError
from reva.odoo_client import OdooCallbackClient
from worker.release_log_lookup import ReleaseLogLookupError, release_log_block

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
    rows re-send, delivered_at is stamped only on a 200). A ReleaseLogLookupError
    while re-reading a release-log entry also returns False without sending —
    the ops event was already recorded by the lookup."""
    log = log or logger
    union = writers.get_ticket_issue_union(ctx.db, odoo_instance_id, ticket_id, model_name)
    if not (union and all(item.get("state") == "closed" for item in union)):
        return False
    if writers.has_pending_change_notes(ctx.db, odoo_instance_id, ticket_id, model_name):
        return False
    notes = writers.get_undelivered_change_notes(ctx.db, odoo_instance_id, ticket_id, model_name)
    if not notes:
        return False

    release_log = None
    release_log_repo = next(
        (note["repo_full_name"] for note in notes if note["source"] == "release-log"), None
    )
    if release_log_repo is not None:
        # Re-read at delivery so later PRs' edits to the entry are what ships.
        try:
            release_log = release_log_block(ctx, release_log_repo, ticket_id, log)
        except ReleaseLogLookupError:
            return False  # rows stay undelivered; the ops event was already recorded by the lookup
        if release_log is None:
            log.warning("release_log_entry_missing", ticket_id=ticket_id)
            writers.record_ops_event(
                ctx.db, "release_log", "warning", "release_log_entry_missing",
                {"ticket_id": ticket_id, "repo": release_log_repo},
            )

    payload = [
        {
            "pr": {
                "number": note["pr_number"],
                "title": note["pr_title"] or "",
                "url": note["pr_url"] or "",
                "repo": note["repo_full_name"],
            },
            "note_html": "" if release_log is not None else note["note_html"],
        }
        for note in notes
    ]
    try:
        odoo.change_summary(
            ticket_id=ticket_id, model_name=model_name, notes=payload, release_log=release_log
        )
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
