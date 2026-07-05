"""Claude tool definition for structured timesheet wording review submission."""

from __future__ import annotations

from typing import Any

from reva.tool_schema import require_no_extra_properties
from reva.types import TimesheetChunkResult

TIMESHEET_TOOL_NAME = "submit_timesheet_review"

_TOOL_DESCRIPTION = (
    "Submit your review of the timesheet lines. You MUST call this tool exactly "
    "once with one result per line_id you were given. Do not write any free-form "
    "response; the worker only reads the tool input."
)


def build_timesheet_tool_schema() -> dict[str, Any]:
    """Return the Anthropic tool definition for submit_timesheet_review."""
    schema = TimesheetChunkResult.model_json_schema()
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {"results": schema["properties"]["results"]},
        "required": ["results"],
        "additionalProperties": False,
    }
    if "$defs" in schema:
        input_schema["$defs"] = schema["$defs"]
    input_schema = require_no_extra_properties(input_schema)
    return {
        "name": TIMESHEET_TOOL_NAME,
        "description": _TOOL_DESCRIPTION,
        "strict": True,
        "input_schema": input_schema,
    }


def timesheet_tool_choice() -> dict[str, Any]:
    """Tool-choice value that forces Claude to call submit_timesheet_review."""
    return {"type": "tool", "name": TIMESHEET_TOOL_NAME}
