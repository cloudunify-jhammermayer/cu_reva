"""Exception hierarchy for the review worker.

Transient errors are surfaced to RQ so the job is retried with backoff.
Permanent errors fail the job immediately.
Stale and Declined are not errors — they are terminal review outcomes.
"""

from __future__ import annotations


class WorkerError(Exception):
    """Base for all worker-raised exceptions."""


class TransientError(WorkerError):
    """Retryable failure (network, 429, 5xx)."""

    def __init__(self, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class PermanentError(WorkerError):
    """Non-retryable failure (4xx, invalid response, validation failure)."""


class MalformedModelOutput(PermanentError):
    """Claude returned a truncated or schema-invalid tool call.

    Permanent at the RQ boundary (re-running a doomed job re-pays Claude), but
    usually a one-off formatting hiccup — callers may retry once in-process
    before treating it as a failure the user sees."""


class ProviderCreditExhausted(PermanentError):
    """The Anthropic account has no credit left (HTTP 400 "Credit balance is
    too low"). Not one of REVA's own spend caps: nothing was paid, and the
    call will succeed again once the balance is topped up, so runners defer
    the job (worker.runner.defer_for_budget) instead of failing it. Subclasses
    PermanentError so any path without specific handling still degrades to
    today's terminal behaviour."""


def is_provider_credit_error(text: str | None) -> bool:
    """True for the Anthropic "credit balance is too low" refusal (any casing)."""
    return bool(text) and "credit balance is too low" in text.lower()


