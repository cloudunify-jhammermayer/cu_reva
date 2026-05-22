"""Tests for review_formatter — pure formatting, no IO."""

from __future__ import annotations

from reva.diff_utils import DiffHunk
from reva.review_formatter import (
    compute_check_conclusion,
    format_check_run_output,
    format_decline_body,
    format_inline_comment,
    format_pr_review_body,
    split_findings,
)
from reva.types import Finding, ReviewResult


def _f(severity, *, file=None, line_start=None, suggestion=None, confidence=0.8, title="t") -> Finding:
    return Finding(
        severity=severity,
        category="bug",
        file=file,
        line_start=line_start,
        line_end=line_start,
        title=title,
        body="body text",
        suggestion=suggestion,
        confidence=confidence,
        is_odoo_specific=False,
    )


def _result(status="completed", findings=None, **kwargs):
    return ReviewResult(
        status=status,
        summary=kwargs.get("summary", "All good."),
        risk_level=kwargs.get("risk_level", "low"),
        findings=findings or [],
        model=kwargs.get("model", "claude-sonnet-4-6"),
        prompt_version=kwargs.get("prompt_version", "v1.0"),
        duration_ms=kwargs.get("duration_ms", 134_000),  # 2m 14s
        estimated_cost_usd=kwargs.get("estimated_cost_usd", 0.042),
        decline_reason=kwargs.get("decline_reason"),
    )


# --- conclusion mapping (pr-review-requirements §9) -------------------------


def test_conclusion_no_findings_is_success():
    assert compute_check_conclusion(_result()) == "success"


def test_conclusion_info_only_is_success():
    assert compute_check_conclusion(_result(findings=[_f("info")])) == "success"


def test_conclusion_minor_only_is_neutral():
    assert compute_check_conclusion(_result(findings=[_f("minor")])) == "neutral"


def test_conclusion_major_is_failure():
    assert compute_check_conclusion(_result(findings=[_f("major")])) == "failure"


def test_conclusion_critical_is_failure():
    assert compute_check_conclusion(_result(findings=[_f("critical")])) == "failure"


def test_conclusion_declined_is_neutral():
    assert compute_check_conclusion(_result(status="declined")) == "neutral"


def test_conclusion_failed_is_failure():
    assert compute_check_conclusion(_result(status="failed")) == "failure"


def test_conclusion_stale_is_skipped():
    assert compute_check_conclusion(_result(status="stale")) == "skipped"


# --- finding split ----------------------------------------------------------


def test_split_findings_inline_vs_unmapped():
    hunks = [DiffHunk(file_path="x.py", new_start=10, new_count=5)]
    f_in = _f("major", file="x.py", line_start=12)
    f_out_of_range = _f("minor", file="x.py", line_start=99)
    f_no_file = _f("info")
    inline, unmapped = split_findings([f_in, f_out_of_range, f_no_file], hunks)
    assert inline == [f_in]
    assert unmapped == [f_out_of_range, f_no_file]


# --- check run output --------------------------------------------------------


def test_check_run_title_lists_severities_present():
    out = format_check_run_output(
        _result(findings=[_f("critical"), _f("minor"), _f("minor")]),
        run_id=7,
    )
    assert "1 critical" in out["title"]
    assert "2 minor" in out["title"]
    assert out["summary"].startswith("## Review Summary")
    assert "**RISK**" in out["summary"]
    assert "Run #7" in out["summary"]
    assert "claude-sonnet-4-6" in out["summary"]
    assert "$0.0420" in out["summary"]


def test_check_run_title_when_no_findings():
    out = format_check_run_output(_result(), run_id=1)
    assert "no issues" in out["title"].lower()


# --- pr review body ---------------------------------------------------------


def test_pr_review_body_includes_counts_table_and_footer():
    body = format_pr_review_body(
        _result(findings=[_f("critical"), _f("minor")]),
        unmapped=[],
        run_id=42,
    )
    assert body.startswith("## REVA · Review")
    assert "**CRITICAL** `1`" in body
    assert "**MINOR** `1`" in body
    assert "Run #42" in body
    assert "Other Observations" not in body


def test_pr_review_body_includes_unmapped_section_when_present():
    unmapped = [_f("major", title="general concern")]
    body = format_pr_review_body(_result(findings=unmapped), unmapped=unmapped, run_id=1)
    assert "**GENERAL**" in body
    assert "general concern" in body


# --- inline comment ---------------------------------------------------------


def test_inline_comment_includes_emoji_confidence_category():
    text = format_inline_comment(
        _f("critical", file="x.py", line_start=10, confidence=0.95, suggestion=None,
           title="SQL injection")
    )
    assert "🔴" in text
    assert "CRITICAL: SQL injection" in text
    assert "**Confidence**: 0.95" in text
    assert "**Category**: bug" in text
    assert "Suggestion" not in text


def test_inline_comment_omits_suggestion_block_when_none():
    text = format_inline_comment(_f("minor", file="x.py", line_start=5, suggestion=None))
    assert "```" not in text


def test_inline_comment_includes_suggestion_block_when_provided():
    text = format_inline_comment(
        _f("minor", file="x.py", line_start=5, suggestion="use foo() instead")
    )
    assert "**Suggestion**" in text
    assert "use foo() instead" in text


# --- decline body -----------------------------------------------------------


def test_decline_body_contains_reason_and_footer():
    text = format_decline_body(
        _result(status="declined", decline_reason="Diff too large (2000 lines > 1000 max)."),
        run_id=99,
    )
    assert "REVA Review — Declined" in text
    assert "2000 lines" in text
    assert "Run #99" in text
