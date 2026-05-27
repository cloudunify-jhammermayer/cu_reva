"""Tests for FindingVerifier.is_resolved."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from reva.finding_verifier import FindingVerifier, StoredFinding
from reva.types import ClaudeResponse


def _make_verifier(resolved: bool = True, raise_exc: Exception | None = None) -> FindingVerifier:
    claude = MagicMock()
    if raise_exc:
        claude.review.side_effect = raise_exc
    else:
        claude.review.return_value = ClaudeResponse(
            model="claude-sonnet-4-6",
            stop_reason="tool_use",
            tool_use_input={"resolved": resolved, "reason": "test reason"},
        )
    return FindingVerifier(claude)


def _finding() -> StoredFinding:
    return StoredFinding(
        file_path="custom_addons/foo.py",
        line_start=42,
        title="Missing null check",
        body="The `user` variable may be None here.",
        severity="major",
        category="bug",
    )


def test_is_resolved_returns_true_when_claude_says_resolved():
    verifier = _make_verifier(resolved=True)
    assert verifier.is_resolved(_finding(), "def foo():\n    pass\n") is True


def test_is_resolved_returns_false_when_claude_says_not_resolved():
    verifier = _make_verifier(resolved=False)
    assert verifier.is_resolved(_finding(), "def foo():\n    x = user.name\n") is False


def test_is_resolved_raises_on_api_error():
    from reva.errors import TransientError
    verifier = _make_verifier(raise_exc=TransientError("rate limited"))
    with pytest.raises(TransientError):
        verifier.is_resolved(_finding(), "content")
