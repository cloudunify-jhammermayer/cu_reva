"""Ticket analysis job orchestration.

run_ticket_analysis is what RQ calls for each enqueued ticket analysis job.
"""

from __future__ import annotations

import structlog

from reva.db import writers
from reva.errors import MalformedModelOutput, PermanentError, TransientError
from reva.github_urls import parse_github_repo_url
from reva.html_guard import ensure_renderable
from reva.ticket_formatter import format_ticket_html
from reva.ticket_knowledge import build_ticket_knowledge
from reva.types import TicketJobParams
from worker.repo_config import load_repo_config
from worker.runner import build_odoo_client, get_context, instance_budget_exceeded

logger = structlog.get_logger()


def repo_core_version(ctx, github_url: str | None, analysis_id: int, log) -> str | None:
    """Resolve the Odoo /core knowledge version for a ticket from its TARGET
    repo's ``.claude-review.yml`` ``odoo_version`` — the version of the project
    the ticket affects, not the helpdesk instance that raised it. Mirrors the
    PR-review path (``reviewer.py``): a repo that requests no version is a
    silent skip; a ``version_unavailable`` ops event is recorded only on genuine
    config drift (the repo requests a version /core doesn't carry). Returns a
    provisioned version string, or None to ground without core knowledge.
    """
    if ctx.core_knowledge is None or not github_url:
        return None
    parsed = parse_github_repo_url(github_url)
    if parsed is None:
        return None
    owner, repo = parsed
    try:
        installation_id = ctx.github.get_repo_installation_id(owner, repo)
        token = ctx.github.get_installation_token(installation_id)
        default_branch = ctx.github.get_repo(token, owner, repo).get("default_branch") or "main"
        config = load_repo_config(ctx.github, token, owner, repo, default_branch)
    except Exception:
        log.warning("ticket_core_knowledge_config_failed", exc_info=True)
        return None
    if not config.odoo_version:
        return None
    version = ctx.core_knowledge.resolve(config.odoo_version)
    if version is None:
        # Config drift (repo requests a version /core doesn't carry): degrade
        # + record it, per the degradations-are-visible invariant.
        log.warning("core_knowledge_unavailable", version=config.odoo_version)
        writers.record_ops_event(
            ctx.db,
            "core_knowledge",
            "warning",
            "version_unavailable",
            {"repo": f"{owner}/{repo}", "requested": config.odoo_version,
             "analysis_id": analysis_id},
        )
    return version


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
            version = repo_core_version(ctx, params.github_url, params.analysis_id, log)
            knowledge = build_ticket_knowledge(
                ctx.claude,
                ctx.prompts_dir,
                params.text,
                core=ctx.core_knowledge,
                version=version,
                db=ctx.db,
                github=ctx.github,
                github_url=params.github_url,
            )
            if knowledge.planner_cost:
                writers.record_claude_spend(ctx.db, "ticket_planner", knowledge.planner_cost)
            if knowledge.planner_error is not None:
                writers.record_ops_event(
                    ctx.db,
                    "ticket_planner",
                    "warning",
                    "planner_failed",
                    {"analysis_id": params.analysis_id, "error": knowledge.planner_error[:300]},
                )
            if knowledge.repo_docs_error is not None:
                log.warning("ticket_repo_docs_failed", error=knowledge.repo_docs_error)
                writers.record_ops_event(
                    ctx.db,
                    "repo_docs",
                    "warning",
                    "retrieval_failed",
                    {
                        "analysis_id": params.analysis_id,
                        "github_url": params.github_url,
                        "error": knowledge.repo_docs_error[:300],
                    },
                )
            extra_blocks = knowledge.blocks or None
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
            repo_docs_sections_used=knowledge.repo_docs_sections,
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
