"""Claude tool definition for structured support-answer submission."""

from __future__ import annotations

from typing import Any

from reva.tool_schema import require_no_extra_properties
from reva.types import SupportAnswerResult

SUPPORT_TOOL_NAME = "submit_support_answer"

_TOOL_DESCRIPTION = (
    "Submit your support answer draft. You MUST call this tool exactly once to "
    "return your structured result. Do not write any free-form response — the "
    "worker only reads the tool input."
)


def build_support_tool_schema() -> dict[str, Any]:
    """Return the Anthropic tool definition for submit_support_answer.

    Derived from SupportAnswerResult so the contract cannot drift from the
    Python types.
    """
    schema = SupportAnswerResult.model_json_schema()

    allowed = [
        "request_kind",
        "answer_status",
        "answer",
        "cannot_answer_reason",
        "open_questions",
        "sources",
        "handoff",
        "language",
        "confidence",
    ]
    properties = {k: v for k, v in schema.get("properties", {}).items() if k in allowed}

    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": allowed,
        "additionalProperties": False,
    }

    if "$defs" in schema:
        input_schema["$defs"] = schema["$defs"]
    input_schema = require_no_extra_properties(input_schema)

    return {
        "name": SUPPORT_TOOL_NAME,
        "description": _TOOL_DESCRIPTION,
        # Strict structured outputs: the API validates tool input against the
        # schema server-side, so list-as-JSON-string drift can't reach us —
        # but only on structured-outputs-capable models (Sonnet 5, Opus 4.8,
        # Haiku 4.5, ...). Older models (e.g. Sonnet 4.6) silently ignore the
        # flag; the runner's one-shot malformed-output retry is the backstop.
        "strict": True,
        "input_schema": input_schema,
    }


def support_tool_choice() -> dict[str, Any]:
    """Tool-choice value that forces Claude to call submit_support_answer."""
    return {"type": "tool", "name": SUPPORT_TOOL_NAME}
