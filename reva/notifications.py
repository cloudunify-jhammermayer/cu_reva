"""Best-effort error notifications via Google Chat incoming webhook."""

from __future__ import annotations

import json
import re

import httpx
import structlog

from reva.url_safety import assert_safe_url

logger = structlog.get_logger()

# Google Chat incoming webhooks always live on this host. Restricting to it
# stops a mistyped/tampered GOOGLE_CHAT_WEBHOOK_URL from being used to POST to
# an arbitrary internal service (SSRF).
_CHAT_ALLOWED_HOSTS = frozenset({"chat.googleapis.com"})


def post_to_chat(webhook_url: str, text: str, *, timeout: float = 5) -> bool:
    """Validate + POST to the Google Chat webhook. Returns True on success.

    Host is validated against the allowlist first (SSRF guard); all errors are
    swallowed (logged) so notifications never mask the original failure. Shared
    by the alert paths and the weekly report (SECU-15) so the SSRF check is
    applied uniformly."""
    try:
        assert_safe_url(webhook_url, allowed_hosts=_CHAT_ALLOWED_HOSTS)
        httpx.post(webhook_url, json={"text": text}, timeout=timeout)
        return True
    except Exception as exc:
        logger.warning("google_chat_notify_failed", error=str(exc))
        return False


def _post_to_chat(webhook_url: str, text: str) -> None:
    post_to_chat(webhook_url, text)

# ---------------------------------------------------------------------------
# Error pattern matchers
# ---------------------------------------------------------------------------

