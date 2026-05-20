"""Claude Messages API client.

Implements `ClaudeClient.review`. Pure HTTP — no retries (RQ owns those),
no SDK dependency. The client extracts the `submit_review` tool_use block
and normalizes token usage (including cache read/write counts).
"""

from __future__ import annotations

import httpx

from reva.errors import PermanentError, TransientError
from reva.review_tool import REVIEW_TOOL_NAME
from reva.types import ClaudeResponse, ContentBlock

DEFAULT_MODEL = "claude-sonnet-4-6"
DEEP_MODEL = "claude-opus-4-7"
ANTHROPIC_VERSION = "2023-06-01"
# Prompt caching is GA on the 2023-06-01 version; no beta header required.


class ClaudeClient:
    BASE_URL = "https://api.anthropic.com/v1/messages"

    def __init__(
        self,
        api_key: str,
        default_model: str = DEFAULT_MODEL,
        deep_model: str = DEEP_MODEL,
        timeout: float = 180.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.api_key = api_key
        self.default_model = default_model
        self.deep_model = deep_model
        self._client = client or httpx.Client(timeout=timeout)

    # ------------------------------------------------------------------ public

    def review(
        self,
        system_blocks: list[ContentBlock],
        user_prompt: str,
        tools: list[dict],
        tool_choice: dict,
        model: str | None = None,
        max_tokens: int = 8192,
    ) -> ClaudeResponse:
        """Call the Claude Messages API and return a normalized response.

        Raises:
            TransientError: on 429 / 5xx / network errors (retryable).
            PermanentError: on 4xx other than 429, or malformed responses.
        """
        body = {
            "model": model or self.default_model,
            "max_tokens": max_tokens,
            "system": list(system_blocks),
            "messages": [{"role": "user", "content": user_prompt}],
            "tools": tools,
            "tool_choice": tool_choice,
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

        try:
            response = self._client.post(self.BASE_URL, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise TransientError(f"Claude request timed out: {exc}") from exc
        except httpx.TransportError as exc:
            # Connection refused, DNS failure, read error mid-response, etc.
            raise TransientError(f"Claude transport error: {exc}") from exc

        if response.status_code != 200:
            raise _map_status_to_error(
                response.status_code,
                response.headers.get("retry-after"),
                response.text,
            )

        return _parse_success(response.json())

    def close(self) -> None:
        self._client.close()


# ------------------------------------------------------------------ internals


def _map_status_to_error(status_code: int, retry_after_header: str | None, body: str) -> Exception:
    """Map an HTTP status code to the appropriate worker exception."""
    snippet = body[:200]
    if status_code == 429 or status_code >= 500:
        retry_after = _parse_retry_after(retry_after_header)
        return TransientError(f"Claude {status_code}: {snippet}", retry_after=retry_after)
    return PermanentError(f"Claude {status_code}: {snippet}")


def _parse_retry_after(value: str | None) -> int | None:
    """Anthropic returns Retry-After as integer seconds. Be defensive."""
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _parse_success(payload: dict) -> ClaudeResponse:
    """Extract tool_use input and usage from a 200 response.

    A successful tool_use call has stop_reason="tool_use" and at least one
    content block with type="tool_use". We require the block's `name` to
    match REVIEW_TOOL_NAME — anything else means Claude went off-script
    (a permanent failure for this contract).
    """
    content = payload.get("content") or []
    tool_use_input: dict | None = None
    for block in content:
        if block.get("type") == "tool_use" and block.get("name") == REVIEW_TOOL_NAME:
            tool_use_input = block.get("input")
            break

    if tool_use_input is None:
        raise PermanentError(
            f"Claude response missing tool_use[{REVIEW_TOOL_NAME}] block "
            f"(stop_reason={payload.get('stop_reason')!r})"
        )

    usage = payload.get("usage") or {}
    return ClaudeResponse(
        model=payload.get("model", ""),
        stop_reason=payload.get("stop_reason"),
        tool_use_input=tool_use_input,
        input_tokens=usage.get("input_tokens", 0) or 0,
        output_tokens=usage.get("output_tokens", 0) or 0,
        cache_read_tokens=usage.get("cache_read_input_tokens", 0) or 0,
        cache_creation_tokens=usage.get("cache_creation_input_tokens", 0) or 0,
    )
