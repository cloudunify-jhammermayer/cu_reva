"""Shared RQ task-boundary contract.

RQ's `Retry` is blind to the exception type — a re-raised `PermanentError`
re-runs the doomed job on every attempt, re-charging Claude and re-sending the
same operator alert (e.g. a force-pushed-away SHA, invalid tool call, purged
input). The runners already record the failure and notify before raising, so at
the RQ boundary a `PermanentError` must be turned into a terminal result; only
`TransientError` (and unclassified errors the runners convert to Permanent)
should propagate for RQ to retry with backoff.

Defining the contract once here — instead of re-implementing it per runner —
is what keeps the four job types from drifting apart again (M4/M5).
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

import structlog

from reva.errors import PermanentError

logger = structlog.get_logger()


def terminal_on_permanent(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap an RQ task so a `PermanentError` ends the job after one attempt.

    Returns a terminal result dict instead of re-raising (which RQ would retry).
    `TransientError` and any other exception propagate unchanged.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except PermanentError as exc:
            logger.info(
                "task_permanent_error_not_retried", task=fn.__name__, error=str(exc)
            )
            return {"status": "failed", "error_class": "permanent", "error": str(exc)}

    return wrapper
