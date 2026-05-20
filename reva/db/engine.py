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
from sqlalchemy.orm import Session, sessionmaker

logger = structlog.get_logger()

_VERSION_RE = re.compile(r"^(\d+)_")


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
        with engine.begin() as conn:
            conn.execute(text(sql))
            conn.execute(
                text("INSERT INTO schema_migrations (version) VALUES (:v)"),
                {"v": version},
            )
        newly_applied.append(version)

    return newly_applied


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
