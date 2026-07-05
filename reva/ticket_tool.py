"""Claude tool definition for structured ticket analysis submission."""

from __future__ import annotations

from typing import Any

from reva.types import TicketAnalysisResult

TICKET_TOOL_NAME = "submit_ticket_analysis"

_TOOL_DESCRIPTION = (
    "Submit your ticket analysis. You MUST call this tool exactly once to return "
    "your structured findings. Do not write any free-form response — the worker "
    "only reads the tool input."
)


def build_ticket_tool_schema() -> dict[str, Any]:
    """Return the Anthropic tool definition for submit_ticket_analysis.

    Derived from TicketAnalysisResult so the contract cannot drift from the
    Python types.
    """
    schema = TicketAnalysisResult.model_json_schema()

    allowed = {
        "summary",
        "missing_info",
        "acceptance_criteria",
        "test_cases",
        "definition_of_ready",
        "definition_of_done",
        "odoo_notes",
    }
    properties = {k: v for k, v in schema.get("properties", {}).items() if k in allowed}

    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": ["summary", "missing_info", "acceptance_criteria", "test_cases",
                     "definition_of_ready", "definition_of_done", "odoo_notes"],
        "additionalProperties": False,
    }

    if "$defs" in schema:
        input_schema["$defs"] = schema["$defs"]

    return {
        "name": TICKET_TOOL_NAME,
        "description": _TOOL_DESCRIPTION,
        # Strict structured outputs: the API validates tool input against the
        # schema server-side, so list-as-JSON-string drift can't reach us.
        "strict": True,
        "input_schema": input_schema,
    }


def ticket_tool_choice() -> dict[str, Any]:
    """Tool-choice value that forces Claude to call submit_ticket_analysis."""
    return {"type": "tool", "name": TICKET_TOOL_NAME}
