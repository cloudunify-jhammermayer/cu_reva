"""Claude tool definition for structured ticket issue planning."""

from __future__ import annotations

from typing import Any

from reva.tool_schema import require_no_extra_properties
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

    properties = {
        k: v for k, v in schema.get("properties", {}).items() if k in ("issues", "summary")
    }

    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": ["summary", "issues"],
        "additionalProperties": False,
    }

    if "$defs" in schema:
        input_schema["$defs"] = schema["$defs"]
    input_schema = require_no_extra_properties(input_schema)

    # Strict structured output lets Claude omit any non-required field —
    # fields with Pydantic defaults (acceptance_criteria, type) came back
    # missing in production plans. Require every issue field in the tool
    # schema; the model defaults stay lenient for persisted plans. Except
    # anchor_ref/complexity_drivers: an unanchored issue is a valid answer,
    # so those two stay in `properties` (settable) but out of `required`.
    item = input_schema.get("$defs", {}).get("TicketIssueItem")
    if isinstance(item, dict) and "properties" in item:
        _optional = {"anchor_ref", "complexity_drivers"}
        item["required"] = [k for k in item["properties"] if k not in _optional]

    return {
        "name": TICKET_ISSUE_TOOL_NAME,
        "description": _TOOL_DESCRIPTION,
        "strict": True,
        "input_schema": input_schema,
    }


def ticket_issue_tool_choice() -> dict[str, Any]:
    """Tool-choice value that forces Claude to call submit_ticket_issues."""
    return {"type": "tool", "name": TICKET_ISSUE_TOOL_NAME}
