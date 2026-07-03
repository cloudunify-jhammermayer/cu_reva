"""Claude tool definition for structured learned-memory distillation."""

from __future__ import annotations

from typing import Any

from reva.types import ReviewMemoryPlan

REVIEW_MEMORY_TOOL_NAME = "submit_review_memory"

_TOOL_DESCRIPTION = (
    "Submit the distilled per-repo review guidance. You MUST call this tool "
    "exactly once. Pass `items` as a structured JSON array of objects — never "
    "as a JSON-encoded string. Return an empty array when the evidence does not "
    "support any durable guidance. Do not write any free-form response."
)


def build_review_memory_tool_schema() -> dict[str, Any]:
    """Anthropic tool definition for submit_review_memory, derived from
    ReviewMemoryPlan so the contract can't drift from the Python types."""
    schema = ReviewMemoryPlan.model_json_schema()
    properties = {k: v for k, v in schema.get("properties", {}).items() if k == "items"}
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": ["items"],
        "additionalProperties": False,
    }
    if "$defs" in schema:
        input_schema["$defs"] = schema["$defs"]
    return {
        "name": REVIEW_MEMORY_TOOL_NAME,
        "description": _TOOL_DESCRIPTION,
        "input_schema": input_schema,
    }


def review_memory_tool_choice() -> dict[str, Any]:
    return {"type": "tool", "name": REVIEW_MEMORY_TOOL_NAME}
