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


