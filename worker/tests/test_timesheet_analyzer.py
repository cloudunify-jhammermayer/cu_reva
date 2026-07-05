"""Tests for TimesheetAnalyzer prompt assembly and validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from reva.errors import PermanentError
from reva.timesheet_analyzer import TimesheetAnalyzer
from reva.timesheet_tool import TIMESHEET_TOOL_NAME
from reva.types import ClaudeResponse, TimesheetLine

PROMPTS_DIR = str(Path(__file__).resolve().parents[2] / "prompts")


@dataclass
class FakeClaude:
    tool_use_input: dict | None = None
    calls: list[dict] = field(default_factory=list)

    def review(self, system_blocks, user_prompt, tools, tool_choice, model=None, max_tokens=8192):
        self.calls.append({
            "system_blocks": system_blocks,
            "user_prompt": user_prompt,
            "tools": tools,
            "tool_choice": tool_choice,
            "max_tokens": max_tokens,
        })
        return ClaudeResponse(
            model="claude-sonnet-4-6",
            stop_reason="tool_use",
            tool_use_input=self.tool_use_input,
            input_tokens=100,
            output_tokens=50,
            cache_read_tokens=0,
            cache_creation_tokens=0,
        )


def _lines() -> list[TimesheetLine]:
    return [
        TimesheetLine(
            line_id=1,
            task_name="Reports",
            project_name="ACME",
            user_name="Jo",
            user_role="developer",
            description="fixed stupid bug",
        ),
        TimesheetLine(
            line_id=2,
            task_name="Workshop",
            project_name="ACME",
            user_name="Sam",
            user_role="consultant",
            description="Meeting",
        ),
    ]


def _ok_input() -> dict:
    return {"results": [
        {"line_id": 1, "status": "rewritten", "updated_desc": "Fixed report layout bug"},
        {"line_id": 2, "status": "needs_human", "reason": "Beschreibung zu unkonkret"},
    ]}


def test_returns_validated_results():
    claude = FakeClaude(tool_use_input=_ok_input())
    response, results = TimesheetAnalyzer(claude=claude, prompts_dir=PROMPTS_DIR).analyze_chunk(
        _lines(),
        flagged_words=["stupid"],
    )
    assert response.model == "claude-sonnet-4-6"
    assert [result.line_id for result in results] == [1, 2]
    assert results[0].status == "rewritten"


def test_system_blocks_prompt_file_plus_flagged_words_cached():
    claude = FakeClaude(tool_use_input=_ok_input())
    TimesheetAnalyzer(claude=claude, prompts_dir=PROMPTS_DIR).analyze_chunk(
        _lines(),
        flagged_words=["stupid", "dumm"],
    )
    blocks = claude.calls[0]["system_blocks"]
    assert "Timesheet Wording Review" in blocks[0]["text"]
    assert "stupid" in blocks[-1]["text"] and "dumm" in blocks[-1]["text"]
    assert blocks[-1]["cache_control"] == {"type": "ephemeral"}
    assert all("cache_control" not in block for block in blocks[:-1])


def test_no_flagged_words_block_when_list_empty():
    claude = FakeClaude(tool_use_input=_ok_input())
    TimesheetAnalyzer(claude=claude, prompts_dir=PROMPTS_DIR).analyze_chunk(
        _lines(),
        flagged_words=[],
    )
    blocks = claude.calls[0]["system_blocks"]
    assert len(blocks) == 1
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}


def test_user_prompt_fences_untrusted_fields():
    claude = FakeClaude(tool_use_input=_ok_input())
    TimesheetAnalyzer(claude=claude, prompts_dir=PROMPTS_DIR).analyze_chunk(
        _lines(),
        flagged_words=[],
    )
    prompt = claude.calls[0]["user_prompt"]
    assert "UNTRUSTED" in prompt
    assert "<line_" in prompt and "</line_" in prompt
    assert "line_id: 1" in prompt and "role: developer" in prompt
    assert "fixed stupid bug" in prompt


def test_forces_tool_choice():
    claude = FakeClaude(tool_use_input=_ok_input())
    TimesheetAnalyzer(claude=claude, prompts_dir=PROMPTS_DIR).analyze_chunk(
        _lines(),
        flagged_words=[],
    )
    assert claude.calls[0]["tool_choice"] == {"type": "tool", "name": TIMESHEET_TOOL_NAME}
    assert claude.calls[0]["max_tokens"] == 16384


def test_missing_tool_call_is_permanent():
    claude = FakeClaude(tool_use_input=None)
    with pytest.raises(PermanentError):
        TimesheetAnalyzer(claude=claude, prompts_dir=PROMPTS_DIR).analyze_chunk(
            _lines(),
            flagged_words=[],
        )


def test_invalid_tool_input_is_permanent():
    claude = FakeClaude(tool_use_input={"results": [{"line_id": 1, "status": "rewritten"}]})
    with pytest.raises(PermanentError):
        TimesheetAnalyzer(claude=claude, prompts_dir=PROMPTS_DIR).analyze_chunk(
            _lines(),
            flagged_words=[],
        )
