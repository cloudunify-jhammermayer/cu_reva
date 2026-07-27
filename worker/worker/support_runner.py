"""Support-answer job orchestration.

run_support_answer is what RQ calls for each enqueued support turn. Two
grounding depths, chosen by the planner (`TicketKnowledge.needs_repo_code`):

- **docs** — the Messages API path (`SupportAnswerer`) with the `/core` and
  repo-docs blocks. Cheap, prompt-cached across turns of a thread.
- **code** — one headless-CLI run of the `reva-support-answer` skill against
  the worker clone, under the per-repo lock. Roughly 10-30x the cost, so the
  gate stays shut on any doubt and every reason for shutting it is recorded.

The answer is written back as a DRAFT for a consultant (`write_field`), never
sent to the customer.
"""

from __future__ import annotations

import structlog

from reva.db import writers
from reva.errors import MalformedModelOutput, PermanentError, TransientError
from reva.github_urls import parse_github_repo_url
from reva.html_guard import ensure_renderable
from reva.persona import render_persona_block, resolve_persona
from reva.support_formatter import format_support_html
from reva.ticket_knowledge import build_ticket_knowledge
from reva.types import SupportAnswerResult, SupportJobParams
from worker.repo_config import code_grounding_allowed, resolve_repo_context
from worker.runner import (
    budget_exceeded,
    build_odoo_client,
    get_context,
    instance_budget_exceeded,
)

logger = structlog.get_logger()

_SUPPORT_SKILL = "reva-support-answer"


def _core_version(ctx, config) -> str | None:
    """The /core knowledge version this repo's answers are grounded against."""
    if ctx.core_knowledge is None or config is None or not config.odoo_version:
        return None
    return ctx.core_knowledge.resolve(config.odoo_version)


def _skill_params(params: SupportJobParams, persona_block: str,
                  prior_turns: list[dict], knowledge) -> dict:
    """Task parameters for the CLI path.

    `review()` nonce-fences every value, so public and internal chatter stay in
    separate fences here too — the never-quote rule lives in the skill and is
    only enforceable if the two are distinguishable.
    """
    def _render(entries) -> str:
        return "\n".join(
            f"[{e.posted_at.isoformat()}] {e.author} ({e.author_kind}): {e.body}"
            for e in entries
        )

    out = {
        "persona": persona_block,
        "subject": params.subject,
        "question": params.question,
    }
    public = [e for e in params.chatter if e.visibility == "public"]
    internal = [e for e in params.chatter if e.visibility == "internal"]
    if public:
        out["public_chatter"] = _render(public)
    if internal:
        out["internal_notes"] = _render(internal)
    if prior_turns:
        out["prior_turns"] = "\n".join(
            f"Q: {t.get('question', '')}\n"
            f"A: {(t.get('result_structured') or {}).get('answer', '')}"
            for t in prior_turns
        )
    for block in knowledge.blocks:
        # The retrieved docs ride along as plain task parameters; the CLI path
        # has no system-block channel.
        out.setdefault("retrieved_knowledge", "")
        out["retrieved_knowledge"] += block.get("text", "") + "\n"
    return out


