"""Ticket analysis job orchestration.

run_ticket_analysis is what RQ calls for each enqueued ticket analysis job.
"""

from __future__ import annotations

import structlog

from reva.db import writers
from reva.errors import MalformedModelOutput, PermanentError, TransientError
from reva.html_guard import ensure_renderable
from reva.ticket_formatter import format_ticket_html
from reva.ticket_knowledge import build_knowledge_block
from reva.types import TicketJobParams
from worker.runner import build_odoo_client, get_context, instance_budget_exceeded

logger = structlog.get_logger()


def instance_odoo_version(ctx, odoo_instance_id: int) -> str | None:
    row = writers.get_odoo_instance(ctx.db, odoo_instance_id)
    return row.get("odoo_version") if row else None


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
        odoo.reset_status(
            ticket_id=params.ticket_id,
            model_name=params.model_name,
            analysis_id=params.analysis_id,
        )
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
            extra_blocks = None
            if ctx.core_knowledge is not None:
                version = ctx.core_knowledge.resolve(
                    instance_odoo_version(ctx, params.odoo_instance_id)
                )
                if version is None:
                    log.warning("ticket_core_knowledge_unavailable")
                    writers.record_ops_event(
                        ctx.db,
                        "core_knowledge",
                        "warning",
                        "ticket_version_unavailable",
                        {
                            "analysis_id": params.analysis_id,
                            "odoo_instance_id": params.odoo_instance_id,
                        },
                    )
                else:
                    block, planner_cost, error = build_knowledge_block(
                        ctx.claude,
                        ctx.core_knowledge,
                        ctx.prompts_dir,
                        version,
                        params.text,
                    )
                    if planner_cost:
                        writers.record_claude_spend(ctx.db, "ticket_planner", planner_cost)
                    if error is not None:
                        writers.record_ops_event(
                            ctx.db,
                            "ticket_planner",
                            "warning",
                            "planner_failed",
                            {"analysis_id": params.analysis_id, "error": error[:300]},
                        )
                    elif block is not None:
                        extra_blocks = [block]
            try:
                response_obj, result = ctx.ticket_analyzer.analyze_with_response(
                    params,
                    extra_system_blocks=extra_blocks,
                )
            except MalformedModelOutput as exc:
                # Truncated/schema-invalid tool call — usually a one-off
                # formatting hiccup, not a doomed input. One paid retry before
                # the consultant sees a failure; a second miss falls through to
                # the PermanentError handler below.
                log.warning("ticket_analysis_malformed_output_retry", error=str(exc))
                writers.record_ops_event(
                    ctx.db,
                    "ticket_analysis",
                    "warning",
                    "malformed_output_retried",
                    {"analysis_id": params.analysis_id, "error": str(exc)[:300]},
                )
                response_obj, result = ctx.ticket_analyzer.analyze_with_response(
                    params,
                    extra_system_blocks=extra_blocks,
                )
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
        writers.record_ticket_analysis_completed(
            ctx.db,
            params.analysis_id,
            html,
            response_obj,
            result_structured=result.model_dump(mode="json"),
        )

    # Well-formedness guard right before the callback: a malformed render must
    # not fail the job — repair it, deliver it, and record an ops event
    # (degradations must be visible, never silent).
    html, was_repaired = ensure_renderable(html)
    if was_repaired:
        log.warning("ticket_analysis_html_repaired")
        writers.record_ops_event(ctx.db, "ticket_analysis", "warning", "html_repaired", {
            "analysis_id": params.analysis_id,
            "ticket_id": params.ticket_id,
        })

    try:
        odoo.write_field(
            ticket_id=params.ticket_id,
            model_name=params.model_name,
            field_name=params.field_name,
            html=html,
        )
    except (PermanentError, TransientError) as exc:
        # DB row is already completed; record the delivery failure (so the tab
        # shows "not in Odoo") in addition to the ops event, then let RQ handle
        # retry/failure.
        log.warning("ticket_analysis_odoo_callback_error", exc_info=True)
        writers.record_ticket_analysis_callback_failed(ctx.db, params.analysis_id, str(exc))
        writers.record_ops_event(ctx.db, "odoo_callback", "error", "write_field_failed", {
            "analysis_id": params.analysis_id,
            "ticket_id": params.ticket_id,
        })
        raise

    writers.record_ticket_analysis_callback_sent(ctx.db, params.analysis_id)
    log.info("ticket_analysis_done")
    return {"status": "completed", "analysis_id": params.analysis_id}
