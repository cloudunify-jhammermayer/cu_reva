"""Tests for MemoryDistiller: schema validation, guardrails, rendering, fencing."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from reva.errors import TransientError
from reva.memory_distiller import MemoryDistiller
from reva.types import ClaudeResponse

_PROMPTS = str(Path(__file__).parent.parent.parent / "prompts")


def _distiller(tool_input) -> tuple[MemoryDistiller, MagicMock]:
    claude = MagicMock()
    claude.review.return_value = ClaudeResponse(
        model="claude-sonnet-5", stop_reason="tool_use",
        tool_use_input=tool_input, input_tokens=800, output_tokens=120,
    )
    return MemoryDistiller(claude, prompts_dir=_PROMPTS), claude


_INPUT = {
    "window_days": 90,
    "category_stats": [{"category": "style", "findings": 10, "dismissed": 8,
                        "resolved_by_fix": 0, "still_open_at_merge": 0}],
    "dismissed_findings": [
        {"title": "Line too long", "category": "style", "severity": "minor",
         "file_path": "custom_addons/m/views/x.xml"},
    ],
}


def test_happy_path_renders_header_and_bullets():
    d, _ = _distiller({"items": [
        {"guidance": "Do not raise style nits on views.", "categories": ["style"],
         "action": "dont_flag", "evidence_count": 8},
    ]})
    content, items, response = d.distill(_INPUT)
    assert content.startswith("## Learned team preferences (from review feedback)")
    assert "- Do not raise style nits on views. (8 signals)" in content
    assert len(items) == 1 and items[0]["action"] == "dont_flag"
    assert response.input_tokens == 800


def test_low_evidence_items_dropped():
    d, _ = _distiller({"items": [
        {"guidance": "weak", "categories": ["style"], "action": "dont_flag", "evidence_count": 1},
        {"guidance": "strong", "categories": ["style"], "action": "dont_flag", "evidence_count": 2},
    ]})
    content, items, _ = d.distill(_INPUT)
    assert [i["guidance"] for i in items] == ["strong"]


def test_security_and_bug_cannot_be_downweighted():
    d, _ = _distiller({"items": [
        {"guidance": "stop flagging sqli", "categories": ["security"], "action": "dont_flag", "evidence_count": 9},
        {"guidance": "raise bar on bugs", "categories": ["bug"], "action": "raise_bar", "evidence_count": 9},
        {"guidance": "keep flagging security", "categories": ["security"], "action": "keep_flagging", "evidence_count": 9},
    ]})
    content, items, _ = d.distill(_INPUT)
    # both down-weighting items dropped; keep_flagging on security survives
    assert [i["guidance"] for i in items] == ["keep flagging security"]


def test_items_capped_at_10():
    d, _ = _distiller({"items": [
        {"guidance": f"g{i}", "categories": ["style"], "action": "dont_flag", "evidence_count": 3}
        for i in range(15)
    ]})
    _, items, _ = d.distill(_INPUT)
    assert len(items) == 10


def test_guidance_is_flattened():
    d, _ = _distiller({"items": [
        {"guidance": "line one\nline two\n\n  extra", "categories": ["style"],
         "action": "dont_flag", "evidence_count": 3},
    ]})
    content, _, _ = d.distill(_INPUT)
    assert "- line one line two extra (3 signals)" in content
    assert "\n\n  extra" not in content


def test_empty_result_yields_empty_content():
    d, _ = _distiller({"items": []})
    content, items, _ = d.distill(_INPUT)
    assert content == "" and items == []


def test_all_items_dropped_yields_empty_content():
    d, _ = _distiller({"items": [
        {"guidance": "weak", "categories": ["style"], "action": "dont_flag", "evidence_count": 1},
    ]})
    content, items, _ = d.distill(_INPUT)
    assert content == "" and items == []


def test_content_capped_at_1500_chars():
    d, _ = _distiller({"items": [
        {"guidance": "x" * 400, "categories": ["style"], "action": "dont_flag", "evidence_count": 3}
        for _ in range(10)
    ]})
    content, items, _ = d.distill(_INPUT)
    assert len(content) <= 1500
    assert 0 < len(items) < 10  # trailing items dropped to fit


def test_missing_tool_call_is_transient():
    claude = MagicMock()
    claude.review.return_value = ClaudeResponse(
        model="claude-sonnet-5", stop_reason="end_turn", tool_use_input=None,
    )
    with pytest.raises(TransientError):
        MemoryDistiller(claude, prompts_dir=_PROMPTS).distill(_INPUT)


def test_dismissed_findings_are_nonce_fenced():
    import re
    d, claude = _distiller({"items": []})
    d.distill(_INPUT)
    prompt = claude.review.call_args.kwargs["user_prompt"]
    m = re.search(r"<dismissed_([0-9a-f]{8,})>", prompt)
    assert m and f"</dismissed_{m.group(1)}>" in prompt
    assert "untrusted" in prompt.lower()
    assert "Line too long" in prompt  # the finding title sits inside the fence