def _classify(error_class: str, raw: str) -> tuple[str, str]:
    """Return (title, detail) for a human-readable Google Chat alert.

    Covers every error surface in the worker:
      - Claude API (4xx / 5xx / network / bad response)
      - GitHub API (auth, rate limit, not found, validation, network)
      - GitHub App private key parsing
      - Internal/unexpected exceptions
    """
    msg = raw.strip()

    # ── Claude API HTTP errors ──────────────────────────────────────────────
    m = re.match(r"Claude (\d+): (.+)", msg, re.DOTALL)
    if m:
        status, body = int(m.group(1)), m.group(2)
        try:
            parsed = json.loads(body)
            api_type = parsed.get("error", {}).get("type", "")
            api_msg  = parsed.get("error", {}).get("message", "")
        except (json.JSONDecodeError, AttributeError):
            api_type, api_msg = "", body

        if status == 401 or api_type == "authentication_error":
            return (
                "Invalid Anthropic API key",
                "HTTP 401 — the API key is rejected by Anthropic.\n"
                "Fix: replace `ANTHROPIC_API_KEY` in `.env` and restart the worker.\n"
                "New keys: console.anthropic.com → API Keys",
            )
        if status == 403 or api_type == "permission_error":
            return (
                "Anthropic API permission denied",
                f"HTTP 403 — the key exists but lacks permission for this model/feature.\n"
                f"Detail: {api_msg or body[:200]}",
            )
        if status == 400 or api_type == "invalid_request_error":
            # Context-window overflow comes through as 400
            if "too large" in api_msg.lower() or "context" in api_msg.lower() or "token" in api_msg.lower():
                return (
                    "Prompt too large for Claude context window",
                    "The diff + system prompt exceeded Claude's maximum context length.\n"
                    "Lower `max_diff_tokens` in `.claude-review.yml` or ask the author to split the PR.",
                )
            return (
                "Claude rejected the request (bad input)",
                f"HTTP 400 — {api_msg or body[:300]}",
            )
        if status == 404:
            return (
                "Claude model not found",
                f"HTTP 404 — the requested model does not exist or is not available on this key.\n"
                f"Detail: {api_msg or body[:200]}",
            )
        if status == 429 or api_type == "rate_limit_error":
            return (
                "Anthropic rate limit exhausted",
                "All retry attempts failed due to rate limiting.\n"
                "The review will need to be manually requeued.\n"
                f"Detail: {api_msg or body[:200]}",
            )
        if status == 529 or api_type == "overloaded_error":
            return (
                "Anthropic API overloaded",
                "Claude returned 529 (overloaded) and all retries were exhausted.\n"
                "Manually requeue the review once API load drops.",
            )
        if 500 <= status < 600:
            return (
                f"Anthropic server error (HTTP {status})",
                f"All retries exhausted after repeated {status} responses.\n"
                f"Detail: {api_msg or body[:200]}",
            )
        return (
            f"Claude API error (HTTP {status})",
            f"{api_msg or body[:300]}",
        )

    # ── Claude network errors ────────────────────────────────────────────────
    if msg.startswith("Claude request timed out"):
        return (
            "Claude API request timed out",
            "The request to Anthropic timed out (180 s limit) — retries exhausted.\n"
            "This usually means the diff was very large or Anthropic is slow.",
        )
    if msg.startswith("Claude transport error"):
        detail = msg[len("Claude transport error:"):].strip()
        return (
            "Cannot reach Anthropic API",
            f"Network/transport error contacting api.anthropic.com.\n"
            f"Detail: {detail[:300]}",
        )
    if "tool_use" in msg and "submit_review" in msg:
        stop = re.search(r"stop_reason=(\S+)", msg)
        return (
            "Claude did not call the review tool",
            f"The model finished without calling `submit_review` "
            f"(stop_reason={stop.group(1) if stop else 'unknown'}).\n"
            "This is a prompt/model mismatch — check that the prompt instructs tool use.",
        )
    if "tool_use input" in msg:
        return (
            "Claude returned malformed review data",
            f"The `submit_review` tool call contained invalid fields.\n"
            f"Detail: {msg[:300]}",
        )
    if "finding failed validation" in msg:
        return (
            "Claude finding failed schema validation",
            f"One or more findings had an unexpected shape.\n"
            f"Detail: {msg[:300]}",
        )

    # ── GitHub API HTTP errors ───────────────────────────────────────────────
    m = re.match(r"GitHub (\d+) \(([^)]+)\): (.+)", msg, re.DOTALL)
    if m:
        status, action, body = int(m.group(1)), m.group(2), m.group(3)

        if status == 401 or "App auth invalid" in body:
            return (
                "GitHub App authentication failed",
                f"HTTP 401 during `{action}`.\n"
                "The JWT signed with the private key was rejected — the key may have been revoked.\n"
                "Generate a new private key in GitHub App settings and update the PEM file.",
            )
        if status == 403 and "rate limited" in msg:
            return (
                "GitHub API rate limit hit",
                f"GitHub rate limit exhausted during `{action}` — the job will retry automatically.\n"
                f"Detail: {body[:200]}",
            )
        if status == 403:
            return (
                "GitHub permission denied",
                f"HTTP 403 during `{action}` — the GitHub App may lack the required permissions.\n"
                "Check that the App has `contents: read`, `pull_requests: write`, and `checks: write`.\n"
                f"Detail: {body[:200]}",
            )
        if status == 404:
            return (
                "GitHub resource not found",
                f"HTTP 404 during `{action}` — the repository, PR, or file no longer exists.\n"
                f"Detail: {body[:200]}",
            )
        if status == 422:
            return (
                "GitHub rejected the request (validation error)",
                f"HTTP 422 during `{action}` — likely a duplicate review or invalid inline comment position.\n"
                f"Detail: {body[:200]}",
            )
        if 500 <= status < 600:
            return (
                f"GitHub server error (HTTP {status})",
                f"GitHub returned {status} during `{action}` — all retries exhausted.\n"
                f"Detail: {body[:200]}",
            )
        return (
            f"GitHub API error (HTTP {status})",
            f"Action: `{action}`\nDetail: {body[:300]}",
        )

    # ── GitHub network errors ────────────────────────────────────────────────
    if "GitHub timeout" in msg:
        action = "token exchange" if "token exchange" in msg else "API request"
        return (
            f"GitHub API timed out ({action})",
            "A request to api.github.com timed out — retries exhausted.",
        )
    if "GitHub transport" in msg:
        detail = re.sub(r"GitHub transport[^:]*: ?", "", msg)
        return (
            "Cannot reach GitHub API",
            f"Network/transport error contacting api.github.com.\n"
            f"Detail: {detail[:300]}",
        )

    # ── GitHub App private key ───────────────────────────────────────────────
    if "parse" in msg.lower() and "key" in msg.lower():
        return (
            "Invalid GitHub App private key",
            "The PEM file at `secrets/github-app-private-key.pem` cannot be parsed.\n"
            "It must start with `-----BEGIN RSA PRIVATE KEY-----`.\n"
            "Download a fresh key from GitHub App settings → Private keys.",
        )

    # ── DB / infrastructure ──────────────────────────────────────────────────
    if "could not connect to server" in msg.lower() or "connection refused" in msg.lower():
        return (
            "Database connection failed",
            f"The worker could not reach PostgreSQL.\n"
            f"Detail: {msg[:300]}",
        )
    if "redis" in msg.lower() and ("connect" in msg.lower() or "refused" in msg.lower()):
        return (
            "Redis connection failed",
            f"The worker could not reach Redis.\n"
            f"Detail: {msg[:300]}",
        )

    # ── Fallback ─────────────────────────────────────────────────────────────
    return (
        f"Unexpected error ({error_class})",
        msg[:400],
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def notify_worker_error(
    webhook_url: str,
    repo_full_name: str,
    pr_number: int,
    error_class: str,
    message: str,
) -> None:
    """POST a formatted alert to a Google Chat space.

    Silently swallows all exceptions — notifications must never mask the
    original error or prevent the worker from recording failures.
    """
    if not webhook_url:
        return

    title, detail = _classify(error_class, message)
    text = (
        f"🔴 *{title}*\n"
        f"Repo: `{repo_full_name}` · PR #{pr_number}\n"
        f"\n"
        f"{detail}"
    )
    _post_to_chat(webhook_url, text)


def notify_operational_alert(webhook_url: str, title: str, detail: str) -> None:
    """POST an operational/infra alert (queue depth, disk, failed jobs) to Google Chat.

    Best-effort: swallows all exceptions so monitoring can never crash the caller.
    """
    if not webhook_url:
        return
    _post_to_chat(webhook_url, f"⚠️ *{title}*\n{detail}")
