"""Ticket analysis job orchestration.

run_ticket_analysis is what RQ calls for each enqueued ticket analysis job.
"""

from __future__ import annotations

import structlog

from reva.db import writers
from reva.errors import PermanentError, TransientError
from reva.ticket_formatter import format_ticket_html
from reva.types import TicketJobParams
from worker.runner import build_odoo_client, get_context, instance_budget_exceeded

logger = structlog.get_logger()


def run_ticket_analysis(job_params: dict) -> dict:
    """RQ task entry point for ticket analysis."""
    ctx = get_context()
    params = TicketJobParams.model_validate(job_params)
    odoo = build_odoo_client(ctx, params.odoo_instance_id)

    log = logger.bind(
        analysis_id=params.analysis_id,
        ticket_id=params.ticket_id,
        model_name=params.model_name,
        field_name=params.field_name,
    )
    log.info("ticket_analysis_start", text_len=len(params.text))

    # Reset Odoo status to pending so the UI shows work-in-progress (handles requeues of completed jobs).
    try:
        odoo.reset_status(ticket_id=params.ticket_id, model_name=params.model_name)
    except Exception:
        log.warning("ticket_analysis_odoo_reset_failed", exc_info=True)

    # Idempotent resume: an RQ retry after a transient *callback* failure finds
    # the analysis already completed. Re-analyzing would re-pay Claude, so reuse
    # the persisted HTML and go straight to the callback. A genuine requeue
    # resets the row to pending first, so it still re-analyzes.
    existing = writers.get_ticket_analysis(ctx.db, params.analysis_id)
    if existing is not None and existing["status"] == "completed" and existing["result_html"]:
        log.info("ticket_analysis_resume_completed")
        html = existing["result_html"]
    else:
        spent = instance_budget_exceeded(ctx, params.odoo_instance_id)
        if spent is not None:
            error = (
                f"Odoo instance daily budget reached (~${spent:.2f} in 24h); "
                f"analysis declined."
            )
            log.warning("ticket_analysis_instance_over_budget", spent_usd=round(spent, 2))
            writers.record_ticket_analysis_failed(ctx.db, params.analysis_id, error)
            raise PermanentError(error)
        try:
            response_obj, result = ctx.ticket_analyzer.analyze_with_response(params)
            html = format_ticket_html(result)
        except TransientError:
            log.warning("ticket_analysis_transient_error", exc_info=True)
            raise
        except PermanentError as exc:
            log.error("ticket_analysis_permanent_error", error=str(exc))
            writers.record_ticket_analysis_failed(ctx.db, params.analysis_id, str(exc))
            raise
        except Exception as exc:
            log.exception("ticket_analysis_unexpected_error")
            writers.record_ticket_analysis_failed(ctx.db, params.analysis_id, str(exc))
            raise PermanentError(str(exc)) from exc

        # Persist HTML before attempting Odoo callback so the result is never lost.
        writers.record_ticket_analysis_completed(ctx.db, params.analysis_id, html, response_obj)

    try:
        odoo.write_field(
            ticket_id=params.ticket_id,
            model_name=params.model_name,
            field_name=params.field_name,
            html=html,
        )
    except (PermanentError, TransientError):
        # DB row is already completed; log and let RQ handle retry/failure.
        log.warning("ticket_analysis_odoo_callback_error", exc_info=True)
        raise

    log.info("ticket_analysis_done")
    return {"status": "completed", "analysis_id": params.analysis_id}
