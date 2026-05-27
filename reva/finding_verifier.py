"""Claude-based verification that a prior finding is still present in the current code."""

from __future__ import annotations

from dataclasses import dataclass

from reva.claude_client import ClaudeClient
from reva.errors import PermanentError
from reva.types import ContentBlock


@dataclass
class StoredFinding:
    file_path: str
    line_start: int | None
    title: str
    body: str
    severity: str
    category: str


_SYSTEM_PROMPT = """\
You are a code reviewer checking whether a previously reported issue has been fixed.
You will be given a finding from a prior code review and the current content of the file.
Determine whether the issue described in the finding is still present at or near the original location.
Be conservative: only mark resolved if you are confident the issue no longer exists.
If the file has been significantly restructured and you cannot locate the original code, mark it as unresolved.\
"""

_VERIFY_TOOL = {
    "name": "verify_finding",
    "description": "Report whether the finding is still present in the current file.",
    "input_schema": {
        "type": "object",
        "properties": {
            "resolved": {
                "type": "boolean",
                "description": "True if the issue is fixed, False if still present.",
            },
            "reason": {
                "type": "string",
                "description": "One sentence explaining the decision.",
            },
        },
        "required": ["resolved", "reason"],
    },
}

_TOOL_CHOICE = {"type": "tool", "name": "verify_finding"}


class FindingVerifier:
    def __init__(self, claude: ClaudeClient) -> None:
        self._claude = claude

    def is_resolved(self, finding: StoredFinding, file_content: str) -> bool:
        """Return True if the finding is no longer present in file_content.

        Raises TransientError / PermanentError on API failure (caller catches).
        """
        line_info = f" line {finding.line_start}" if finding.line_start else ""
        user_prompt = (
            f"## Finding\n"
            f"**Title:** {finding.title}\n"
            f"**Severity:** {finding.severity}\n"
            f"**Category:** {finding.category}\n"
            f"**Original location:** {finding.file_path}{line_info}\n"
            f"**Description:** {finding.body}\n\n"
            f"## Current file content\n```\n{file_content}\n```\n\n"
            f"Is this issue still present in the current file?"
        )
        system_blocks: list[ContentBlock] = [{"type": "text", "text": _SYSTEM_PROMPT}]
        response = self._claude.review(
            system_blocks=system_blocks,
            user_prompt=user_prompt,
            tools=[_VERIFY_TOOL],
            tool_choice=_TOOL_CHOICE,
            max_tokens=512,
        )
        if response.tool_use_input is None:
            raise PermanentError("FindingVerifier: Claude did not call verify_finding")
        return bool(response.tool_use_input.get("resolved", False))
