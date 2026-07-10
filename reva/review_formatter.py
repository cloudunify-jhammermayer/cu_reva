"""Pure formatting helpers for REVA's GitHub output.

No HTTP, no IO. Given a ReviewResult, produce the strings/dicts the
GitHub poster will send. Tested in isolation; see `tests/test_review_formatter.py`.

Templates mirror `doc/08-github-output.md` and the conclusion matrix in
`doc/pr-review-requirements.md` §9.
"""

from __future__ import annotations

import re
from typing import Literal

from reva.diff_utils import DiffHunk, find_line_in_hunks
from reva.types import Finding, IntentIssueVerdict, ReviewResult, Severity

# Internal filesystem roots that must not leak into PR-facing error text (SECU-21):
# the repo cache, temp dir, worker home, container app dir, core worktree.
_INTERNAL_PATH_RE = re.compile(r"/(?:repos|tmp|home|app|core)(?:/[A-Za-z0-9_.\-]+)*")


def _redact_internal_paths(msg: str) -> str:
    """Replace internal server paths (/repos/…, /tmp/…, …) with a placeholder so a
    failure message shown on the PR doesn't disclose the server's layout."""
    return _INTERNAL_PATH_RE.sub("<path>", msg)


def _md_cell(s: str) -> str:
    """Make a (model-produced) string safe in a markdown table cell (USAB-6):
    escape pipes and flatten newlines so a finding title can't break the table."""
    return s.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


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
    "critical": "CRITICAL",
    "major": "MAJOR",
    "minor": "MINOR",
    "info": "INFO",
}


# --- Conclusion mapping ------------------------------------------------------


_SEVERITY_RANK: dict[str, int] = {"info": 0, "minor": 1, "major": 2, "critical": 3}


def compute_check_conclusion(result: ReviewResult) -> CheckConclusion:
    """Map a ReviewResult to the GitHub Check Run `conclusion`.

    Matches the blocking matrix in pr-review-requirements.md §9. The blocking
    severity is per-repo via `result.block_on_severity` (default "major" — any
    major/critical blocks, as before). "none" never blocks.
    """
    if result.status in ("stale", "skipped_trivial"):
        return "skipped"
    if result.status == "declined":
        return "neutral"
    if result.status == "failed":
        return "failure"  # safety: failures block until resolved

    # status == "completed" — gate on the per-repo threshold.
    threshold = result.block_on_severity
    if threshold == "none":
        return "success"
    max_rank = max((_SEVERITY_RANK[f.severity] for f in result.findings), default=-1)
    if max_rank >= _SEVERITY_RANK[threshold]:
        return "failure"
    if max_rank >= _SEVERITY_RANK["minor"]:
        return "neutral"  # findings present but below the gate
    return "success"  # only info, or no findings


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
        # Multi-line comments post a start_line..line range; GitHub rejects the
        # whole review if either endpoint isn't in the diff, so require both.
        end = f.line_end or f.line_start
        if (
            f.file
            and f.line_start
            and find_line_in_hunks(f.file, f.line_start, hunks)
            and find_line_in_hunks(f.file, end, hunks)
        ):
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
        parts.append(_findings_tldr(result.findings))
        if result.intent_check:
            parts.append(_format_intent_check(result.intent_check))
        parts.append(f"**RISK** `{result.risk_level}`")
    elif result.status == "declined":
        parts.append(f"## Declined\n\n{result.decline_reason or result.summary or 'Declined.'}")
    elif result.status == "stale":
        parts.append("## Skipped\n\nThe PR head SHA changed before the review completed; "
                     "a new review will be scheduled on the latest commit.")
    elif result.status == "skipped_trivial":
        parts.append("## Skipped\n\n"
                     + (result.summary or "No substantive changes to review."))
    elif result.status == "failed":
        msg = result.error_message or "An internal error prevented the review from completing."
        parts.append(f"## Error\n\n{_redact_internal_paths(msg)}")

    footer = _footer(result, run_id)
    if footer:
        parts.append(footer)
    return {"title": title, "summary": "\n\n".join(parts), "text": ""}


def _check_run_title(result: ReviewResult) -> str:
    if result.status == "declined":
        return f"{AGENT_NAME} declined this review"
    if result.status == "stale":
        return f"{AGENT_NAME} skipped — head SHA changed"
    if result.status == "skipped_trivial":
        return f"{AGENT_NAME} skipped — no substantive changes"
    if result.status == "failed":
        return f"{AGENT_NAME} encountered an error"
    # completed
    return _summary_one_liner(_severity_counts(result.findings))


