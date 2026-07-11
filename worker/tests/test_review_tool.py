"""Tests for the submit_review tool schema + intent-check types."""

from __future__ import annotations

from reva.review_tool import build_review_tool_schema
from reva.types import IntentIssueVerdict, RepoConfig


def test_schema_exposes_optional_intent_check():
    schema = build_review_tool_schema()
    input_schema = schema["input_schema"]
    assert "intent_check" in input_schema["properties"]
    # Optional: the reviewer must be able to omit it (delta reviews, no linked issue).
    assert "intent_check" not in input_schema["required"]
    assert input_schema["required"] == ["summary", "risk_level", "findings"]


def test_schema_inlines_intent_verdict_def():
    schema = build_review_tool_schema()
    assert "IntentIssueVerdict" in schema["input_schema"].get("$defs", {})


def test_intent_verdict_note_truncated_to_300():
    v = IntentIssueVerdict(issue_number=1, verdict="matches", note="Z" * 500)
    assert len(v.note) == 300
    assert v.note.endswith("...")


def test_intent_verdict_note_defaults_empty():
    v = IntentIssueVerdict(issue_number=1, verdict="unclear")
    assert v.note == ""


def test_repo_config_intent_check_defaults_on():
    assert RepoConfig().intent_check is True
    assert RepoConfig.model_validate({"intent_check": False}).intent_check is False


def test_repo_config_board_status_sync_defaults_on():
    assert RepoConfig().board_status_sync is True
    assert RepoConfig.model_validate({"board_status_sync": False}).board_status_sync is False


def test_repo_config_work_status_defaults_on():
    assert RepoConfig().work_status is True
    assert RepoConfig.model_validate({"work_status": False}).work_status is False
