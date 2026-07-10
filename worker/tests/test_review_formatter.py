"""Tests for review_formatter — pure formatting, no IO."""

from __future__ import annotations

import pytest

from reva.diff_utils import DiffHunk
from reva.review_formatter import (
    compute_check_conclusion,
    format_check_run_output,
    format_decline_body,
    format_inline_comment,
    format_inline_comment_payload,
    format_pr_review_body,
    split_findings,
)
from reva.types import Finding, IntentIssueVerdict, ReviewResult


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
        block_on_severity=kwargs.get("block_on_severity", "major"),
        intent_check=kwargs.get("intent_check"),
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


def test_conclusion_skipped_trivial_is_skipped():
    assert compute_check_conclusion(_result(status="skipped_trivial")) == "skipped"


def test_skipped_trivial_output_is_non_error():
    out = format_check_run_output(_result(status="skipped_trivial", summary="No substantive changes."))
    assert "error" not in out["title"].lower()
    assert "Skipped" in out["summary"]


# --- per-repo gating (block_on_severity) ------------------------------------


def test_gate_critical_threshold_major_is_neutral():
    r = _result(findings=[_f("major")], block_on_severity="critical")
    assert compute_check_conclusion(r) == "neutral"


def test_gate_critical_threshold_critical_is_failure():
    r = _result(findings=[_f("critical")], block_on_severity="critical")
    assert compute_check_conclusion(r) == "failure"


def test_gate_minor_threshold_minor_is_failure():
    r = _result(findings=[_f("minor")], block_on_severity="minor")
    assert compute_check_conclusion(r) == "failure"


def test_gate_minor_threshold_info_only_is_success():
    r = _result(findings=[_f("info")], block_on_severity="minor")
    assert compute_check_conclusion(r) == "success"


def test_gate_none_never_blocks():
    r = _result(findings=[_f("critical")], block_on_severity="none")
    assert compute_check_conclusion(r) == "success"


def test_gate_declined_ignores_threshold():
    r = _result(status="declined", block_on_severity="none")
    assert compute_check_conclusion(r) == "neutral"


# --- finding split ----------------------------------------------------------


def test_split_findings_inline_vs_unmapped():
    hunks = [DiffHunk(file_path="x.py", new_start=10, new_count=5)]
    f_in = _f("major", file="x.py", line_start=12)
    f_out_of_range = _f("minor", file="x.py", line_start=99)
    f_no_file = _f("info")
    inline, unmapped = split_findings([f_in, f_out_of_range, f_no_file], hunks)
    assert inline == [f_in]
    assert unmapped == [f_out_of_range, f_no_file]


def test_split_findings_multiline_range_must_be_fully_in_hunk():
    """A finding whose line_end escapes the hunk is unmapped (GitHub 422 otherwise)."""
    hunks = [DiffHunk(file_path="x.py", new_start=10, new_count=5)]  # lines 10-14
    spanning = Finding(
        severity="major", category="bug", file="x.py",
        line_start=12, line_end=99,  # start in hunk, end outside
        title="t", body="b", confidence=0.8, is_odoo_specific=False,
    )
    inline, unmapped = split_findings([spanning], hunks)
    assert inline == []
    assert unmapped == [spanning]


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


def test_failed_check_run_redacts_internal_paths():
    """SECU-21: the failure Check Run shown on the PR must not leak server paths."""
    result = ReviewResult(
        status="failed", summary="", risk_level="low",
        error_message="Claude did not create output file at /repos/acme/widgets/.reva_review_ab12.json",
    )
    summary = format_check_run_output(result, run_id=1)["summary"]
    assert "/repos/acme/widgets" not in summary
    assert "<path>" in summary


def test_inline_payload_requires_line_start():
    """CORR-20: an inline comment must anchor to a line; None fails loudly."""
    with pytest.raises(ValueError, match="line_start"):
        format_inline_comment_payload(_f("major", file="a.py", line_start=None))


def test_finding_title_with_pipe_is_escaped_in_table():
    """USAB-6: a model title containing '|' or newline must not break the table."""
    body = format_pr_review_body(
        _result(findings=[_f("major", file="x.py", line_start=3, title="a | b\nc")]),
        unmapped=[],
        run_id=1,
    )
    assert "a \\| b" in body          # pipe escaped
    assert "a | b\nc" not in body     # raw pipe+newline gone


def test_finding_body_and_suggestion_are_bounded():
    """CORR-14: oversized body/suggestion are truncated so one finding can't blow
    past GitHub's comment-size limit and fail the whole review."""
    from reva.types import Finding
    f = Finding(severity="major", category="bug", title="t",
                body="x" * 20000, suggestion="y" * 20000, confidence=0.9)
    assert len(f.body) <= 8000
    assert f.body.endswith("...")
    assert len(f.suggestion) <= 4000


# --- Requirements check (issue-conformance verdicts) -------------------------


def _iv(n=42, verdict="matches", note="does what the issue asked"):
    return IntentIssueVerdict(issue_number=n, verdict=verdict, note=note)


def test_check_run_renders_requirements_check():
    out = format_check_run_output(_result(intent_check=[_iv()]))
    assert "### Requirements check" in out["summary"]
    assert "#42" in out["summary"]
    assert "matches" in out["summary"]
    assert "does what the issue asked" in out["summary"]


def test_requirements_check_absent_without_verdicts():
    assert "Requirements check" not in format_check_run_output(_result())["summary"]


def test_requirements_check_symbols_per_verdict():
    out = format_check_run_output(_result(intent_check=[
        _iv(1, "matches"), _iv(2, "partial"),
        _iv(3, "does_not_match"), _iv(4, "unclear"),
    ]))["summary"]
    assert "✅ #1" in out and "⚠️ #2" in out and "❌ #3" in out and "❓ #4" in out
    # Enum values render human-readable.
    assert "does not match" in out and "does_not_match" not in out


def test_pr_review_body_renders_requirements_check():
    body = format_pr_review_body(_result(intent_check=[_iv(verdict="partial")]), unmapped=[])
    assert "### Requirements check" in body


def test_mismatch_verdict_never_changes_conclusion():
    # Advisory only (SECU-6 posture): a does_not_match with no findings stays success.
    r = _result(intent_check=[_iv(verdict="does_not_match")])
    assert compute_check_conclusion(r) == "success"


def test_verdict_note_empty_renders_without_colon():
    out = format_check_run_output(_result(intent_check=[_iv(note="")]))["summary"]
    assert "#42 — matches\n" in out or out.rstrip().endswith("#42 — matches")