def _counts_table(counts: dict[Severity, int]) -> str:
    rows = [
        f"| {SEVERITY_EMOJI[sev]} {_SEVERITY_LABEL[sev]} | {counts[sev]} |"
        for sev in ("critical", "major", "minor", "info")
    ]
    return "| Severity | Count |\n|---|---|\n" + "\n".join(rows)


_INTENT_SYMBOL = {
    "matches": "✅",
    "partial": "⚠️",
    "does_not_match": "❌",
    "unclear": "❓",
}


def _format_intent_check(verdicts: list[IntentIssueVerdict]) -> str:
    """Advisory per-linked-issue conformance section. Never feeds the check
    conclusion — verdicts derive from UNTRUSTED issue text (SECU-6 posture)."""
    lines = ["### Requirements check", ""]
    for v in verdicts:
        symbol = _INTENT_SYMBOL.get(v.verdict, "❓")
        entry = f"- {symbol} #{v.issue_number} — {v.verdict.replace('_', ' ')}"
        if v.note:
            entry += f": {_md_cell(v.note)}"
        lines.append(entry)
    return "\n".join(lines)


def _findings_tldr(findings: list[Finding]) -> str:
    """Findings summary grouped by severity with a sub-table per level."""
    by_severity: dict[Severity, list[Finding]] = {
        "critical": [], "major": [], "minor": [], "info": []
    }
    for f in findings:
        by_severity[f.severity].append(f)

    lines = ["### Findings Summary", ""]
    for sev in ("critical", "major", "minor", "info"):
        group = by_severity[sev]
        label = _SEVERITY_LABEL[sev]
        lines.append(f"**{label}** `{len(group)}`")
        lines.append("")
        if group:
            lines.append("| File | Issue | Confidence |")
            lines.append("|---|---|---|")
            for f in group:
                location = f"`{f.file}:{f.line_start}`" if f.file and f.line_start else (f"`{f.file}`" if f.file else "*(general)*")
                lines.append(f"| {location} | {_md_cell(f.title)} | {f.confidence:.0%} |")
        else:
            lines.append("*No findings.*")
        lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines).rstrip()


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
    return f"*{' · '.join(bits)}*"


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
    parts: list[str] = [f"## {AGENT_NAME} · Review"]
    if result.summary:
        parts.append(result.summary)
    parts.append(_findings_tldr(result.findings))
    if result.intent_check:
        parts.append(_format_intent_check(result.intent_check))
    parts.append(f"**RISK** `{result.risk_level}`")
    if unmapped:
        parts.append(_format_unmapped_section(unmapped))
    parts.append(_footer(result, run_id))
    parts.append(
        "*Resolve a thread once addressed — that tells me the finding landed. "
        "Reply `/dismiss` on a finding you disagree with, or `/mute <category>` "
        "to stop flagging that category in this repo (`/unmute` to undo).*"
    )
    return "\n\n".join(parts)


def _format_unmapped_section(unmapped: list[Finding]) -> str:
    lines = ["**GENERAL**", "", "| File | Issue | Confidence |", "|---|---|---|"]
    for f in unmapped:
        location = f"`{f.file}:{f.line_start}`" if f.file and f.line_start else (f"`{f.file}`" if f.file else "*(general)*")
        lines.append(f"| {location} | {_md_cell(f.title)} | {f.confidence:.0%} |")
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
    """Shape the dict the GitHub Reviews API expects in `comments[]`.

    Requires `line_start` — an inline comment must anchor to a line. Callers
    route line-less findings to the review body (see `split_findings`); enforce
    the invariant here so a stray None fails loudly instead of building an
    invalid payload / TypeError on the line-range check (CORR-20).
    """
    if finding.line_start is None:
        raise ValueError("inline comment requires line_start; route via split_findings")
    payload: dict = {
        "path": finding.file,
        "line": finding.line_start,
        "side": "RIGHT",
        "body": format_inline_comment(finding),
    }
    if finding.line_end is not None and finding.line_end > finding.line_start:
        payload["start_line"] = finding.line_start
        payload["start_side"] = "RIGHT"
        payload["line"] = finding.line_end
    return payload


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