def run_support_answer(job_params: dict) -> dict:
    """RQ task entry point for one support turn."""
    ctx = get_context()
    params = SupportJobParams.model_validate(job_params)
    odoo = build_odoo_client(ctx, params.odoo_instance_id)

    log = logger.bind(
        turn_id=params.turn_id,
        thread_id=params.thread_id,
        ticket_id=params.ticket_id,
        model_name=params.model_name,
    )
    log.info("support_answer_start", question_len=len(params.question))

    existing = writers.get_support_turn(ctx.db, params.turn_id)
    if existing is not None and existing["status"] == "completed" and existing["answer_html"]:
        # Idempotent resume: an RQ retry after a transient CALLBACK failure
        # must not re-pay Claude. A genuine requeue resets the row to pending.
        log.info("support_answer_resume_completed")
        html = existing["answer_html"]
    else:
        try:
            html = _produce_answer(ctx, params, odoo, log)
        except TransientError:
            # RQ will retry; the turn stays pending on purpose so the retry
            # resumes it rather than the dedup treating it as finished.
            log.warning("support_answer_transient_error", exc_info=True)
            raise
        except Exception as exc:
            # ANY terminal failure must mark the row, or the turn sits in
            # `pending` forever and the one-pending-turn dedup rejects every
            # future request for this record — the feature wedges itself on a
            # single bad run. This is how a malformed tool schema took the
            # record out of service instead of just failing one answer.
            log.exception("support_answer_failed")
            writers.record_support_turn_failed(ctx.db, params.turn_id, str(exc))
            writers.record_ops_event(
                ctx.db, "support_answer", "error", "answer_failed",
                {"turn_id": params.turn_id, "error": str(exc)[:300]},
            )
            raise

    html, was_repaired = ensure_renderable(html)
    if was_repaired:
        log.warning("support_answer_html_repaired")
        writers.record_ops_event(ctx.db, "support_answer", "warning", "html_repaired", {
            "turn_id": params.turn_id, "ticket_id": params.ticket_id,
        })

    # Read the metadata back off the persisted turn rather than threading it out
    # of _produce_answer: the resume branch above never calls that function, and
    # both branches must deliver the same three values.
    row = writers.get_support_turn(ctx.db, params.turn_id) or {}
    structured = row.get("result_structured") or {}

    try:
        odoo.write_field(
            ticket_id=params.ticket_id,
            model_name=params.model_name,
            field_name=params.field_name,
            html=html,
            answer_status=row.get("answer_status") or "",
            confidence=structured.get("confidence") or "",
            request_kind=row.get("request_kind") or "",
        )
    except (PermanentError, TransientError) as exc:
        # The turn is already completed; record the delivery failure so the TUI
        # shows "not in Odoo", then let RQ retry.
        log.warning("support_answer_odoo_callback_error", exc_info=True)
        writers.record_support_turn_callback_failed(ctx.db, params.turn_id, str(exc))
        writers.record_ops_event(ctx.db, "odoo_callback", "error", "write_field_failed", {
            "turn_id": params.turn_id, "ticket_id": params.ticket_id,
        })
        raise

    writers.record_support_turn_callback_sent(ctx.db, params.turn_id)
    log.info("support_answer_done")
    return {"status": "completed", "turn_id": params.turn_id}


