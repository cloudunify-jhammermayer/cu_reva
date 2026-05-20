"""Content sanity tests for the on-disk prompts/ directory.

Verifies that PromptBuilder can load the real files and that the major
template placeholders survive the read/format cycle.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reva.prompt_builder import PromptBuilder

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


@pytest.fixture()
def builder() -> PromptBuilder:
    return PromptBuilder(prompts_dir=str(PROMPTS_DIR))


def test_prompts_dir_exists():
    assert PROMPTS_DIR.is_dir(), f"prompts/ missing at {PROMPTS_DIR}"
    for name in ("system.md", "diff_review.md", "deep_review.md", "odoo19.md", "CHANGELOG.md"):
        assert (PROMPTS_DIR / name).is_file(), f"prompts/{name} missing"


def test_system_blocks_baseline(builder):
    blocks = builder.build_system_blocks({}, claude_md=None)
    assert len(blocks) == 1
    assert blocks[0]["type"] == "text"
    assert "REVA" in blocks[0]["text"]
    assert "submit_review" in blocks[0]["text"]
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}


def test_system_blocks_with_odoo(builder):
    blocks = builder.build_system_blocks({"framework": "odoo"}, claude_md=None)
    assert len(blocks) == 2
    assert "Odoo 19" in blocks[1]["text"]
    assert blocks[1]["cache_control"] == {"type": "ephemeral"}


def test_system_blocks_with_claude_md(builder):
    blocks = builder.build_system_blocks({}, claude_md="This module handles money.")
    assert len(blocks) == 2
    assert "Repository-Specific Instructions" in blocks[1]["text"]
    assert "handles money" in blocks[1]["text"]


def test_system_blocks_full_set(builder):
    blocks = builder.build_system_blocks(
        {"framework": "odoo", "custom_instructions": "Be strict on currency_id."},
        claude_md="CLAUDE.md content here.",
    )
    # system + odoo + claude_md + custom = 4
    assert len(blocks) == 4
    texts = [b["text"] for b in blocks]
    assert any("REVA" in t for t in texts)
    assert any("Odoo 19" in t for t in texts)
    assert any("Repository-Specific Instructions" in t for t in texts)
    assert any("currency_id" in t for t in texts)


def test_get_version_returns_v1_0(builder):
    assert builder.get_version() == "v1.0"


def test_diff_review_template_renders(builder):
    out = builder.build_user_prompt(
        mode="diff",
        pr_title="Add foo",
        pr_body="Implements feature X.",
        diff="@@ -1 +1 @@\n-old\n+new",
        changed_files=["x.py", "y.py"],
        base_branch="main",
        head_branch="feat/foo",
    )
    assert "Add foo" in out
    assert "Implements feature X" in out
    assert "@@ -1 +1 @@" in out
    assert "- x.py" in out
    assert "- y.py" in out
    assert "main" in out and "feat/foo" in out
    assert "submit_review" in out


def test_deep_review_template_adds_deep_sections(builder):
    out = builder.build_user_prompt(
        mode="deep",
        pr_title="Refactor auth",
        pr_body="",
        diff="@@ -1 +1 @@\n-old\n+new",
        changed_files=["auth.py"],
        base_branch="main",
        head_branch="refactor/auth",
    )
    assert "deep review" in out.lower()
    assert "Architectural impact" in out or "architectural" in out.lower()
    assert "Cross-file" in out or "cross-file" in out.lower()
