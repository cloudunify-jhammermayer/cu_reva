"""Helpers for Anthropic tool input schemas."""

from __future__ import annotations

from typing import Any

_UNSUPPORTED_TOOL_SCHEMA_KEYS = {
    # Anthropic's strict tool-schema subset rejects array cardinality keywords.
    # Keep those constraints in the Pydantic models; enforce after tool output.
    "maxItems",
    "minItems",
}


def require_no_extra_properties(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a copy where every object schema rejects extra properties.

    Anthropic's strict tool validation requires `additionalProperties: false`
    not only on the top-level input object, but also on nested object schemas
    emitted by Pydantic under `$defs`.
    """
    copied = {
        key: value
        for key, value in schema.items()
        if key not in _UNSUPPORTED_TOOL_SCHEMA_KEYS
    }
    if copied.get("type") == "object":
        copied.setdefault("additionalProperties", False)
    for key in ("properties", "$defs"):
        value = copied.get(key)
        if isinstance(value, dict):
            copied[key] = {
                name: require_no_extra_properties(child)
                if isinstance(child, dict) else child
                for name, child in value.items()
            }
    items = copied.get("items")
    if isinstance(items, dict):
        copied["items"] = require_no_extra_properties(items)
    for key in ("anyOf", "oneOf", "allOf"):
        value = copied.get(key)
        if isinstance(value, list):
            copied[key] = [
                require_no_extra_properties(item) if isinstance(item, dict) else item
                for item in value
            ]
    return copied
