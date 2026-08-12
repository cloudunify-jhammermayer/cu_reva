"""Claude Messages API client.

Implements `ClaudeClient.review`. Pure HTTP — no retries (RQ owns those),
no SDK dependency. The client extracts any tool_use block from the response
and normalizes token usage (including cache read/write counts). Callers are
responsible for validating the tool name and input schema.
"""

from __future__ import annotations

import httpx

from reva.config import DEFAULT_MODEL, DEEP_MODEL
from reva.errors import PermanentError, TransientError
from reva.types import ClaudeResponse, ContentBlock

ANTHROPIC_VERSION = "2023-06-01"
# Prompt caching is GA on the 2023-06-01 version; no beta header required.


def _build_user_content(
    user_prompt: str,
    images: list[tuple[str, str, str]] | None,
    images_preamble: str | None = None,
) -> str | list[dict]:
    """The user turn's content: a plain string when there are no images.

    With images, the turn becomes a block list laid out images-FIRST, each
    preceded by its label text block, with the prompt last. Two reasons for that
    order: it is the documented best-performing layout for the Messages API, and
    the labels are what let the model tie a block to the [Image N] marker its
    sender left in the prompt text.

    `images_preamble` is caller-authored text placed ahead of every image — the
    hook callers use to frame untrusted image bytes before the model reads them.
    Kept content-free here so the wording lives with the feature that needs it.

    Caching is unaffected — images ride in the user turn, i.e. after the last
    cache_control breakpoint, which lives in `system`. Image tokens are ordinary
    input tokens, so usage/cost accounting needs no special case either.
    """
    if not images:
        return user_prompt
    blocks: list[dict] = []
    if images_preamble:
        blocks.append({"type": "text", "text": images_preamble})
    for label, media_type, data in images:
        blocks.append({"type": "text", "text": label})
        blocks.append(
            {
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": data},
            }
        )
    blocks.append({"type": "text", "text": user_prompt})
    return blocks


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
        thinking: dict | None = None,
        images: list[tuple[str, str, str]] | None = None,
        images_preamble: str | None = None,
    ) -> ClaudeResponse:
        """Call the Claude Messages API and return a normalized response.

        `thinking` is omitted from the body unless a caller passes one, so every
        existing call site keeps its current behaviour. Note what omitting it
        MEANS on the current default model: Sonnet 5 runs adaptive thinking when
        the field is absent, and max_tokens caps thinking PLUS response text
        together — so a caller whose max_tokens was sized for the answer alone
        can truncate mid-tool-call. Pass {"type": "disabled"} to spend the whole
        budget on the answer.

        `images` is an optional list of (label, media_type, base64_data) — see
        _build_user_content for the block layout. Omitting it (the default)
        produces a body byte-identical to the string-content shape every other
        caller relies on; a test pins that identity.

        Raises:
            TransientError: on 429 / 5xx / network errors (retryable).
            PermanentError: on 4xx other than 429, or malformed responses.
        """
        body = {
            "model": model or self.default_model,
            "max_tokens": max_tokens,
            "system": list(system_blocks),
            "messages": [
                {
                    "role": "user",
                    "content": _build_user_content(user_prompt, images, images_preamble),
                }
            ],
            "tools": tools,
            "tool_choice": tool_choice,
        }
        if thinking is not None:
            body["thinking"] = thinking
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

    def chat(
        self,
        system: str,
        user: str,
        model: str | None = None,
        max_tokens: int = 1024,
    ) -> str:
        """Plain text call — no tool use. Returns the first text block.

        Raises TransientError / PermanentError with the same semantics as review().
        """
        body = {
            "model": model or self.default_model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
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
            raise TransientError(f"Claude transport error: {exc}") from exc

        if response.status_code != 200:
            raise _map_status_to_error(
                response.status_code,
                response.headers.get("retry-after"),
                response.text,
            )

        content = response.json().get("content") or []
        for block in content:
            if block.get("type") == "text":
                return block["text"].strip()
        raise PermanentError("Claude returned no text block in chat response")

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
    """Extract the first tool_use block and usage from a 200 response.

    Returns tool_use_input=None when no tool_use block is present; callers
    check for None and raise PermanentError with context-specific messages.
    """
    content = payload.get("content") or []
    tool_use_input: dict | None = None
    for block in content:
        if block.get("type") == "tool_use":
            tool_use_input = block.get("input")
            break

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
