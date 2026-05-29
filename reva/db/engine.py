"""Engine, session factory, and migration runner.

`migrate(engine, migrations_dir)` executes every `*.sql` file under
`migrations_dir` whose leading integer version is not already in
`schema_migrations`. Each file runs in its own transaction; partial
application is impossible.

Production: worker/api processes call `migrate()` at startup against the
Postgres engine. The migration files are Postgres DDL.

Tests: SQLite-based tests bypass `migrate()` and call
`Base.metadata.create_all(engine)` instead — the model layer is portable
across both dialects.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import structlog
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

logger = structlog.get_logger()

_VERSION_RE = re.compile(r"^(\d+)_")

# Arbitrary constant key for the Postgres advisory lock that serializes
# concurrent process startups running migrate() against the same database.
_MIGRATION_LOCK_KEY = 982_374_001


def create_engine_from_url(url: str, **kwargs) -> Engine:
    """Create a SQLAlchemy engine from a connection URL.

    Defaults to pool_pre_ping=True so stale connections are recycled.
    """
    kwargs.setdefault("pool_pre_ping", True)
    kwargs.setdefault("future", True)
    return create_engine(url, **kwargs)


def migrate(engine: Engine, migrations_dir: str | Path) -> list[int]:
    """Apply pending migrations from `migrations_dir`.

    Returns the list of versions newly applied. Idempotent — a second call
    after a successful run applies nothing.
    """
    migrations_dir = Path(migrations_dir)
    if not migrations_dir.is_dir():
        raise FileNotFoundError(f"migrations_dir not found: {migrations_dir}")

    # On Postgres, hold a session-level advisory lock for the whole run so two
    # processes starting at once cannot race to apply the same DDL. SQLite
    # (tests) is single-writer, so no lock is needed there.
    lock_conn = None
    if engine.dialect.name == "postgresql":
        lock_conn = engine.connect()
        lock_conn.exec_driver_sql(f"SELECT pg_advisory_lock({_MIGRATION_LOCK_KEY})")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS schema_migrations ("
                    "version INTEGER PRIMARY KEY, "
                    "applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
                )
            )

        with engine.connect() as conn:
            applied = {
                row[0]
                for row in conn.execute(text("SELECT version FROM schema_migrations")).fetchall()
            }

        files = sorted(
            (p for p in migrations_dir.glob("*.sql")),
            key=lambda p: _parse_version(p.name),
        )

        newly_applied: list[int] = []
        for path in files:
            version = _parse_version(path.name)
            if version in applied:
                continue
            sql = path.read_text()
            logger.info("migration_applying", version=version, file=path.name)
            try:
                with engine.begin() as conn:
                    # exec_driver_sql sends the file verbatim to the driver: it
                    # runs multi-statement DDL and does not treat ':' as a bind
                    # parameter (which text() would).
                    conn.exec_driver_sql(sql)
                    conn.execute(
                        text("INSERT INTO schema_migrations (version) VALUES (:v)"),
                        {"v": version},
                    )
            except (IntegrityError, OperationalError, ProgrammingError):
                # A peer applied this migration concurrently: IntegrityError on the
                # schema_migrations INSERT, or DDL "already exists" (psycopg raises
                # ProgrammingError/DuplicateTable; some drivers OperationalError).
                # Verify by re-reading the applied set; skip only if confirmed.
                with engine.connect() as c:
                    confirmed = c.execute(
                        text("SELECT 1 FROM schema_migrations WHERE version = :v"),
                        {"v": version},
                    ).first()
                if confirmed:
                    logger.info("migration_applied_by_peer", version=version, file=path.name)
                    continue
                raise
            newly_applied.append(version)

        return newly_applied
    finally:
        if lock_conn is not None:
            lock_conn.exec_driver_sql(f"SELECT pg_advisory_unlock({_MIGRATION_LOCK_KEY})")
            lock_conn.close()


def _parse_version(filename: str) -> int:
    match = _VERSION_RE.match(filename)
    if not match:
        raise ValueError(f"migration filename must start with a version number: {filename}")
    return int(match.group(1))


# ------------------------------------------------------------------ Database


class Database:
    """Thin facade around a SQLAlchemy engine + session factory.

    Holds the writer methods used by `tasks.run_review` and the read
    methods that satisfy the `reviewer.RepoLookup` Protocol.

    Construct once at worker/api startup; share across requests/jobs.
    """

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._sessionmaker = sessionmaker(
            bind=engine, expire_on_commit=False, autoflush=False, future=True
        )

    # --- session lifecycle -------------------------------------------------

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._sessionmaker()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # --- migration shortcut -----------------------------------------------

    def migrate(self, migrations_dir: str | Path | None = None) -> list[int]:
        if migrations_dir is None:
            migrations_dir = os.environ.get("REVA_MIGRATIONS_DIR", "/app/db/migrations")
        return migrate(self.engine, migrations_dir)
