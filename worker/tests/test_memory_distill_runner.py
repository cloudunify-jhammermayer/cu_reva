"""Tests for the run_memory_distill RQ job."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from reva.types import ClaudeResponse
from worker.memory_distill_runner import run_memory_distill


def _response() -> ClaudeResponse:
    return ClaudeResponse(
        model="claude-sonnet-5", stop_reason="tool_use",
        tool_use_input={}, input_tokens=800, output_tokens=100,
    )


def test_budget_skip_before_paid_call():
    ctx = MagicMock()
    with patch("worker.memory_distill_runner.get_context", return_value=ctx), \
         patch("worker.memory_distill_runner.budget_exceeded", return_value=12.0), \
         patch("worker.memory_distill_runner.writers") as w:
        out = run_memory_distill(5)
    assert out["status"] == "skipped_budget"
    ctx.memory_distiller.distill.assert_not_called()
    w.record_repo_memory.assert_not_called()


def test_distills_records_version_and_spend():
    ctx = MagicMock()
    ctx.memory_distiller.distill.return_value = ("block", [{"guidance": "g"}], _response())
    with patch("worker.memory_distill_runner.get_context", return_value=ctx), \
         patch("worker.memory_distill_runner.budget_exceeded", return_value=None), \
         patch("worker.memory_distill_runner.writers") as w:
        w.get_memory_distill_input.return_value = {
            "window_days": 90, "category_stats": [], "dismissed_count": 3,
            "newest_feedback_at": None,
        }
        w.record_repo_memory.return_value = 2
        out = run_memory_distill(5)
    assert out == {"status": "completed", "version": 2, "items": 1}
    w.record_repo_memory.assert_called_once()
    spend = [c for c in w.record_claude_spend.call_args_list if c.args[1] == "learned_memory"]
    assert len(spend) == 1 and spend[0].args[2] > 0