def _produce_answer(ctx, params: SupportJobParams, odoo, log) -> str:
    """Run the paid work for one turn and persist it. Returns the rendered HTML."""
    spent = instance_budget_exceeded(ctx, params.odoo_instance_id)
    if spent is not None:
        error = (
            f"Odoo instance daily budget reached (~${spent:.2f} in 24h); "
            f"support answer declined."
        )
        log.warning("support_answer_instance_over_budget", spent_usd=round(spent, 2))
        writers.record_support_turn_failed(ctx.db, params.turn_id, error)
        raise PermanentError(error)

    repo = resolve_repo_context(ctx.github, params.github_url, log)
    config = repo[3] if repo else None
    version = _core_version(ctx, config)

    repo_full_name = None
    if params.github_url:
        parsed = parse_github_repo_url(params.github_url)
        if parsed:
            repo_full_name = f"{parsed[0]}/{parsed[1]}"
    persona = resolve_persona(ctx.db, repo_full_name, params.persona_context)
    persona_block = render_persona_block(persona)

    knowledge = build_ticket_knowledge(
        ctx.claude, ctx.prompts_dir, params.question,
        core=ctx.core_knowledge, version=version,
        db=ctx.db, github=ctx.github, github_url=params.github_url,
    )
    if knowledge.planner_cost:
        writers.record_claude_spend(ctx.db, "support_planner", knowledge.planner_cost)
    if knowledge.planner_error is not None:
        writers.record_ops_event(ctx.db, "support_planner", "warning", "planner_failed", {
            "turn_id": params.turn_id, "error": knowledge.planner_error[:300],
        })
    if knowledge.repo_docs_error is not None:
        writers.record_ops_event(ctx.db, "repo_docs", "warning", "retrieval_failed", {
            "turn_id": params.turn_id, "error": knowledge.repo_docs_error[:300],
        })

    prior_turns = writers.prior_support_turns(
        ctx.db, params.thread_id,
        before_seq=(writers.get_support_turn(ctx.db, params.turn_id) or {}).get("seq", 1),
        # A re-press of "Support request" after a failure is a RETRY of this
        # exact question, not a follow-up — replaying it makes the model
        # restate its earlier answer instead of answering afresh.
        exclude_question=params.question,
    )

    grounding = "docs" if knowledge.blocks else "none"
    response = result = None

    if knowledge.needs_repo_code and code_grounding_allowed(config):
        if repo is None:
            # Wanted code, can't have it: no URL, unparseable, or the App isn't
            # installed. Degrade to docs and SAY SO — a silently ungrounded
            # answer is indistinguishable from a well-grounded one.
            log.warning("support_code_grounding_unavailable",
                        github_url=params.github_url)
            writers.record_ops_event(
                ctx.db, "support_answer", "warning", "code_grounding_unavailable",
                {"turn_id": params.turn_id, "github_url": params.github_url},
            )
        else:
            over = budget_exceeded(ctx)
            if over is not None:
                # Escalation is the expensive leg; decline IT rather than the
                # whole turn, and answer from docs instead.
                log.warning("support_code_grounding_over_budget",
                            spent_usd=round(over, 2))
                writers.record_ops_event(
                    ctx.db, "support_answer", "warning", "code_grounding_over_budget",
                    {"turn_id": params.turn_id, "spent_usd": round(over, 2)},
                )
            else:
                owner, name, token, config = repo
                skill_params = _skill_params(
                    params, persona_block, prior_turns, knowledge
                )
                # The lock spans clone + run so a concurrent job can't reset the
                # shared working tree while the CLI is reading it. A busy lock
                # raises TransientError and RQ retries the whole turn.
                with ctx.runner.repo_lock(owner, name):
                    repo_path = ctx.runner.ensure_repo(owner, name, None, token)
                    response = ctx.runner.review(
                        repo_path=repo_path, skill=_SUPPORT_SKILL,
                        params=skill_params, odoo=config.odoo,
                    )
                grounding = "code"

    if response is None:
        try:
            response, result = ctx.support_answerer.answer_with_response(
                params, persona_block, prior_turns,
                extra_system_blocks=knowledge.blocks or None,
            )
        except MalformedModelOutput as exc:
            # `strict: true` is NOT enforced for this tool (verified against the
            # live API, with and without the structured-outputs beta header), so
            # roughly one call in ten still returns drifted output — typically
            # `handoff` as a mangled JSON string that no unwrap can repair. It's
            # a one-off formatting hiccup, not a doomed input: one paid retry
            # before the consultant sees a failure. Mirrors ticket_runner.
            log.warning("support_answer_malformed_output_retry", error=str(exc))
            writers.record_ops_event(
                ctx.db, "support_answer", "warning", "malformed_output_retried",
                {"turn_id": params.turn_id, "error": str(exc)[:300]},
            )
            response, result = ctx.support_answerer.answer_with_response(
                params, persona_block, prior_turns,
                extra_system_blocks=knowledge.blocks or None,
            )
    else:
        if response.tool_use_input is None:
            raise PermanentError(
                f"{_SUPPORT_SKILL} produced no {SupportAnswerResult.__name__}"
            )
        result = SupportAnswerResult.model_validate(response.tool_use_input)

    html = format_support_html(result)
    writers.record_support_turn_completed(
        ctx.db, params.turn_id, html, response,
        result_structured=result.model_dump(mode="json"),
        request_kind=result.request_kind,
        answer_status=result.answer_status,
        grounding_level=grounding,
    )
    return html
