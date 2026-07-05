"""Claude tool definition for structured ticket issue planning."""

from __future__ import annotations

from typing import Any

from reva.types import TicketIssuePlan

TICKET_ISSUE_TOOL_NAME = "submit_ticket_issues"

_TOOL_DESCRIPTION = (
    "Submit your GitHub issue plan. You MUST call this tool exactly once to "
    "return the issues to create. Pass `issues` as a structured JSON array of "
    "objects — never as a JSON-encoded string. Do not write any free-form "
    "response — the worker only reads the tool input."
)


def build_ticket_issue_tool_schema() -> dict[str, Any]:
    """Return the Anthropic tool definition for submit_ticket_issues.

    Derived from TicketIssuePlan so the contract cannot drift from the
    Python types.
    """
    schema = TicketIssuePlan.model_json_schema()

    properties = {k: v for k, v in schema.get("properties", {}).items() if k == "issues"}

    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": ["issues"],
        "additionalProperties": False,
    }

    if "$defs" in schema:
        input_schema["$defs"] = schema["$defs"]

    return {
        "name": TICKET_ISSUE_TOOL_NAME,
        "description": _TOOL_DESCRIPTION,
        "strict": True,
        "input_schema": input_schema,
    }


def ticket_issue_tool_choice() -> dict[str, Any]:
    """Tool-choice value that forces Claude to call submit_ticket_issues."""
    return {"type": "tool", "name": TICKET_ISSUE_TOOL_NAME}
