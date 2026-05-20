"""Adapter that exposes the Reviewer's RepoLookup Protocol over a Database.

Reviewer imports the Protocol from `worker.reviewer`; concrete satisfaction
lives here so the reviewer doesn't depend on the DB module.
"""

from __future__ import annotations

from reva.db import writers
from reva.db.engine import Database


class DatabaseRepoLookup:
    """Implements `worker.reviewer.RepoLookup` against the live DB."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def get_owner_name(self, repository_id: int) -> tuple[str, str]:
        return writers.get_owner_name(self._db, repository_id)

    def get_pr_basic(self, pull_request_id: int) -> dict:
        return writers.get_pr_basic(self._db, pull_request_id)
