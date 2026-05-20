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


class StaleHeadError(WorkerError):
    """Head SHA changed before the review completed. Terminal, not retried."""


class DeclinedError(WorkerError):
    """Review declined by policy (diff too large, etc.). Terminal, not retried."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
