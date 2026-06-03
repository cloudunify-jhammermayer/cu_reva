"""Shared structured-logging setup for all REVA services (INFR-4).

Call `configure_logging()` once at the very top of each service's entry point.
After that, REVA's own `structlog` calls AND stdlib logs (uvicorn, RQ,
SQLAlchemy) render through a single formatter:

  - JSON by default (machine-parseable, consistent across services);
  - human-readable console when REVA_LOG_FORMAT=console (local dev);
  - level from REVA_LOG_LEVEL (default INFO).

Without this, structlog uses its unconfigured defaults, so every service's logs
looked different and there was no level control.
"""

from __future__ import annotations

import logging
import os
import sys

import structlog

_configured = False


def configure_logging() -> None:
    global _configured
    if _configured:  # idempotent — safe if called from multiple entry points
        return

    level = getattr(logging, os.environ.get("REVA_LOG_LEVEL", "INFO").upper(), logging.INFO)
    console = os.environ.get("REVA_LOG_FORMAT", "json").lower() == "console"

    # Processors shared by REVA's structlog calls and foreign (stdlib) records.
    shared = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]
    renderer = (
        structlog.dev.ConsoleRenderer() if console else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=shared
        + [
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            # Hand off to the stdlib ProcessorFormatter so structlog and stdlib
            # records share one rendering pipeline.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    # uvicorn/gunicorn attach their own handlers and don't propagate by default;
    # clear them so their records flow through our root handler (one format).
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "gunicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True

    _configured = True
