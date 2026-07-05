"""Schema + validation tests for the submit_timesheet_review tool contract."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from reva.timesheet_tool import (
    TIMESHEET_TOOL_NAME,
    build_timesheet_tool_schema,
    timesheet_tool_choice,
)
from reva.types import TimesheetChunkResult, TimesheetLine, TimesheetLineResult


def test_tool_schema_shape():
    schema = build_timesheet_tool_schema()
    assert schema["name"] == TIMESHEET_TOOL_NAME == "submit_timesheet_review"
    inp = schema["input_schema"]
    assert inp["required"] == ["results"]
    assert inp["additionalProperties"] is False
    assert "results" in inp["properties"]


def test_tool_choice_forces_tool():
    assert timesheet_tool_choice() == {"type": "tool", "name": TIMESHEET_TOOL_NAME}


def test_result_ok_needs_no_extras():
    result = TimesheetLineResult(line_id=1, status="ok")
    assert result.updated_desc is None and result.reason is None


def test_result_rewritten_requires_updated_desc():
    with pytest.raises(ValidationError):
        TimesheetLineResult(line_id=1, status="rewritten")
    with pytest.raises(ValidationError):
        TimesheetLineResult(line_id=1, status="rewritten", updated_desc="   ")
    result = TimesheetLineResult(
        line_id=1,
        status="rewritten",
        updated_desc="Implemented report",
    )
    assert result.updated_desc == "Implemented report"


def test_result_needs_human_requires_reason():
    with pytest.raises(ValidationError):
        TimesheetLineResult(line_id=1, status="needs_human")
    result = TimesheetLineResult(line_id=1, status="needs_human", reason="too thin")
    assert result.reason == "too thin"


def test_result_rejects_unknown_status():
    with pytest.raises(ValidationError):
        TimesheetLineResult(line_id=1, status="skipped")


def test_chunk_result_validates_from_tool_input():
    payload = {
        "results": [
            {"line_id": 1, "status": "ok"},
            {
                "line_id": 2,
                "status": "rewritten",
                "updated_desc": "Konzeption Berichtswesen",
            },
            {
                "line_id": 3,
                "status": "needs_human",
                "reason": "keine Taetigkeit erkennbar",
            },
        ]
    }
    chunk = TimesheetChunkResult.model_validate(payload)
    assert [result.line_id for result in chunk.results] == [1, 2, 3]


def test_line_caps_description_length():
    with pytest.raises(ValidationError):
        TimesheetLine(
            line_id=1,
            task_name="t",
            project_name="p",
            user_name="u",
            user_role="developer",
            description="x" * 4001,
        )


def test_line_rejects_unknown_role():
    with pytest.raises(ValidationError):
        TimesheetLine(
            line_id=1,
            task_name="t",
            project_name="p",
            user_name="u",
            user_role="manager",
            description="d",
        )
