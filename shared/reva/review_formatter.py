"""Pure formatting helpers for REVA's GitHub output.

No HTTP, no IO. Given a ReviewResult, produce the strings/dicts the
GitHub poster will send. Tested in isolation; see `tests/test_review_formatter.py`.

Templates mirror `doc/08-github-output.md` and the conclusion matrix in
`doc/pr-review-requirements.md` §9.
"""

from __future__ import annotations

from typing import Literal

from reva.diff_utils import DiffHunk, find_line_in_hunks
from reva.types import Finding, ReviewResult, Severity

AGENT_NAME = "REVA"
CHECK_RUN_NAME = "REVA Review"
REVIEW_EVENT = "COMMENT"  # doc/08: always COMMENT; Check Run handles blocking.

CheckConclusion = Literal["success", "neutral", "failure", "skipped"]

SEVERITY_EMOJI: dict[Severity, str] = {
    "critical": "🔴",
    "major": "🟠",
    "minor": "🟡",
    "info": "🔵",
}

_SEVERITY_LABEL: dict[Severity, str] = {
    "critical": "Critical",
    "major": "Major",
    "minor": "Minor",
    "info": "Info",
}


# --- Conclusion mapping ------------------------------------------------------


def compute_check_conclusion(result: ReviewResult) -> CheckConclusion:
    """Map a ReviewResult to the GitHub Check Run `conclusion`.

    Matches the blocking matrix in pr-review-requirements.md §9.
    """
    if result.status == "stale":
        return "skipped"
    if result.status == "declined":
        return "neutral"
    if result.status == "failed":
        return "failure"  # safety: failures block until resolved

    # status == "completed"
    severities = {f.severity for f in result.findings}
    if "critical" in severities or "major" in severities:
        return "failure"
    if "minor" in severities:
        return "neutral"
    # only info, or no findings
    return "success"


# --- Finding partitioning ----------------------------------------------------


def split_findings(
    findings: list[Finding], hunks: list[DiffHunk]
) -> tuple[list[Finding], list[Finding]]:
    """Partition findings into (inline, unmapped).

    A finding is inline-postable when it has a file + line_start that falls
    inside a diff hunk on the new side. Everything else (no file, no line,
    or line outside any hunk) is unmapped and appears in the review body.
    """
    inline: list[Finding] = []
    unmapped: list[Finding] = []
    for f in findings:
        if f.file and f.line_start and find_line_in_hunks(f.file, f.line_start, hunks):
            inline.append(f)
        else:
            unmapped.append(f)
    return inline, unmapped


# --- Check Run output --------------------------------------------------------


def _severity_counts(findings: list[Finding]) -> dict[Severity, int]:
    counts: dict[Severity, int] = {"critical": 0, "major": 0, "minor": 0, "info": 0}
    for f in findings:
        counts[f.severity] += 1
    return counts


def _summary_one_liner(counts: dict[Severity, int]) -> str:
    parts = [f"{n} {label.lower()}" for label, n in (
        ("critical", counts["critical"]),
        ("major", counts["major"]),
        ("minor", counts["minor"]),
        ("info", counts["info"]),
    ) if n]
    if not parts:
        return f"{AGENT_NAME} found no issues."
    return f"{AGENT_NAME} found " + ", ".join(parts) + "."


def format_check_run_output(result: ReviewResult, run_id: int | None = None) -> dict:
    """Return the {title, summary, text} payload for a Check Run output.

    Supports all four statuses (completed / declined / stale / failed) —
    title and summary adapt to the outcome.
    """
    title = _check_run_title(result)

    parts: list[str] = []
    if result.status == "completed":
        if result.summary:
            parts.append(f"## Review Summary\n\n{result.summary}")
        parts.append(_counts_table(_severity_counts(result.findings)))
        parts.append(f"**Risk Level**: {result.risk_level}")
    elif result.status == "declined":
        parts.append(f"## Declined\n\n{result.decline_reason or result.summary or 'Declined.'}")
    elif result.status == "stale":
        parts.append("## Skipped\n\nThe PR head SHA changed before the review completed; "
                     "a new review will be scheduled on the latest commit.")
    elif result.status == "failed":
        msg = result.error_message or "An internal error prevented the review from completing."
        parts.append(f"## Error\n\n{msg}")

    footer = _footer(result, run_id)
    if footer:
        parts.append(footer)
    return {"title": title, "summary": "\n\n".join(parts), "text": ""}


