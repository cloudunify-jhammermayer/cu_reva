"""Every Messages-API forced-tool definition opts into strict structured output."""

from __future__ import annotations

from reva.finding_verifier import _VERIFY_PRESENT_TOOL, _VERIFY_TOOL
from reva.review_memory_tool import build_review_memory_tool_schema
from reva.ticket_issue_tool import build_ticket_issue_tool_schema
from reva.ticket_tool import build_ticket_tool_schema
from reva.timesheet_tool import build_timesheet_tool_schema


def _assert_strict_objects(schema: dict, path: str = "input_schema") -> None:
    assert "maxItems" not in schema, path
    assert "minItems" not in schema, path
    if schema.get("type") == "object":
        assert schema.get("additionalProperties") is False, path
    for key in ("properties", "$defs"):
        value = schema.get(key)
        if isinstance(value, dict):
            for name, child in value.items():
                if isinstance(child, dict):
                    _assert_strict_objects(child, f"{path}.{key}.{name}")
    items = schema.get("items")
    if isinstance(items, dict):
        _assert_strict_objects(items, f"{path}.items")
    for key in ("anyOf", "oneOf", "allOf"):
        value = schema.get(key)
        if isinstance(value, list):
            for index, child in enumerate(value):
                if isinstance(child, dict):
                    _assert_strict_objects(child, f"{path}.{key}[{index}]")


def test_ticket_tool_is_strict():
    assert build_ticket_tool_schema()["strict"] is True
    _assert_strict_objects(build_ticket_tool_schema()["input_schema"])


def test_ticket_issue_tool_is_strict():
    assert build_ticket_issue_tool_schema()["strict"] is True
    _assert_strict_objects(build_ticket_issue_tool_schema()["input_schema"])


def test_timesheet_tool_objects_reject_extras():
    _assert_strict_objects(build_timesheet_tool_schema()["input_schema"])


def test_review_memory_tool_objects_reject_extras():
    _assert_strict_objects(build_review_memory_tool_schema()["input_schema"])


def test_verifier_tools_are_strict():
    assert _VERIFY_TOOL["strict"] is True
    assert _VERIFY_PRESENT_TOOL["strict"] is True
