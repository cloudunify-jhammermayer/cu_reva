"""Every Messages-API forced-tool definition opts into strict structured output."""

from __future__ import annotations

from reva.finding_verifier import _VERIFY_PRESENT_TOOL, _VERIFY_TOOL
from reva.ticket_issue_tool import build_ticket_issue_tool_schema
from reva.ticket_tool import build_ticket_tool_schema


def test_ticket_tool_is_strict():
    assert build_ticket_tool_schema()["strict"] is True


def test_ticket_issue_tool_is_strict():
    assert build_ticket_issue_tool_schema()["strict"] is True


def test_verifier_tools_are_strict():
    assert _VERIFY_TOOL["strict"] is True
    assert _VERIFY_PRESENT_TOOL["strict"] is True
