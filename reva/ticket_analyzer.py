"""Pure ticket analysis: calls Claude and returns a validated TicketAnalysisResult.

No side effects — no DB writes, no HTTP calls to Odoo.
The caller (ticket_runner.py) owns persistence and the callback POST.
HTML formatting lives in ticket_formatter.py.
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
