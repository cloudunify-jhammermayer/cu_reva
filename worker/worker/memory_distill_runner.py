"""RQ job: distill a repo's dismissed-finding history into learned review memory.

Enqueued by the scheduler's maybe_distill_memories loop. Budget-gated before the
paid call (cost-control invariant); writes a new memory version (possibly empty).
"""

from __future__ import annotations

import structlog

from reva.cost import estimate_cost
from reva.db import writers
from worker.runner import budget_exceeded, get_context

logger = structlog.get_logger()


def run_memory_distill(repository_id: int) -> dict:
    """Distill and persist one repo's learned memory. TransientError (malformed
    distill output) rides the RQ retries; the prior active version stays live
    until a new one lands."""
    ctx = get_context()
    log = logger.bind(repository_id=repository_id)

    # Cost-control invariant: check the rolling 24h cap (advisory-locked read)
    # before the paid distill call.
    spent = budget_exceeded(ctx)
    if spent is not None:
        log.info("memory_distill_budget_skip", spent=round(spent, 2))
        return {"status": "skipped_budget"}

    distill_input = writers.get_memory_distill_input(ctx.db, repository_id)
    content, items, response = ctx.memory_distiller.distill(distill_input)

    newest = distill_input.get("newest_feedback_at")
    source_stats = {
        "window_days": distill_input.get("window_days"),
        "category_stats": distill_input.get("category_stats", []),
        "dismissed_count": distill_input.get("dismissed_count", 0),
        "newest_feedback_at": newest.isoformat() if newest is not None else None,
    }
    version = writers.record_repo_memory(
        ctx.db, repository_id,
        items=items, content=content, source_stats=source_stats, response=response,
    )
    cost = estimate_cost(
        response.model, response.input_tokens, response.output_tokens,
        response.cache_read_tokens, response.cache_creation_tokens,
    )
    writers.record_claude_spend(ctx.db, "learned_memory", cost)
    log.info(
        "memory_distill_done", version=version, items=len(items), chars=len(content),
    )
    return {"status": "completed", "version": version, "items": len(items)}
