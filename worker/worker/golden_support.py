"""Shared plumbing for golden-estimate anchoring across the estimating runners.

Lives in the worker package, not in `reva/golden_estimates.py`, so the loader
stays a pure function with no database dependency.
"""

from __future__ import annotations

from typing import Any

from reva.db import writers
from reva.golden_estimates import Degradation


def record_degradations(
    db: Any,
    log: Any,
    component: str,
    degradations: list[Degradation],
    detail: dict,
) -> None:
    """Log AND ops-event every anchoring degradation.

    Both halves are mandatory: a silently unanchored estimate is
    indistinguishable from a well-anchored one.
    """
    for degradation in degradations:
        log.warning(f"golden_estimates_{degradation.reason}", **degradation.detail)
        writers.record_ops_event(
            db,
            component,
            "warning",
            f"golden_estimates_{degradation.reason}",
            {**detail, **degradation.detail},
        )
