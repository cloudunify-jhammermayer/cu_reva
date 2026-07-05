"""Reviewer triage integration: default-off, escalate-only, fail-open."""

from __future__ import annotations

from reva.triage import TriageDecision
from reva.types import ClaudeResponse
from tests.test_reviewer import (
    FakeGitHub,
    FakeRunner,
    _claude_response_with_findings,
    _make_reviewer,
    _params,
)


class FakeClaude:
    pass


def _empty_response() -> ClaudeResponse:
    return _claude_response_with_findings([])


def test_triage_default_off_never_calls_decide(monkeypatch):
    runner = FakeRunner(response=_empty_response())
    reviewer, _, _, runner, _ = _make_reviewer(runner=runner)

    def fail_decide(*args, **kwargs):
        raise AssertionError("triage should be disabled by default")

    monkeypatch.setattr("worker.reviewer.triage_mod.decide", fail_decide)

    result = reviewer.execute(_params())

    assert result.status == "completed"
    assert runner.last_skill == "reva-diff-review"
    assert result.triage_escalation is None


def test_triage_does_not_touch_explicit_full_or_comments(monkeypatch):
    calls = []

    def fake_decide(*args, **kwargs):
        calls.append(args)
        return TriageDecision("deep", "security"), 0.002

    monkeypatch.setattr("worker.reviewer.triage_mod.decide", fake_decide)

    runner = FakeRunner(response=_empty_response())
    reviewer, _, _, _, _ = _make_reviewer(
        runner=runner,
        claude=FakeClaude(),
        triage_enabled=True,
    )
    reviewer.execute(_params(review_mode="full", trigger_event="opened"))
    reviewer.execute(_params(trigger_event="comment"))

    assert calls == []


def test_repo_config_can_disable_triage(monkeypatch):
    calls = []

    def fake_decide(*args, **kwargs):
        calls.append(args)
        return TriageDecision("deep", "security"), 0.002

    monkeypatch.setattr("worker.reviewer.triage_mod.decide", fake_decide)
    github = FakeGitHub(file_contents={".claude-review.yml": "triage: false\n"})
    runner = FakeRunner(response=_empty_response())
    reviewer, _, _, _, _ = _make_reviewer(
        github=github,
        runner=runner,
        claude=FakeClaude(),
        triage_enabled=True,
    )

    result = reviewer.execute(_params())

    assert calls == []
    assert result.triage_escalation is None
    assert runner.last_skill == "reva-diff-review"


def test_triage_none_keeps_diff_mode_and_records_spend(monkeypatch):
    spend = []

    def fake_decide(claude, prompts_dir, diff, changed_files, current_mode):
        return TriageDecision("none", "routine"), 0.002

    monkeypatch.setattr("worker.reviewer.triage_mod.decide", fake_decide)
    runner = FakeRunner(response=_empty_response())
    reviewer, _, _, _, _ = _make_reviewer(
        runner=runner,
        claude=FakeClaude(),
        triage_enabled=True,
        spend_recorder=lambda kind, cost: spend.append((kind, cost)),
    )

    result = reviewer.execute(_params())

    assert result.triage_escalation is None
    assert runner.last_skill == "reva-diff-review"
    assert runner.last_model == runner.default_model
    assert spend == [("triage", 0.002)]


def test_triage_deep_uses_full_skill_deep_model_and_stamp(monkeypatch):
    spend = []

    def fake_decide(claude, prompts_dir, diff, changed_files, current_mode):
        return TriageDecision("deep", "raw SQL"), 0.003

    monkeypatch.setattr("worker.reviewer.triage_mod.decide", fake_decide)
    runner = FakeRunner(response=_empty_response())
    reviewer, _, _, _, _ = _make_reviewer(
        runner=runner,
        claude=FakeClaude(),
        triage_enabled=True,
        spend_recorder=lambda kind, cost: spend.append((kind, cost)),
    )

    result = reviewer.execute(_params(review_mode="diff-all"))

    assert result.triage_escalation == "deep"
    assert runner.last_skill == "reva-full-review"
    assert runner.last_model == runner.deep_model
    assert spend == [("triage", 0.003)]


def test_triage_error_fails_open_and_records_ops_event(monkeypatch):
    ops_events = []

    def fake_decide(claude, prompts_dir, diff, changed_files, current_mode):
        return TriageDecision("none", "error: api down"), 0.0

    monkeypatch.setattr("worker.reviewer.triage_mod.decide", fake_decide)
    runner = FakeRunner(response=_empty_response())
    reviewer, _, _, _, _ = _make_reviewer(
        runner=runner,
        claude=FakeClaude(),
        triage_enabled=True,
        ops_recorder=lambda c, s, e, d: ops_events.append((c, s, e, d)),
    )

    result = reviewer.execute(_params())

    assert result.status == "completed"
    assert result.triage_escalation is None
    assert runner.last_skill == "reva-diff-review"
    assert ops_events == [
        (
            "triage",
            "warning",
            "decide_failed",
            {
                "repository_id": 1,
                "pull_request_id": 1,
                "reason": "error: api down",
            },
        )
    ]
