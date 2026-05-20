"""Claude tool definition for structured review submission.

We force Claude to return findings via a tool_use call rather than free-form
JSON. The schema is derived from the pydantic ReviewResult model so the
worker's validation and Claude's contract cannot drift apart.
"""

from __future__ import annotations

from typing import Any

from reva.types import ReviewResult

REVIEW_TOOL_NAME = "submit_review"

_TOOL_DESCRIPTION = (
    "Submit your code review. You MUST call this tool exactly once to return "
    "your findings. Do not write any free-form response — the worker only "
    "reads the tool input."
)


def build_review_tool_schema() -> dict[str, Any]:
    """Return the Anthropic tool definition for submit_review.

    Derived from the ReviewResult pydantic model. Internal-only fields
    (timings, tokens, cost, error) are stripped — Claude only fills in the
    review content (summary, risk_level, findings).
    """
    schema = ReviewResult.model_json_schema()

    # Restrict Claude's contract to the content fields it must produce.
    allowed = {"summary", "risk_level", "findings"}
    properties = {k: v for k, v in schema.get("properties", {}).items() if k in allowed}

    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": ["summary", "risk_level", "findings"],
        "additionalProperties": False,
    }

    # Inline the Finding sub-schema if pydantic emitted $defs references.
    if "$defs" in schema:
        input_schema["$defs"] = schema["$defs"]

    return {
        "name": REVIEW_TOOL_NAME,
        "description": _TOOL_DESCRIPTION,
        "input_schema": input_schema,
    }


def tool_choice_force_submit() -> dict[str, Any]:
    """Tool-choice value that forces Claude to call submit_review."""
    return {"type": "tool", "name": REVIEW_TOOL_NAME}
