"""Timesheet wording review job orchestration."""

from __future__ import annotations

import structlog

from reva.db import writers
from reva.errors import PermanentError, TransientError
from reva.types import (
    TIMESHEET_CHUNK_SIZE,
    TimesheetJobParams,
    TimesheetLine,
    TimesheetLineResult,
)
from worker.runner import build_odoo_client, get_context, instance_budget_exceeded

logger = structlog.get_logger()


def _chunks(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def _stats(row: dict) -> dict:
    return {
        "total": row["total_lines"],
        "ok": row["ok_count"],
        "rewritten": row["rewritten_count"],
        "needs_human": row["needs_human_count"],
    }


def _send_callback(ctx, params: TimesheetJobParams, row: dict, log) -> None:
    if row["callback_sent_at"] is not None:
        return
    payload = row["callback_payload"] or {"results": []}
    odoo = build_odoo_client(ctx, params.odoo_instance_id)
    try:
        odoo.timesheet_results(
            request_id=params.request_id,
            results=payload.get("results", []),
            stats=_stats(row),
        )
    except PermanentError as exc:
        log.warning("timesheet_review_odoo_callback_permanent", exc_info=True)
        writers.record_timesheet_run_failed(
            ctx.db, params.run_id, f"odoo callback rejected: {exc}"
        )
        writers.record_ops_event(ctx.db, "odoo_callback", "error", "timesheet_results_failed", {
            "run_id": params.run_id,
            "request_id": params.request_id,
        })
        raise
    except TransientError:
        log.warning("timesheet_review_odoo_callback_error", exc_info=True)
        writers.record_ops_event(ctx.db, "odoo_callback", "error", "timesheet_results_failed", {
            "run_id": params.run_id,
            "request_id": params.request_id,
        })
        raise
    writers.record_timesheet_callback_sent(ctx.db, params.run_id)


def run_timesheet_review(job_params: dict) -> dict:
    """RQ task entry point for timesheet wording review."""
    ctx = get_context()
    params = TimesheetJobParams.model_validate(job_params)
    if ctx.timesheet_analyzer is None:
        raise PermanentError("TimesheetAnalyzer is not configured")

    log = logger.bind(
        run_id=params.run_id,
        request_id=params.request_id,
        odoo_instance_id=params.odoo_instance_id,
    )
    log.info("timesheet_review_start", lines=len(params.lines))

    row = writers.get_timesheet_run(ctx.db, params.run_id)
    if row is None:
        raise PermanentError(f"timesheet review run {params.run_id} not found")

    if row["status"] == "completed":
        _send_callback(ctx, params, row, log)
        log.info("timesheet_review_resume_completed")
        return {"status": "completed", "run_id": params.run_id}
    if row["status"] == "failed":
        raise PermanentError(row["error_message"] or "timesheet review already failed")

    processed = writers.get_timesheet_line_ids(ctx.db, params.run_id)
    remaining = [line for line in params.lines if line.line_id not in processed]
    if remaining:
        spent = instance_budget_exceeded(ctx, params.odoo_instance_id)
        if spent is not None:
            error = (
                f"Odoo instance daily budget reached (~${spent:.2f} in 24h); "
                "timesheet review declined."
            )
            log.warning("timesheet_review_instance_over_budget", spent_usd=round(spent, 2))
            writers.record_timesheet_run_failed(ctx.db, params.run_id, error)
            raise PermanentError(error)

    for chunk in _chunks(remaining, TIMESHEET_CHUNK_SIZE):
        try:
            results, responses = _process_chunk(
                ctx.timesheet_analyzer,
                chunk,
                params.flagged_words,
                log,
            )
            writers.record_timesheet_chunk(ctx.db, params.run_id, results, responses)
        except TransientError:
            log.warning("timesheet_review_transient_error", exc_info=True)
            raise
        except PermanentError as exc:
            log.error("timesheet_review_permanent_error", error=str(exc))
            writers.record_timesheet_run_failed(ctx.db, params.run_id, str(exc))
            raise
        except Exception as exc:
            log.exception("timesheet_review_unexpected_error")
            writers.record_timesheet_run_failed(ctx.db, params.run_id, str(exc))
            raise PermanentError(str(exc)) from exc

    writers.record_timesheet_run_completed(ctx.db, params.run_id)
    row = writers.get_timesheet_run(ctx.db, params.run_id)
    if row is None:
        raise PermanentError(f"timesheet review run {params.run_id} disappeared")
    _send_callback(ctx, params, row, log)

    log.info("timesheet_review_done")
    return {"status": "completed", "run_id": params.run_id}


def _process_chunk(
    analyzer,
    chunk: list[TimesheetLine],
    flagged_words: list[str],
    log,
) -> tuple[list[TimesheetLineResult], list]:
    """One Claude call plus one coverage retry for missing line IDs."""
    response, results = analyzer.analyze_chunk(chunk, flagged_words=flagged_words)
    responses = [response]
    expected = {line.line_id for line in chunk}
    by_id = {result.line_id: result for result in results if result.line_id in expected}
    extra = {result.line_id for result in results} - expected
    if extra:
        log.warning("timesheet_review_unknown_line_ids", extra=sorted(extra))

    missing = expected - set(by_id)
    if missing:
        log.warning("timesheet_review_coverage_retry", missing=sorted(missing))
        retry_lines = [line for line in chunk if line.line_id in missing]
        retry_response, retry_results = analyzer.analyze_chunk(
            retry_lines,
            flagged_words=flagged_words,
        )
        responses.append(retry_response)
        for result in retry_results:
            if result.line_id in missing:
                by_id[result.line_id] = result

    final: list[TimesheetLineResult] = []
    originals = {line.line_id: line.description for line in chunk}
    for line in chunk:
        result = by_id.get(line.line_id)
        if result is None:
            result = TimesheetLineResult(
                line_id=line.line_id,
                status="needs_human",
                reason="no result returned",
            )
        elif (
            result.status == "rewritten"
            and (result.updated_desc or "").strip() == originals[line.line_id].strip()
        ):
            result = TimesheetLineResult(line_id=line.line_id, status="ok")
        final.append(result)
    return final, responses
