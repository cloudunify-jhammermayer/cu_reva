"""Pure ticket analysis: Claude call + HTML formatter.

No side effects — no DB writes, no HTTP calls to Odoo.
The caller (ticket_runner.py) owns persistence and the callback POST.
"""

from __future__ import annotations

import os

from reva.claude_client import ClaudeClient
from reva.errors import PermanentError
from reva.ticket_tool import TICKET_TOOL_NAME, build_ticket_tool_schema, ticket_tool_choice
from reva.types import ClaudeResponse, ContentBlock, TicketAnalysisResult, TicketJobParams


class TicketAnalyzer:
    def __init__(self, claude: ClaudeClient, prompts_dir: str) -> None:
        self._claude = claude
        self._prompts_dir = prompts_dir

    def analyze(self, params: TicketJobParams) -> TicketAnalysisResult:
        """Call Claude and return a validated TicketAnalysisResult."""
        _, result = self.analyze_with_response(params)
        return result

    def analyze_with_response(
        self, params: TicketJobParams
    ) -> tuple[ClaudeResponse, TicketAnalysisResult]:
        """Call Claude and return (raw response, validated result).

        The raw response is needed by the runner to record token usage.
        """
        system_blocks = self._build_system()
        tool_schema = build_ticket_tool_schema()

        response = self._claude.review(
            system_blocks=system_blocks,
            user_prompt=params.text,
            tools=[tool_schema],
            tool_choice=ticket_tool_choice(),
        )

        if response.tool_use_input is None:
            raise PermanentError(
                f"Claude did not call {TICKET_TOOL_NAME} "
                f"(stop_reason={response.stop_reason})"
            )

        try:
            result = TicketAnalysisResult.model_validate(response.tool_use_input)
        except Exception as exc:
            raise PermanentError(
                f"ticket analysis result failed schema validation: {exc}"
            ) from exc

        return response, result

    def _build_system(self) -> list[ContentBlock]:
        path = os.path.join(self._prompts_dir, "ticket_analysis.md")
        with open(path) as f:
            text = f.read()
        return [
            {
                "type": "text",
                "text": text,
                "cache_control": {"type": "ephemeral"},
            }
        ]


# ---------------------------------------------------------------------------
# HTML formatter
# ---------------------------------------------------------------------------

_CATEGORY_LABEL = {
    "happy_path": "Happy Path",
    "edge_case": "Edge Cases",
    "error_scenario": "Error Scenarios",
}


_CONFIDENCE_BADGE: dict[str, str] = {
    "explicit": (
        '<span style="font-size:0.75em;font-weight:bold;color:#1a7f37;'
        'background:#dafbe1;border:1px solid #82cfae;border-radius:3px;'
        'padding:1px 5px;margin-left:6px;vertical-align:middle;">from ticket</span>'
    ),
    "inferred": (
        '<span style="font-size:0.75em;font-weight:bold;color:#9a6700;'
        'background:#fff8c5;border:1px solid #e3b341;border-radius:3px;'
        'padding:1px 5px;margin-left:6px;vertical-align:middle;">inferred</span>'
    ),
    "assumed": (
        '<span style="font-size:0.75em;font-weight:bold;color:#57606a;'
        'background:#f6f8fa;border:1px solid #d0d7de;border-radius:3px;'
        'padding:1px 5px;margin-left:6px;vertical-align:middle;">assumed</span>'
    ),
}


def format_ticket_html(result: TicketAnalysisResult) -> str:
    """Convert a TicketAnalysisResult into an HTML string suitable for Odoo HTML fields."""
    parts: list[str] = []

    # Legend
    legend_items = "".join(
        f'<span style="margin-right:12px;">{_CONFIDENCE_BADGE[k]} {label}</span>'
        for k, label in [
            ("explicit", "directly stated in ticket"),
            ("inferred", "derived from context"),
            ("assumed", "standard practice, not in ticket"),
        ]
    )
    parts.append(
        f'<p style="font-size:0.85em;color:#57606a;border-bottom:1px solid #d0d7de;'
        f'padding-bottom:6px;">{legend_items}</p>'
    )

    # Summary
    parts.append(f"<h2>Summary</h2><p>{_esc(result.summary)}</p>")

    # Missing information
    if result.missing_info:
        items = "".join(
            f"<li>{_CONFIDENCE_BADGE.get(i.confidence, '')} {_esc(i.text)}</li>"
            for i in result.missing_info
        )
        parts.append(f"<h2>Missing Information</h2><ul>{items}</ul>")
    else:
        parts.append("<h2>Missing Information</h2><p><em>No missing information identified.</em></p>")

    # Acceptance criteria
    if result.acceptance_criteria:
        items = "".join(
            f"<li>"
            f"{_CONFIDENCE_BADGE.get(ac.confidence, '')} "
            f"<strong>Given</strong> {_esc(ac.given)} "
            f"<strong>When</strong> {_esc(ac.when)} "
            f"<strong>Then</strong> {_esc(ac.then)}"
            f"</li>"
            for ac in result.acceptance_criteria
        )
        parts.append(f"<h2>Acceptance Criteria</h2><ul>{items}</ul>")

    # Test cases grouped by category
    if result.test_cases:
        by_category: dict[str, list[tuple[str, str]]] = {}
        for tc in result.test_cases:
            by_category.setdefault(tc.category, []).append((tc.description, tc.confidence))

        tc_html = "<h2>Test Cases</h2>"
        for cat in ("happy_path", "edge_case", "error_scenario"):
            cases = by_category.get(cat, [])
            if cases:
                label = _CATEGORY_LABEL[cat]
                items = "".join(
                    f"<li>{_CONFIDENCE_BADGE.get(conf, '')} {_esc(desc)}</li>"
                    for desc, conf in cases
                )
                tc_html += f"<h3>{label}</h3><ul>{items}</ul>"
        parts.append(tc_html)

    # Definition of ready
    if result.definition_of_ready:
        items = "".join(
            f"<li>&#9744; {_CONFIDENCE_BADGE.get(i.confidence, '')} {_esc(i.text)}</li>"
            for i in result.definition_of_ready
        )
        parts.append(f"<h2>Definition of Ready</h2><ul>{items}</ul>")

    # Definition of done
    if result.definition_of_done:
        items = "".join(
            f"<li>&#9744; {_CONFIDENCE_BADGE.get(i.confidence, '')} {_esc(i.text)}</li>"
            for i in result.definition_of_done
        )
        parts.append(f"<h2>Definition of Done</h2><ul>{items}</ul>")

    # Odoo-specific notes
    if result.odoo_notes:
        items = "".join(
            f"<li>{_CONFIDENCE_BADGE.get(i.confidence, '')} {_esc(i.text)}</li>"
            for i in result.odoo_notes
        )
        parts.append(f"<h2>Odoo-Specific Notes</h2><ul>{items}</ul>")

    parts.append("<p><em>Generated by REVA</em></p>")
    return "\n".join(parts)


def _esc(text: str) -> str:
    """Minimal HTML escaping for user-supplied strings."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
