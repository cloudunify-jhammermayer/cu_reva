"""Claude tool definition for structured ticket analysis submission."""

from __future__ import annotations

from typing import Any

from reva.tool_schema import require_no_extra_properties
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
        "odoo_notes",
        "standard_coverage",
        "existing_customizations",
        "estimates",
    }
    properties = {k: v for k, v in schema.get("properties", {}).items() if k in allowed}

    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": [
            "summary",
            "missing_info",
            "odoo_notes",
            "standard_coverage",
            "existing_customizations",
            "estimates",
        ],
        "additionalProperties": False,
    }

    if "$defs" in schema:
        input_schema["$defs"] = schema["$defs"]
    input_schema = require_no_extra_properties(input_schema)

    # Neither field is the model's to fill: anchor_confidence is computed from
    # anchor distance after the call, dropped_drivers is bookkeeping the
    # validator writes. Leaving either in the schema would invite the model to
    # supply a value code always overwrites — wasted tokens and a misleading
    # contract. Both carry defaults, so Pydantic never lists them in
    # `required`; dropping the properties is enough.
    story_def = input_schema.get("$defs", {}).get("StoryEstimate")
    if isinstance(story_def, dict):
        for name in ("anchor_confidence", "dropped_drivers"):
            story_def.get("properties", {}).pop(name, None)

    return {
        "name": TICKET_TOOL_NAME,
        "description": _TOOL_DESCRIPTION,
        # Strict structured outputs: the API validates tool input against the
        # schema server-side, so list-as-JSON-string drift can't reach us —
        # but only on structured-outputs-capable models (Sonnet 5, Opus 4.8,
        # Haiku 4.5, ...). Older models (e.g. Sonnet 4.6) silently ignore the
        # flag; the runner's one-shot malformed-output retry is the backstop.
        "strict": True,
        "input_schema": input_schema,
    }


def ticket_tool_choice() -> dict[str, Any]:
    """Tool-choice value that forces Claude to call submit_ticket_analysis."""
    return {"type": "tool", "name": TICKET_TOOL_NAME}
