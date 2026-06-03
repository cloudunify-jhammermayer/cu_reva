"""Database layer for the worker (and, later, the api).

Single source of truth for the live schema is `models.py`; the
.sql files under `db/migrations/` are the production deploy path
and must stay in lockstep with the models.
"""

from reva.db import repo_lookup, writers
from reva.db.engine import Database, create_engine_from_url, migrate
from reva.db.repo_lookup import DatabaseRepoLookup
from reva.db.models import (
    AdminAudit,
    AuditRun,
    Base,
    ClaudeSpend,
    GithubEvent,
    PendingReview,
    PromptVersion,
    PullRequest,
    Repository,
    ReviewFeedback,
    ReviewFinding,
    ReviewJob,
    ReviewRun,
)

__all__ = [
    "Database",
    "DatabaseRepoLookup",
    "create_engine_from_url",
    "migrate",
    "repo_lookup",
    "writers",
    "AdminAudit",
    "AuditRun",
    "Base",
    "ClaudeSpend",
    "GithubEvent",
    "PendingReview",
    "PromptVersion",
    "PullRequest",
    "Repository",
    "ReviewFeedback",
    "ReviewFinding",
    "ReviewJob",
    "ReviewRun",
]
