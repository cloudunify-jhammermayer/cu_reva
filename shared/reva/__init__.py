"""REVA shared library.

Imported by both the worker (orchestration + RQ task entry) and the api
(webhook receiver + scheduler). Holds:
  - types (Finding, ReviewResult, JobParams, ClaudeResponse, ContentBlock)
  - errors (TransientError, PermanentError, ...)
  - external API clients (claude_client, github_client)
  - HTTP plumbing (_github_http)
  - DB layer (db.engine, db.models, db.writers, db.repo_lookup)
  - pure formatters (review_formatter, diff_utils, cost)
  - prompt assembly (prompt_builder)
  - tool definition (review_tool)

Nothing in this package owns side effects on the queue; that's worker-only.
"""

__version__ = "0.1.0"
