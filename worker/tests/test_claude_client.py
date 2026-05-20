"""Tests for ClaudeClient.

Uses httpx.MockTransport to inject canned responses — no live API calls.
The recorded fixture in tests/fixtures/successful_review.json mirrors a
real Anthropic Messages API response shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from reva.claude_client import ANTHROPIC_VERSION, ClaudeClient
from reva.errors import PermanentError, TransientError
from reva.review_tool import (
    REVIEW_TOOL_NAME,
    build_review_tool_schema,
    tool_choice_force_submit,
)

FIXTURES = Path(__file__).parent / "fixtures"


# --- helpers ------------------------------------------------------------------


def _make_client(handler):
    """Build a ClaudeClient wired to a MockTransport handler."""
    transport = httpx.MockTransport(handler)
    return ClaudeClient(api_key="test-key", client=httpx.Client(transport=transport))


def _review_args():
    return {
        "system_blocks": [
            {"type": "text", "text": "You are REVA.", "cache_control": {"type": "ephemeral"}}
        ],
        "user_prompt": "Review this PR diff.",
        "tools": [build_review_tool_schema()],
        "tool_choice": tool_choice_force_submit(),
    }


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# --- happy path ---------------------------------------------------------------


def test_review_happy_path_parses_tool_use_and_usage():
    payload = _load_fixture("successful_review.json")

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=payload)

    client = _make_client(handler)
    result = client.review(**_review_args())

    assert result.tool_use_input is not None
    assert result.tool_use_input["risk_level"] == "critical"
    assert result.tool_use_input["findings"][0]["title"].startswith("SQL injection")

    # Usage propagated, including cache fields.
    assert result.input_tokens == 1234
    assert result.output_tokens == 256
    assert result.cache_creation_tokens == 300
    assert result.cache_read_tokens == 5000
    assert result.model == "claude-sonnet-4-6"
    assert result.stop_reason == "tool_use"

    # Request was shaped correctly.
    assert captured["headers"]["x-api-key"] == "test-key"
    assert captured["headers"]["anthropic-version"] == ANTHROPIC_VERSION
    assert captured["body"]["tool_choice"] == {"type": "tool", "name": REVIEW_TOOL_NAME}
    assert captured["body"]["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_review_cache_fields_default_to_zero_when_absent():
    payload = _load_fixture("successful_review.json")
    payload["usage"].pop("cache_read_input_tokens")
    payload["usage"].pop("cache_creation_input_tokens")

    client = _make_client(lambda req: httpx.Response(200, json=payload))
    result = client.review(**_review_args())

    assert result.cache_read_tokens == 0
    assert result.cache_creation_tokens == 0


# --- malformed 200 responses --------------------------------------------------


def test_review_raises_permanent_when_tool_use_block_missing():
    payload = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-6",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "I refuse to use the tool."}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    client = _make_client(lambda req: httpx.Response(200, json=payload))

    with pytest.raises(PermanentError) as exc_info:
        client.review(**_review_args())
    assert REVIEW_TOOL_NAME in str(exc_info.value)


def test_review_raises_permanent_when_tool_use_has_wrong_name():
    payload = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-6",
        "stop_reason": "tool_use",
        "content": [
            {"type": "tool_use", "id": "t1", "name": "some_other_tool", "input": {}}
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    client = _make_client(lambda req: httpx.Response(200, json=payload))

    with pytest.raises(PermanentError):
        client.review(**_review_args())


# --- HTTP status mapping ------------------------------------------------------


def test_review_maps_429_to_transient_with_retry_after():
    def handler(req):
        return httpx.Response(
            429,
            headers={"retry-after": "42"},
            json={"error": {"type": "rate_limit_error", "message": "slow down"}},
        )

    client = _make_client(handler)
    with pytest.raises(TransientError) as exc_info:
        client.review(**_review_args())
    assert exc_info.value.retry_after == 42


def test_review_maps_500_to_transient():
    client = _make_client(lambda req: httpx.Response(500, text="boom"))
    with pytest.raises(TransientError) as exc_info:
        client.review(**_review_args())
    assert exc_info.value.retry_after is None
    assert "500" in str(exc_info.value)


def test_review_maps_400_to_permanent():
    client = _make_client(lambda req: httpx.Response(400, text="bad input"))
    with pytest.raises(PermanentError) as exc_info:
        client.review(**_review_args())
    assert "400" in str(exc_info.value)


def test_review_maps_401_to_permanent():
    client = _make_client(lambda req: httpx.Response(401, text="bad key"))
    with pytest.raises(PermanentError):
        client.review(**_review_args())


# --- transport-level failures -------------------------------------------------


def test_review_maps_timeout_to_transient():
    def handler(req):
        raise httpx.ReadTimeout("read timeout", request=req)

    client = _make_client(handler)
    with pytest.raises(TransientError) as exc_info:
        client.review(**_review_args())
    assert "timed out" in str(exc_info.value)


def test_review_maps_connection_error_to_transient():
    def handler(req):
        raise httpx.ConnectError("dns fail", request=req)

    client = _make_client(handler)
    with pytest.raises(TransientError) as exc_info:
        client.review(**_review_args())
    assert "transport error" in str(exc_info.value)


# --- request shape ------------------------------------------------------------


def test_review_passes_overridden_model():
    payload = _load_fixture("successful_review.json")
    captured: dict = {}

    def handler(req):
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json=payload)

    client = _make_client(handler)
    client.review(**_review_args(), model="claude-opus-4-7", max_tokens=4096)

    assert captured["body"]["model"] == "claude-opus-4-7"
    assert captured["body"]["max_tokens"] == 4096


def test_review_defaults_to_sonnet_when_no_model_passed():
    payload = _load_fixture("successful_review.json")
    captured: dict = {}

    def handler(req):
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json=payload)

    client = _make_client(handler)
    client.review(**_review_args())

    assert captured["body"]["model"] == "claude-sonnet-4-6"
