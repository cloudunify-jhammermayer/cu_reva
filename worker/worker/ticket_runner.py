"""Ticket analysis job orchestration.

run_ticket_analysis is what RQ calls for each enqueued ticket analysis job.
"""

from __future__ import annotations

import structlog
from pydantic import ValidationError

from reva import config
from reva.db import writers
from reva.errors import MalformedModelOutput, PermanentError, TransientError
from reva.github_urls import parse_github_repo_url
from reva.golden_estimates import apply_anchor, calibration_block, load
from reva.html_guard import ensure_renderable
from reva.ticket_formatter import format_ticket_html
from reva.ticket_knowledge import build_ticket_knowledge, core_source_param
from reva.types import TicketJobParams
from worker.golden_support import record_degradations
from worker.repo_config import (
    code_grounding_allowed,
    load_repo_config,
    resolve_repo_context,
)
from worker.runner import build_odoo_client, get_context, instance_budget_exceeded

logger = structlog.get_logger()


def _prompts_dir(ctx) -> str:
    """The prompts directory the analyzer and CLI runner both read from.

    `WorkerContext.prompts_dir` is already how this module reaches it
    (`build_ticket_knowledge` below); no separate/hardcoded source exists.
    """
    return ctx.prompts_dir


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


_TICKET_SKILL = "reva-ticket-analysis"


def _record_golden_degradations(ctx, log, degradations, analysis_id: int) -> None:
    """Fix this module's component and detail key for record_degradations."""
    record_degradations(
        ctx.db, log, "ticket_analysis", degradations, {"analysis_id": analysis_id}
    )


def _try_code_grounded_analysis(ctx, params, knowledge, log, version=None):
    """Run the analysis against the repo clone when the planner asked for code.

    Returns ``(response, result)`` on success, or ``(None, None)`` to fall back
    to the Messages API path. Every fallback reason is recorded — a silently
    ungrounded analysis is indistinguishable from a well-grounded one.

    A busy repo lock raises TransientError on purpose: RQ retries the whole
    job rather than quietly downgrading a ticket the planner said needs code.
    """
    from reva.types import TicketAnalysisResult

    repo = resolve_repo_context(ctx.github, params.github_url, log)
    if repo is None:
        # No URL, unparseable, or the App isn't installed on it. Project-less
        # tickets land here too, which is correct: there is nothing to read.
        if params.github_url:
            log.warning("ticket_code_grounding_unavailable", github_url=params.github_url)
            writers.record_ops_event(
                ctx.db, "ticket_analysis", "warning", "code_grounding_unavailable",
                {"analysis_id": params.analysis_id, "github_url": params.github_url},
            )
        return None, None

    owner, name, token, repo_config = repo
    if not code_grounding_allowed(repo_config):
        log.info("ticket_code_grounding_disabled", repo=f"{owner}/{name}")
        writers.record_ops_event(
            ctx.db, "ticket_analysis", "info", "code_grounding_disabled",
            {"analysis_id": params.analysis_id, "repo": f"{owner}/{name}"},
        )
        return None, None

    skill_params = {"ticket_text": params.text}
    if knowledge.blocks:
        skill_params["retrieved_knowledge"] = "\n".join(
            block.get("text", "") for block in knowledge.blocks
        )
    extra_dirs = None
    core_source = core_source_param(ctx.core_knowledge, version)
    if core_source is not None:
        extra_dirs, skill_params["core_knowledge"] = core_source

    block, golden_degradations = calibration_block(
        _prompts_dir(ctx),
        limit=config.GOLDEN_ESTIMATE_LIMIT,
        enabled=config.GOLDEN_ESTIMATES,
    )
    _record_golden_degradations(ctx, log, golden_degradations, params.analysis_id)

    with ctx.runner.repo_lock(owner, name):
        repo_path = ctx.runner.ensure_repo(owner, name, None, token)
        response = ctx.runner.review(
            repo_path=repo_path, skill=_TICKET_SKILL, params=skill_params,
            skill_vars={"ESTIMATE_CALIBRATION": block},
            odoo=repo_config.odoo, extra_dirs=extra_dirs,
        )
    if response.tool_use_input is None:
        raise PermanentError(f"{_TICKET_SKILL} produced no analysis JSON")
    try:
        return response, TicketAnalysisResult.model_validate(response.tool_use_input)
    except ValidationError as exc:
        # No tool schema constrains this path — the skill file is the whole
        # contract, and the model can still drift off it (analyses 77/78,
        # 2026-07-27: `missing_info[].question`, `confidence: "high"`). Re-running
        # the CLI would re-pay 10-30x and re-take the lock, so fall back to the
        # docs-only leg: a downgraded analysis beats none. The CLI run was
        # already paid for, so its spend goes to the ledger the budget cap reads
        # even though the response is discarded.
        log.warning("ticket_code_grounding_malformed_output", error=str(exc))
        writers.record_claude_spend(
            ctx.db, "ticket_analysis_grounding", response.total_cost_usd
        )
        writers.record_ops_event(
            ctx.db, "ticket_analysis", "warning", "code_grounding_malformed_output",
            {"analysis_id": params.analysis_id, "repo": f"{owner}/{name}",
             "error": str(exc)[:300]},
        )
        return None, None


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
            response_obj = result = None
            if knowledge.needs_repo_code:
                response_obj, result = _try_code_grounded_analysis(
                    ctx, params, knowledge, log, version
                )
            if response_obj is None:
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
                # Only this leg populates last_golden_degradations (set inside
                # TicketAnalyzer._build_system, reached only via
                # analyze_with_response). Read it here, right after the call
                # that set it — not at the convergence point below, which also
                # runs for the CLI leg, where this attribute would still hold
                # whatever a PREVIOUS job left there (workers are long-lived RQ
                # processes) and get recorded under the wrong analysis_id.
                _record_golden_degradations(
                    ctx, log,
                    getattr(ctx.ticket_analyzer, "last_golden_degradations", []),
                    params.analysis_id,
                )

            # Both legs converge here. Resolve each cited anchor and derive its
            # confidence in code — the model's own value is never trusted.
            # load()'s degradations are NOT recorded here: they were already
            # recorded upstream by whichever leg actually ran this job —
            # calibration_block() inside _try_code_grounded_analysis (CLI leg)
            # or last_golden_degradations just above (Messages-API leg), both of
            # which call the same load() internally. Recording them again here
            # would double-count every load()-level degradation (file_missing,
            # bands_invalid, anchor_invalid, anchor_hours_mismatch).
            golden, _ = load(_prompts_dir(ctx))
            for estimate in result.estimates:
                _record_golden_degradations(
                    ctx, log,
                    apply_anchor(estimate, golden, score_confidence=True),
                    params.analysis_id,
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
            analysis_id=params.analysis_id,
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