def _check_run_title(result: ReviewResult) -> str:
    if result.status == "declined":
        return f"{AGENT_NAME} declined this review"
    if result.status == "stale":
        return f"{AGENT_NAME} skipped — head SHA changed"
    if result.status == "failed":
        return f"{AGENT_NAME} encountered an error"
    # completed
    return _summary_one_liner(_severity_counts(result.findings))


def _counts_table(counts: dict[Severity, int]) -> str:
    rows = [
        f"| {SEVERITY_EMOJI[sev]} {_SEVERITY_LABEL[sev]} | {counts[sev]} |"
        for sev in ("critical", "major", "minor", "info")
    ]
    return "### Findings Summary\n\n| Severity | Count |\n|---|---|\n" + "\n".join(rows)


def _footer(result: ReviewResult, run_id: int | None) -> str:
    """Generate the small italic footer used in summaries and review bodies."""
    bits: list[str] = [AGENT_NAME]
    if result.prompt_version:
        bits.append(result.prompt_version)
    if result.model:
        bits.append(result.model)
    if result.duration_ms is not None:
        bits.append(_format_duration(result.duration_ms))
    if result.estimated_cost_usd:
        bits.append(f"${result.estimated_cost_usd:.4f}")
    if run_id is not None:
        bits.append(f"Run #{run_id}")
    return f"*{' | '.join(bits)}*"


def _format_duration(ms: int) -> str:
    if ms < 1000:
        return f"{ms}ms"
    seconds = ms // 1000
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}m {seconds:02d}s"


# --- PR review body ----------------------------------------------------------


def format_pr_review_body(
    result: ReviewResult,
    unmapped: list[Finding],
    run_id: int | None = None,
) -> str:
    """Top-level body of the PR review (not an inline comment)."""
    parts: list[str] = [f"## 🔍 {AGENT_NAME} Review"]
    if result.summary:
        parts.append(result.summary)
    parts.append(_counts_table(_severity_counts(result.findings)))
    parts.append(f"**Risk Level**: {result.risk_level}")
    if unmapped:
        parts.append(_format_unmapped_section(unmapped))
    parts.append(_footer(result, run_id))
    parts.append("*React with 👍 or 👎 on individual comments to help me improve.*")
    return "\n\n".join(parts)


def _format_unmapped_section(unmapped: list[Finding]) -> str:
    lines = ["### Other Observations", ""]
    for f in unmapped:
        emoji = SEVERITY_EMOJI[f.severity]
        location = f.file or "(no file)"
        if f.file and f.line_start:
            location = f"{f.file}:{f.line_start}"
        lines.append(f"- {emoji} **{_SEVERITY_LABEL[f.severity]}** — {f.title} ({location})")
        if f.body:
            lines.append(f"  {f.body}")
    return "\n".join(lines)


# --- Inline comment ----------------------------------------------------------


def format_inline_comment(finding: Finding) -> str:
    emoji = SEVERITY_EMOJI[finding.severity]
    lines = [
        f"### {emoji} {_SEVERITY_LABEL[finding.severity]}: {finding.title}",
        "",
        f"**Confidence**: {finding.confidence:.2f}",
        f"**Category**: {finding.category}",
        "",
        finding.body,
    ]
    if finding.suggestion:
        lines.extend(
            [
                "",
                "**Suggestion**:",
                "```",
                finding.suggestion,
                "```",
            ]
        )
    return "\n".join(lines)


def format_inline_comment_payload(finding: Finding) -> dict:
    """Shape the dict the GitHub Reviews API expects in `comments[]`."""
    return {
        "path": finding.file,
        "line": finding.line_start,
        "body": format_inline_comment(finding),
    }


# --- Decline message ---------------------------------------------------------


def format_decline_body(result: ReviewResult, run_id: int | None = None) -> str:
    reason = result.decline_reason or "Review declined."
    parts = [
        f"## ⚠️ {AGENT_NAME} Review — Declined",
        "",
        reason,
        "",
        "You can trigger a review on a smaller PR by pushing changes or commenting `/review`.",
        "",
        _footer(result, run_id),
    ]
    return "\n".join(parts)
