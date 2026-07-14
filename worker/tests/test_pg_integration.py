"""Real-Postgres integration tests for the concurrency guards (D1/TEST-1).

These exercise behaviour that **SQLite silently no-ops**, so the normal unit
suite can't see it:

  - `FOR UPDATE SKIP LOCKED` (the poller's multi-replica claim)
  - `pg_advisory_xact_lock` (the rolling budget cap's serialized read)
  - the stale-running reaper against real timestamptz semantics

Skipped unless REVA_TEST_POSTGRES_URL points at a throwaway Postgres, e.g.:

  docker run -d --name pg -e POSTGRES_USER=review -e POSTGRES_PASSWORD=test \
    -e POSTGRES_DB=reviews -p 55433:5432 postgres:16-alpine
  REVA_TEST_POSTGRES_URL=postgresql://review:test@localhost:55433/reviews \
    worker/.venv/bin/python -m pytest worker/tests/test_pg_integration.py -q
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, text

# Repo-root-anchored so migrations resolve no matter the pytest cwd (CI runs
# from worker/, local runs from the repo root).
_MIGRATIONS_DIR = str(Path(__file__).resolve().parents[2] / "db" / "migrations")

from reva.db import writers
from reva.db.engine import Database, create_engine_from_url
from reva.db.models import PendingReview, ReviewRun
from reva.types import JobParams

PG_URL = os.environ.get("REVA_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not PG_URL, reason="REVA_TEST_POSTGRES_URL not set (real-Postgres integration tier)"
)


@pytest.fixture()
def pg_db():
    db = Database(create_engine_from_url(PG_URL))
    db.migrate(_MIGRATIONS_DIR)
    # Clean slate per test. repositories CASCADEs to PRs/pending/runs/findings;
    # the repo-docs tables key on repo_full_name (no FK) so truncate them too.
    with db.engine.begin() as conn:
        conn.execute(text(
            "TRUNCATE repositories, claude_spend, repo_doc_sections, repo_docs_sync "
            "RESTART IDENTITY CASCADE"
        ))
    yield db
    db.engine.dispose()


def _seed_pending(db) -> int:
    repo_id = writers.upsert_repository(
        db, github_repository_id=1, owner="acme", name="widgets",
        default_branch="main", installation_id=500,
    )
    pr_id = writers.upsert_pull_request(
        db, repository_id=repo_id, github_pr_id=9001, pr_number=42, title="t",
        author_login="alice", base_branch="main", head_branch="feat",
        head_sha="abc", state="open", draft=False,
    )
    return writers.upsert_pending_review(
        db, repository_id=repo_id, pull_request_id=pr_id, pr_number=42,
        head_sha="abc", installation_id=500, trigger_event="opened",
        review_mode="diff", scheduled_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )


def test_skip_locked_makes_a_second_claimer_skip_a_locked_row(pg_db):
    """A pending row another poller is mid-consuming (row-locked) must be SKIPPED
    by a second poller — not blocked, not double-claimed. This is the multi-replica
    safety the poller relies on; on SQLite the clause is a no-op."""
    pending_id = _seed_pending(pg_db)
    claim = (
        select(PendingReview.id)
        .where(PendingReview.id == pending_id)
        .with_for_update(skip_locked=True)
    )

    # Connection A claims and HOLDS the row lock (open transaction).
    conn_a = pg_db.engine.connect()
    tx_a = conn_a.begin()
    try:
        got_a = conn_a.execute(claim).scalar_one_or_none()
        assert got_a == pending_id  # A claimed it

        # Connection B tries the same claim while A holds the lock → skipped.
        with pg_db.engine.connect() as conn_b:
            conn_b.begin()
            got_b = conn_b.execute(claim).scalar_one_or_none()
        assert got_b is None, "SKIP LOCKED failed: a second claimer saw a locked row"
    finally:
        tx_a.rollback()
        conn_a.close()

    # Once A releases, the row is claimable again.
    with pg_db.engine.connect() as conn_c:
        conn_c.begin()
        assert conn_c.execute(claim).scalar_one_or_none() == pending_id


def _seed_repo_pr(db) -> JobParams:
    repo_id = writers.upsert_repository(
        db, github_repository_id=1, owner="acme", name="widgets",
        default_branch="main", installation_id=500,
    )
    pr_id = writers.upsert_pull_request(
        db, repository_id=repo_id, github_pr_id=9001, pr_number=42, title="t",
        author_login="alice", base_branch="main", head_branch="feat",
        head_sha="abc", state="open", draft=False,
    )
    return JobParams(
        repository_id=repo_id, pull_request_id=pr_id, head_sha="abc",
        installation_id=500, review_mode="diff", trigger_event="opened",
    )


def test_claim_review_run_blocks_a_second_distinct_job(pg_db):
    """CONC-1: two distinct worker jobs for the same (repo,pr,sha,mode) — only one
    may claim it. The other must bail (no duplicate paid review). A retry of the
    same job re-claims; once the run is terminal, a new job may re-claim."""
    params = _seed_repo_pr(pg_db)

    rid1, claimed1 = writers.claim_review_run(pg_db, params, job_id="job-1")
    assert claimed1 is True

    rid2, claimed2 = writers.claim_review_run(pg_db, params, job_id="job-2")
    assert (rid2, claimed2) == (rid1, False), "a second distinct job wrongly claimed an in-flight review"

    # same job retrying (RQ retry) must re-claim and proceed
    _, claimed_retry = writers.claim_review_run(pg_db, params, job_id="job-1")
    assert claimed_retry is True

    # terminal run → a new job may re-claim (explicit re-review path)
    with pg_db.session() as s:
        s.get(ReviewRun, rid1).status = "completed"
    _, claimed_after = writers.claim_review_run(pg_db, params, job_id="job-9")
    assert claimed_after is True


def test_claim_review_run_is_atomic_under_concurrency(pg_db):
    """Two jobs racing the claim at the same instant: exactly ONE wins (FOR UPDATE
    serializes on real Postgres; SQLite would no-op the lock)."""
    import threading

    params = _seed_repo_pr(pg_db)
    barrier = threading.Barrier(2)
    results: dict[str, bool] = {}

    def claim(job_id: str):
        barrier.wait()  # line them up to race
        _, claimed = writers.claim_review_run(pg_db, params, job_id=job_id)
        results[job_id] = claimed

    t1 = threading.Thread(target=claim, args=("job-a",))
    t2 = threading.Thread(target=claim, args=("job-b",))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sum(results.values()) == 1, f"exactly one job must win the claim, got {results}"


def test_budget_advisory_lock_read_returns_correct_total(pg_db):
    """The serialized (advisory-locked) spend read must run for real on Postgres
    and return the correct rolling total across all ledger kinds."""
    writers.record_claude_spend(pg_db, "review", 1.25)
    writers.record_claude_spend(pg_db, "audit", 2.00)
    writers.record_claude_spend(pg_db, "reply", 0.50)
    since = datetime.now(timezone.utc) - timedelta(days=1)
    total = writers.sum_estimated_cost_since(pg_db, since, serialize=True)
    assert total == pytest.approx(3.75)


def test_reaper_fails_stale_running_runs(pg_db):
    """A run stuck in 'running' past the threshold is reaped to 'failed' on real
    timestamptz semantics (not the naive SQLite clock)."""
    repo_id = writers.upsert_repository(
        pg_db, github_repository_id=1, owner="acme", name="widgets",
        default_branch="main", installation_id=500,
    )
    pr_id = writers.upsert_pull_request(
        pg_db, repository_id=repo_id, github_pr_id=9001, pr_number=42, title="t",
        author_login="alice", base_branch="main", head_branch="feat",
        head_sha="abc", state="open", draft=False,
    )
    params = JobParams(
        repository_id=repo_id, pull_request_id=pr_id, head_sha="abc",
        installation_id=500, review_mode="diff", trigger_event="opened",
    )
    run_id = writers.record_review_started(pg_db, params)
    # Backdate started_at well beyond the threshold.
    with pg_db.engine.begin() as conn:
        conn.execute(
            text("UPDATE review_runs SET started_at = now() - interval '2 hours' WHERE id = :id"),
            {"id": run_id},
        )

    reaped = writers.reap_stale_running_reviews(pg_db, older_than_seconds=3600)
    assert reaped == 1
    with pg_db.session() as s:
        run = s.get(ReviewRun, run_id)
        assert run.status == "failed"
        assert run.error_class == "stale"


def test_reaper_does_not_double_reap_under_concurrency(pg_db):
    """CONC-8: two scheduler replicas reaping at once must not both claim the same
    stale row — SKIP LOCKED gives it to exactly one."""
    import threading

    params = _seed_repo_pr(pg_db)
    run_id = writers.record_review_started(pg_db, params)
    with pg_db.engine.begin() as conn:
        conn.execute(
            text("UPDATE review_runs SET started_at = now() - interval '2 hours' WHERE id = :id"),
            {"id": run_id},
        )

    barrier = threading.Barrier(2)
    reaped: list[int] = []
    lock = threading.Lock()

    def reap():
        barrier.wait()
        n = writers.reap_stale_running_reviews(pg_db, older_than_seconds=3600)
        with lock:
            reaped.append(n)

    t1 = threading.Thread(target=reap)
    t2 = threading.Thread(target=reap)
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sum(reaped) == 1, f"stale row must be reaped exactly once, got {reaped}"


# ---- repo-docs retrieval (migration 039, FTS + advisory lock) ---------------


class _FakeGitHub:
    """Serves a default-branch tree + file contents for sync_repo_docs."""

    def __init__(self, sha, files):
        self._sha = sha
        self._files = files

    def get_repo_installation_id(self, owner, repo):
        return 1

    def get_installation_token(self, installation_id):
        return "tok"

    def get_repo(self, token, owner, repo):
        return {"default_branch": "main"}

    def get_tree(self, token, owner, repo, ref, recursive=True):
        return {
            "sha": self._sha, "truncated": False,
            "tree": [{"path": p, "type": "blob", "size": 1} for p in self._files],
        }

    def get_file_content(self, token, owner, repo, path, ref):
        return self._files.get(path)


def _seed_sections(db, repo, rows):
    from reva.db.models import RepoDocSection

    with db.session() as s:
        for path, title, body in rows:
            s.add(RepoDocSection(
                repo_full_name=repo, path=path, anchor="a", title=title, body=body,
            ))


def test_migration_039_creates_repo_docs_tables_and_column(pg_db):
    """The tables + ticket_analyses column exist after migrate (real DDL)."""
    with pg_db.engine.connect() as conn:
        for tbl in ("repo_doc_sections", "repo_docs_sync"):
            assert conn.execute(
                text("SELECT to_regclass(:t)"), {"t": tbl}
            ).scalar_one() is not None, tbl
        col = conn.execute(text(
            "SELECT 1 FROM information_schema.columns WHERE table_name = 'ticket_analyses' "
            "AND column_name = 'repo_docs_sections_used'"
        )).scalar_one_or_none()
        assert col == 1


def test_repo_docs_fts_ranks_and_scopes(pg_db):
    """The real Postgres FTS query ranks a dense title+body match above a
    passing mention and never crosses repo boundaries."""
    from reva.repo_docs import search_repo_docs

    _seed_sections(pg_db, "acme/widgets", [
        ("p1", "Quotation templates", "manage quotation templates for quotation flows"),
        ("p2", "Misc notes", "a passing mention of quotation somewhere in prose"),
    ])
    _seed_sections(pg_db, "other/repo", [
        ("p3", "Quotation", "quotation quotation quotation"),  # must not leak in
    ])

    hits = search_repo_docs(pg_db, "acme/widgets", ["quotation"])
    paths = [h["path"] for h in hits]
    assert paths == ["p1", "p2"]          # dense match ranks first; other repo absent


def test_repo_docs_fts_or_of_terms_realistic_planner_query(pg_db):
    """A section matching only SOME of a realistic many-term planner query must
    still hit (OR-of-terms), and a section matching more terms ranks first.
    Under the old single plainto_tsquery (AND of all terms) this returned
    nothing at all."""
    from reva.repo_docs import search_repo_docs

    _seed_sections(pg_db, "acme/widgets", [
        ("layout", "Custom quotation layout",
         "we customized the quotation PDF output for sales orders"),
        ("hr", "Payroll rules", "salary computation for employees"),
    ])

    planner_terms = ["quotation", "template", "pdf", "layout", "sale", "sale_management"]
    hits = search_repo_docs(pg_db, "acme/widgets", planner_terms)
    paths = [h["path"] for h in hits]
    assert paths and paths[0] == "layout"  # multi-term match found and ranked first
    assert "hr" not in paths               # zero-term match stays out


def test_repo_docs_advisory_lock_busy_skip(pg_db):
    """A second sync of the SAME repo while the per-repo lock is held returns
    'busy' and writes nothing; a DIFFERENT repo syncs concurrently (per-repo
    key). On SQLite this lock is a no-op — only real here."""
    from reva.db.models import RepoDocsSync
    from reva.repo_docs import _LOCK_CLASSID, _lock_objid, sync_repo_docs

    gh = _FakeGitHub("sha-new", {"custom_addons/a/README.md": "# H\nbody\n"})

    conn_a = pg_db.engine.connect()
    tx_a = conn_a.begin()
    try:
        conn_a.execute(
            text("SELECT pg_advisory_xact_lock(:c, :o)"),
            {"c": _LOCK_CLASSID, "o": _lock_objid("acme/widgets")},
        )

        # Same repo → contends with the held lock → busy, no writes.
        from reva.db.models import RepoDocSection

        result = sync_repo_docs(pg_db, gh, "acme", "widgets")
        assert result["status"] == "busy"
        with pg_db.session() as s:
            assert s.get(RepoDocsSync, "acme/widgets") is None
            assert s.query(RepoDocSection).filter_by(
                repo_full_name="acme/widgets"
            ).count() == 0

        # Different repo → different objid → syncs fine while A holds its lock.
        other = sync_repo_docs(pg_db, gh, "other", "repo")
        assert other["status"] == "synced"
    finally:
        tx_a.rollback()
        conn_a.close()
