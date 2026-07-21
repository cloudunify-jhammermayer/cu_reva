# Multiple Odoo Instances Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make each Odoo instance a first-class, TUI-creatable record with its own REVA-minted scoped API key and its own (encrypted) callback config, with per-instance token/cost reporting.

**Architecture:** A new `odoo_instances` table; the two ticket tables (`ticket_analyses`, `ticket_issue_runs`) gain an `odoo_instance_id` FK. Inbound: the two Odoo *create* routes authenticate by instance key (key→identity), every other route stays master-key-only. Outbound: the worker builds an `OdooCallbackClient` per job from the run's instance (callback URL + Fernet-decrypted key). Cost is summed off the run tables per instance over lifetime/24h/30d, split by task type. A new read-write TUI "Odoo" tab manages instances and shows cost.

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy 2.0 (`Mapped`/`mapped_column`), Pydantic v2, RQ, `cryptography` (Fernet — already a dependency), Postgres/SQLite, Go + Bubble Tea (TUI).

**Spec:** `docs/superpowers/specs/2026-06-22-multiple-odoo-instances-design.md`

## Global Constraints

- **No legacy single-Odoo path.** The Odoo ticket features are not deployed; the env callback path (`ODOO_CALLBACK_URL` / `ODOO_CALLBACK_API_KEY`) is **removed**, not kept as a fallback. `odoo_instance_id` is app-required on every new ticket run.
- **Scoping:** instance-scoped keys reach **only** the two create routes (`POST /api/v1/ticket-analysis`, `POST /api/v1/create-issues`). The master `REVA_API_KEY` reaches everything else and is **rejected** on the two create routes (no instance to attribute).
- **Inbound keys stored hashed** (SHA-256 hex); plaintext shown exactly once at create/rotate. **Outbound keys stored Fernet-encrypted** under `REVA_SECRET_KEY` (`env_or_file`); never returned by any GET.
- **DB conventions:** new migration file `db/migrations/018_odoo_instances.sql`, idempotent (`CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`), `id BIGSERIAL PRIMARY KEY`; matching ORM model in `reva/db/models.py` (tests build from models via `create_all`). SQLAlchemy 2.0 `Mapped[]`/`mapped_column` only.
- **Secrets:** load via `reva.config.env_or_file`. Don't log plaintext keys.
- **Definition of done:** `worker`, `api`, **and** `scheduler` pytest suites green (shared `reva/` change) + `ruff check reva worker/worker api/app scheduler/scheduler`; and `cd tui && go build ./... && go vet ./... && go test ./...` green.

## Shared Interfaces (defined across tasks — names are fixed here)

- `reva/secrets_crypto.py`: `encrypt(plaintext: str) -> str`, `decrypt(token: str) -> str`. (Task 2)
- `reva/db/models.py`: `OdooInstance`; `TicketAnalysis.odoo_instance_id: Mapped[int | None]`; `TicketIssueRun.odoo_instance_id: Mapped[int | None]`. (Task 1)
- `reva/types.py`: `TicketJobParams.odoo_instance_id: int`; `TicketIssueJobParams.odoo_instance_id: int`. (Task 6 / 7)
- `reva/db/writers.py`: `create_odoo_instance(db, *, name, key_hash, key_prefix, callback_url, callback_api_key_enc) -> int`; `get_odoo_instance(db, instance_id) -> dict | None`; `rotate_odoo_instance_key(db, instance_id, *, key_hash, key_prefix) -> bool`; `update_odoo_instance(db, instance_id, **fields) -> bool`. (Task 3)
- `api/app/queries/odoo_instances.py`: `resolve_odoo_instance_by_key(db, token) -> tuple[int, str] | None`; `list_odoo_instances(db) -> list[dict]`; `get_odoo_instance_cost(db, instance_id) -> dict`. (Task 3)
- `api/app/dependencies.py`: `ResolvedOdooInstance(id: int, name: str)`; `require_odoo_instance(request, db) -> ResolvedOdooInstance`. (Task 5)
- `worker/worker/runner.py`: `build_odoo_client(ctx, odoo_instance_id: int) -> OdooCallbackClient`. (Task 7)
- TUI `api.ClientIface`: `OdooInstances() (*OdooInstancePage, error)`, `CreateOdooInstance(name, callbackURL, callbackKey string) (*OdooInstanceCreated, error)`, `RotateOdooInstanceKey(id int) (*OdooInstanceCreated, error)`, `SetOdooInstanceActive(id int, active bool) error`. (Task 8)

---

### Task 1: DB migration + ORM model (data model)

**Files:**
- Create: `db/migrations/018_odoo_instances.sql`
- Modify: `reva/db/models.py` (add `OdooInstance`; add `odoo_instance_id` to `TicketAnalysis` + `TicketIssueRun`; change the `idx_ticket_issue_runs_pending` Index)
- Test: `worker/tests/test_odoo_instance_model.py` (uses shared `reva/`; any service venv works — run under `worker`)

**Interfaces:**
- Produces: `OdooInstance` model; `TicketAnalysis.odoo_instance_id`; `TicketIssueRun.odoo_instance_id`; the per-instance partial-unique pending index.

- [ ] **Step 1: Write the failing test**

Create `worker/tests/test_odoo_instance_model.py`:

```python
"""ORM-level tests for the odoo_instances table + per-instance ticket scoping.

SQLite enforces the partial unique index via sqlite_where, so the cross-instance
dedup constraint is exercised here (the raw 018 migration SQL is Postgres-only).
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from reva.db import Base, Database, create_engine_from_url
from reva.db.models import OdooInstance, TicketIssueRun


@pytest.fixture()
def db() -> Database:
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def _instance(s, name: str) -> int:
    inst = OdooInstance(name=name, key_hash=f"hash-{name}", key_prefix="reva_odoo_x")
    s.add(inst)
    s.flush()
    return inst.id


def _pending_run(s, *, instance_id: int, ticket_id: int) -> None:
    s.add(
        TicketIssueRun(
            odoo_instance_id=instance_id, ticket_id=ticket_id,
            model_name="helpdesk.ticket", github_url="https://github.com/o/r",
            name="n", description="d", analysis_html="", priority="1",
            ticket_url="https://odoo/1", status="pending",
        )
    )
    s.flush()


def test_two_instances_share_ticket_id(db: Database) -> None:
    with db.session() as s:
        a = _instance(s, "a")
        b = _instance(s, "b")
        _pending_run(s, instance_id=a, ticket_id=42)
        _pending_run(s, instance_id=b, ticket_id=42)  # different instance → OK


def test_same_instance_duplicate_pending_rejected(db: Database) -> None:
    with pytest.raises(IntegrityError):
        with db.session() as s:
            a = _instance(s, "a")
            _pending_run(s, instance_id=a, ticket_id=42)
            _pending_run(s, instance_id=a, ticket_id=42)  # same instance → reject
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd worker && .venv/bin/python -m pytest tests/test_odoo_instance_model.py -v`
Expected: FAIL — `ImportError` / `AttributeError` on `OdooInstance` (model not defined yet), or `TypeError` on the `odoo_instance_id` kwarg.

- [ ] **Step 3a: Add the migration SQL**

Create `db/migrations/018_odoo_instances.sql`:

```sql
-- Each Odoo instance that talks to REVA: its REVA-minted inbound key (stored
-- hashed) and its own outbound callback target (URL + Fernet-encrypted key).
CREATE TABLE IF NOT EXISTS odoo_instances (
    id                    BIGSERIAL PRIMARY KEY,
    name                  TEXT NOT NULL UNIQUE,
    key_hash              TEXT NOT NULL UNIQUE,
    key_prefix            TEXT NOT NULL,
    callback_url          TEXT NOT NULL DEFAULT '',
    callback_api_key_enc  TEXT NOT NULL DEFAULT '',
    active                BOOLEAN NOT NULL DEFAULT true,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Source-instance scoping on the two ticket tables. App-required (every new row
-- is stamped at create time); nullable at the DB level only because Postgres
-- can't add a NOT NULL column without a default to a possibly-non-empty table.
ALTER TABLE ticket_analyses   ADD COLUMN IF NOT EXISTS odoo_instance_id BIGINT REFERENCES odoo_instances(id);
ALTER TABLE ticket_issue_runs ADD COLUMN IF NOT EXISTS odoo_instance_id BIGINT REFERENCES odoo_instances(id);

-- One in-flight create-issues run PER INSTANCE per (ticket_id, model_name).
-- Replaces the single-Odoo index so two instances may each have a pending run
-- for the same ticket_id.
DROP INDEX IF EXISTS idx_ticket_issue_runs_pending;
CREATE UNIQUE INDEX IF NOT EXISTS idx_ticket_issue_runs_pending
    ON ticket_issue_runs (odoo_instance_id, ticket_id, model_name)
    WHERE status = 'pending';
```

- [ ] **Step 3b: Add the `OdooInstance` model**

In `reva/db/models.py`, add this class (place it near the other ticket models, after `TicketIssueRun`). `_PK`, `Boolean`, `Text`, `DateTime`, `func` are already imported at the top of the file:

```python
class OdooInstance(Base):
    """An Odoo instance that sends work to REVA. Mirrors db/migrations/018.

    `key_hash` is the SHA-256 of the REVA-minted inbound key (plaintext shown
    once at create/rotate). `callback_api_key_enc` is the Fernet-encrypted
    outbound Bearer REVA sends to this Odoo's callback endpoints.
    """

    __tablename__ = "odoo_instances"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    key_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    key_prefix: Mapped[str] = mapped_column(Text, nullable=False)
    callback_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    callback_api_key_enc: Mapped[str] = mapped_column(Text, nullable=False, default="")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
```

- [ ] **Step 3c: Add the FK columns + change the pending index**

In `reva/db/models.py`, inside `class TicketAnalysis`, add (after `field_name`):

```python
    odoo_instance_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("odoo_instances.id")
    )
```

Inside `class TicketIssueRun`, add (after `model_name`):

```python
    odoo_instance_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("odoo_instances.id")
    )
```

Then in `TicketIssueRun.__table_args__`, replace the existing pending index with the per-instance one:

```python
        # One in-flight run per Odoo INSTANCE per record (migration 018).
        Index(
            "idx_ticket_issue_runs_pending",
            "odoo_instance_id",
            "ticket_id",
            "model_name",
            unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
```

(`BigInteger`, `ForeignKey`, `Index`, `text` are already imported at the top of the file.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd worker && .venv/bin/python -m pytest tests/test_odoo_instance_model.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add db/migrations/018_odoo_instances.sql reva/db/models.py worker/tests/test_odoo_instance_model.py
git commit -m "feat(db): odoo_instances table + per-instance ticket scoping"
```

---

### Task 2: Encryption helper (`reva/secrets_crypto.py`)

**Files:**
- Create: `reva/secrets_crypto.py`
- Test: `worker/tests/test_secrets_crypto.py`

**Interfaces:**
- Produces: `encrypt(plaintext: str) -> str`, `decrypt(token: str) -> str`. Empty string passes through (callbacks disabled). Missing/blank `REVA_SECRET_KEY` raises `RuntimeError` when a non-empty value is processed.

- [ ] **Step 1: Write the failing test**

Create `worker/tests/test_secrets_crypto.py`:

```python
"""Fernet wrapper for instance outbound callback keys (REVA_SECRET_KEY)."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from reva import secrets_crypto


@pytest.fixture()
def key(monkeypatch) -> str:
    k = Fernet.generate_key().decode()
    monkeypatch.setenv("REVA_SECRET_KEY", k)
    return k


def test_round_trip(key) -> None:
    token = secrets_crypto.encrypt("super-secret")
    assert token != "super-secret"
    assert secrets_crypto.decrypt(token) == "super-secret"


def test_empty_passthrough(key) -> None:
    assert secrets_crypto.encrypt("") == ""
    assert secrets_crypto.decrypt("") == ""


def test_missing_key_raises_on_nonempty(monkeypatch) -> None:
    monkeypatch.delenv("REVA_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError):
        secrets_crypto.encrypt("x")
    # Empty value never needs the key.
    assert secrets_crypto.encrypt("") == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd worker && .venv/bin/python -m pytest tests/test_secrets_crypto.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'reva.secrets_crypto'`.

- [ ] **Step 3: Write the implementation**

Create `reva/secrets_crypto.py`:

```python
"""Symmetric encryption for secrets stored in the DB (Fernet).

Used for Odoo instances' outbound callback API keys: REVA needs the plaintext
at call time, so the key is encrypted at rest under REVA_SECRET_KEY rather than
hashed. REVA_SECRET_KEY is a Fernet key (generate with Fernet.generate_key()).
"""

from __future__ import annotations

from cryptography.fernet import Fernet

from reva.config import env_or_file


def _fernet() -> Fernet:
    key = env_or_file("REVA_SECRET_KEY")
    if not key:
        raise RuntimeError(
            "REVA_SECRET_KEY is not set — cannot encrypt/decrypt Odoo callback "
            "keys. Generate one with `python -c \"from cryptography.fernet "
            "import Fernet; print(Fernet.generate_key().decode())\"`."
        )
    return Fernet(key.encode())


def encrypt(plaintext: str) -> str:
    """Fernet-encrypt `plaintext`. Empty string passes through unchanged."""
    if plaintext == "":
        return ""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    """Decrypt a token from `encrypt`. Empty string passes through unchanged."""
    if token == "":
        return ""
    return _fernet().decrypt(token.encode()).decode()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd worker && .venv/bin/python -m pytest tests/test_secrets_crypto.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add reva/secrets_crypto.py worker/tests/test_secrets_crypto.py
git commit -m "feat(reva): Fernet helper for encrypting Odoo callback keys"
```

---

### Task 3: Writers + read queries for odoo_instances

**Files:**
- Modify: `reva/db/writers.py` (add `OdooInstance` import; add 4 writer functions)
- Create: `api/app/queries/odoo_instances.py`
- Test: `worker/tests/test_odoo_instance_writers.py` (writers); `api/tests/test_odoo_instance_queries.py` (queries)

**Interfaces:**
- Produces:
  - `create_odoo_instance(db, *, name, key_hash, key_prefix, callback_url, callback_api_key_enc) -> int`
  - `get_odoo_instance(db, instance_id) -> dict | None` (includes `callback_url`, `callback_api_key_enc`)
  - `rotate_odoo_instance_key(db, instance_id, *, key_hash, key_prefix) -> bool`
  - `update_odoo_instance(db, instance_id, **fields) -> bool`
  - `resolve_odoo_instance_by_key(db, token) -> tuple[int, str] | None`
  - `list_odoo_instances(db) -> list[dict]`
  - `get_odoo_instance_cost(db, instance_id) -> dict`

- [ ] **Step 1: Write the failing writer test**

Create `worker/tests/test_odoo_instance_writers.py`:

```python
from __future__ import annotations

import pytest

from reva.db import Base, Database, create_engine_from_url, writers


@pytest.fixture()
def db() -> Database:
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def test_create_and_get(db: Database) -> None:
    iid = writers.create_odoo_instance(
        db, name="ACME", key_hash="h1", key_prefix="reva_odoo_aa",
        callback_url="https://odoo.acme/write-field", callback_api_key_enc="enc",
    )
    row = writers.get_odoo_instance(db, iid)
    assert row["name"] == "ACME"
    assert row["callback_url"] == "https://odoo.acme/write-field"
    assert row["callback_api_key_enc"] == "enc"
    assert row["active"] is True


def test_rotate_changes_hash(db: Database) -> None:
    iid = writers.create_odoo_instance(
        db, name="ACME", key_hash="h1", key_prefix="reva_odoo_aa",
        callback_url="", callback_api_key_enc="",
    )
    assert writers.rotate_odoo_instance_key(db, iid, key_hash="h2", key_prefix="reva_odoo_bb")
    row = writers.get_odoo_instance(db, iid)
    assert row["key_hash"] == "h2"
    assert row["key_prefix"] == "reva_odoo_bb"


def test_update_fields(db: Database) -> None:
    iid = writers.create_odoo_instance(
        db, name="ACME", key_hash="h1", key_prefix="p", callback_url="", callback_api_key_enc="",
    )
    assert writers.update_odoo_instance(db, iid, active=False, callback_url="https://x/write-field")
    row = writers.get_odoo_instance(db, iid)
    assert row["active"] is False
    assert row["callback_url"] == "https://x/write-field"
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd worker && .venv/bin/python -m pytest tests/test_odoo_instance_writers.py -v`
Expected: FAIL — `AttributeError: module 'reva.db.writers' has no attribute 'create_odoo_instance'`.

- [ ] **Step 3a: Add the writers**

In `reva/db/writers.py`, add `OdooInstance` to the `from reva.db.models import (...)` block (keep alphabetical-ish ordering near the other models). Then append these functions:

```python
def create_odoo_instance(
    db: Database,
    *,
    name: str,
    key_hash: str,
    key_prefix: str,
    callback_url: str,
    callback_api_key_enc: str,
) -> int:
    """Insert an odoo_instances row and return its id."""
    with db.session() as s:
        row = OdooInstance(
            name=name,
            key_hash=key_hash,
            key_prefix=key_prefix,
            callback_url=callback_url,
            callback_api_key_enc=callback_api_key_enc,
        )
        s.add(row)
        s.flush()
        return row.id


def get_odoo_instance(db: Database, instance_id: int) -> dict | None:
    """Return an odoo_instances row as a dict (incl. callback config), or None."""
    with db.session() as s:
        row = s.get(OdooInstance, instance_id)
        if row is None:
            return None
        return {
            "id": row.id,
            "name": row.name,
            "key_prefix": row.key_prefix,
            "key_hash": row.key_hash,
            "callback_url": row.callback_url,
            "callback_api_key_enc": row.callback_api_key_enc,
            "active": row.active,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }


def rotate_odoo_instance_key(
    db: Database, instance_id: int, *, key_hash: str, key_prefix: str
) -> bool:
    """Replace the inbound key hash/prefix. Returns False if the row is missing."""
    with db.session() as s:
        row = s.get(OdooInstance, instance_id)
        if row is None:
            return False
        row.key_hash = key_hash
        row.key_prefix = key_prefix
        row.updated_at = datetime.now(timezone.utc)
        return True


def update_odoo_instance(db: Database, instance_id: int, **fields: object) -> bool:
    """Update name/callback_url/callback_api_key_enc/active. Returns False if missing."""
    allowed = {"name", "callback_url", "callback_api_key_enc", "active"}
    with db.session() as s:
        row = s.get(OdooInstance, instance_id)
        if row is None:
            return False
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"update_odoo_instance: unknown field {key!r}")
            setattr(row, key, value)
        row.updated_at = datetime.now(timezone.utc)
        return True
```

(`datetime`, `timezone` are already imported at the top of `writers.py`.)

- [ ] **Step 3b: Run writer test to verify it passes**

Run: `cd worker && .venv/bin/python -m pytest tests/test_odoo_instance_writers.py -v`
Expected: PASS (3 tests).

- [ ] **Step 4a: Write the failing query test**

Create `api/tests/test_odoo_instance_queries.py`:

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.queries import odoo_instances as q
from reva.db import Base, Database, create_engine_from_url, writers
from reva.db.models import TicketAnalysis, TicketIssueRun


@pytest.fixture()
def db() -> Database:
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def test_resolve_by_key_active_only(db: Database) -> None:
    # resolve_odoo_instance_by_key takes the RAW token and hashes it, so seed
    # the instance with the SHA-256 of a known token.
    import hashlib

    token = "reva_odoo_secret"
    iid = writers.create_odoo_instance(
        db, name="ACME", key_hash=hashlib.sha256(token.encode()).hexdigest(),
        key_prefix="p", callback_url="", callback_api_key_enc="",
    )
    assert q.resolve_odoo_instance_by_key(db, token) == (iid, "ACME")
    assert q.resolve_odoo_instance_by_key(db, "wrong-token") is None

    # Deactivated instances no longer resolve.
    writers.update_odoo_instance(db, iid, active=False)
    assert q.resolve_odoo_instance_by_key(db, token) is None


def test_cost_windows_split_by_task(db: Database) -> None:
    iid = writers.create_odoo_instance(
        db, name="ACME", key_hash="h", key_prefix="p",
        callback_url="", callback_api_key_enc="",
    )
    now = datetime.now(timezone.utc)
    with db.session() as s:
        s.add(TicketAnalysis(
            odoo_instance_id=iid, ticket_id=1, model_name="m", field_name="f",
            input_text="t", status="completed", estimated_cost_usd=2,
            input_tokens=10, output_tokens=5, created_at=now,
        ))
        s.add(TicketAnalysis(  # 40 days ago → outside 30d window
            odoo_instance_id=iid, ticket_id=2, model_name="m", field_name="f",
            input_text="t", status="completed", estimated_cost_usd=7,
            input_tokens=1, output_tokens=1, created_at=now - timedelta(days=40),
        ))
        s.add(TicketIssueRun(
            odoo_instance_id=iid, ticket_id=3, model_name="m",
            github_url="g", name="n", description="d", analysis_html="",
            priority="1", ticket_url="u", status="completed",
            estimated_cost_usd=3, input_tokens=20, output_tokens=8, created_at=now,
        ))
    cost = q.get_odoo_instance_cost(db, iid)
    assert cost["lifetime"]["analysis"]["cost_usd"] == 9.0
    assert cost["last_30d"]["analysis"]["cost_usd"] == 2.0
    assert cost["lifetime"]["issues"]["cost_usd"] == 3.0
    assert cost["last_24h"]["issues"]["input_tokens"] == 20
```

- [ ] **Step 4b: Write the query module**

Create `api/app/queries/odoo_instances.py`:

```python
"""Read queries for odoo_instances: key resolution, list, and cost rollups."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from reva.db.engine import Database
from reva.db.models import OdooInstance, TicketAnalysis, TicketIssueRun


def resolve_odoo_instance_by_key(db: Database, token: str) -> tuple[int, str] | None:
    """Return (id, name) for the ACTIVE instance whose inbound key is `token`."""
    digest = hashlib.sha256(token.encode()).hexdigest()
    with db.session() as s:
        row = s.execute(
            select(OdooInstance.id, OdooInstance.name).where(
                OdooInstance.key_hash == digest,
                OdooInstance.active.is_(True),
            )
        ).first()
        return (row[0], row[1]) if row is not None else None


def _zero_task() -> dict:
    return {"cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0, "count": 0}


def _sum_for(s, model, instance_id: int, since: datetime | None) -> dict:
    q = select(
        func.coalesce(func.sum(model.estimated_cost_usd), 0),
        func.coalesce(func.sum(model.input_tokens), 0),
        func.coalesce(func.sum(model.output_tokens), 0),
        func.count(model.id),
    ).where(model.odoo_instance_id == instance_id)
    if since is not None:
        q = q.where(model.created_at >= since)
    cost, inp, out, cnt = s.execute(q).one()
    return {
        "cost_usd": float(cost),
        "input_tokens": int(inp),
        "output_tokens": int(out),
        "count": int(cnt),
    }


def get_odoo_instance_cost(db: Database, instance_id: int) -> dict:
    """Per-instance cost: lifetime / 24h / 30d, each split analysis vs issues."""
    now = datetime.now(timezone.utc)
    windows = {
        "lifetime": None,
        "last_24h": now - timedelta(hours=24),
        "last_30d": now - timedelta(days=30),
    }
    with db.session() as s:
        out: dict = {}
        for label, since in windows.items():
            out[label] = {
                "analysis": _sum_for(s, TicketAnalysis, instance_id, since),
                "issues": _sum_for(s, TicketIssueRun, instance_id, since),
            }
    return out


def list_odoo_instances(db: Database) -> list[dict]:
    """All instances (newest first) with their cost rollup folded in."""
    with db.session() as s:
        rows = s.execute(
            select(OdooInstance).order_by(OdooInstance.created_at.desc())
        ).scalars().all()
        instances = [
            {
                "id": r.id,
                "name": r.name,
                "key_prefix": r.key_prefix,
                "callback_url": r.callback_url,
                "active": r.active,
                "created_at": r.created_at,
            }
            for r in rows
        ]
    for inst in instances:
        inst["cost"] = get_odoo_instance_cost(db, inst["id"])
    return instances
```

- [ ] **Step 4c: Run query test to verify it passes**

Run: `cd api && .venv/bin/python -m pytest tests/test_odoo_instance_queries.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add reva/db/writers.py api/app/queries/odoo_instances.py worker/tests/test_odoo_instance_writers.py api/tests/test_odoo_instance_queries.py
git commit -m "feat(db,api): odoo_instances writers + key-resolve/list/cost queries"
```

---

### Task 4: Management API routes (`/api/v1/odoo-instances`, master-gated)

**Files:**
- Create: `api/app/schemas/odoo_instances.py`
- Create: `api/app/routes/v1/odoo_instances.py`
- Modify: `api/app/routes/v1/__init__.py` (register the management router — full restructure happens in Task 5; here just add `odoo_instances.router` under the existing master gate)
- Test: `api/tests/test_v1_odoo_instances.py`

**Interfaces:**
- Consumes: writers + queries from Task 3; `secrets_crypto.encrypt` from Task 2.
- Produces: `POST/GET /api/v1/odoo-instances`, `GET /api/v1/odoo-instances/{id}/cost`, `POST /api/v1/odoo-instances/{id}/rotate-key`, `PATCH /api/v1/odoo-instances/{id}`. Helper `mint_inbound_key() -> tuple[str, str, str]` (plaintext, sha256 hex, prefix) lives in this route module.

- [ ] **Step 1: Write the failing test**

Create `api/tests/test_v1_odoo_instances.py`:

```python
"""CRUD for /api/v1/odoo-instances (master-key, admin-only)."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from app.dependencies import get_db, get_settings
from app.main import app
from app.settings import Settings
from reva.db import Base, Database, create_engine_from_url


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("REVA_SECRET_KEY", Fernet.generate_key().decode())
    engine = create_engine_from_url(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = Database(engine)
    settings = Settings(
        database_url="sqlite:///:memory:", github_app_id=1,
        github_webhook_secret="x", github_private_key="x",
        redis_url="redis://localhost:6379/0",
    )
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: settings
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_create_returns_plaintext_key_once(client):
    r = client.post("/api/v1/odoo-instances", json={
        "name": "ACME", "callback_url": "https://odoo.acme/write-field",
        "callback_api_key": "outbound-secret",
    })
    assert r.status_code == 201
    body = r.json()
    assert body["api_key"].startswith("reva_odoo_")
    assert body["name"] == "ACME"
    instance_id = body["id"]

    # GET never returns the secret.
    lst = client.get("/api/v1/odoo-instances").json()
    assert "api_key" not in lst["items"][0]
    assert lst["items"][0]["key_prefix"].startswith("reva_odoo_")
    assert "cost" in lst["items"][0]

    # Rotate mints a new key.
    rot = client.post(f"/api/v1/odoo-instances/{instance_id}/rotate-key")
    assert rot.status_code == 200
    assert rot.json()["api_key"] != body["api_key"]


def test_patch_toggles_active(client):
    iid = client.post("/api/v1/odoo-instances", json={
        "name": "ACME", "callback_url": "", "callback_api_key": "",
    }).json()["id"]
    r = client.patch(f"/api/v1/odoo-instances/{iid}", json={"active": False})
    assert r.status_code == 200
    assert client.get("/api/v1/odoo-instances").json()["items"][0]["active"] is False


def test_create_requires_secret_key_when_outbound_set(client, monkeypatch):
    monkeypatch.delenv("REVA_SECRET_KEY", raising=False)
    r = client.post("/api/v1/odoo-instances", json={
        "name": "NoSecret", "callback_url": "https://x/write-field",
        "callback_api_key": "outbound-secret",
    })
    assert r.status_code == 400
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd api && .venv/bin/python -m pytest tests/test_v1_odoo_instances.py -v`
Expected: FAIL — 404 on `POST /api/v1/odoo-instances` (route not registered).

- [ ] **Step 3a: Write the schemas**

Create `api/app/schemas/odoo_instances.py`:

```python
"""Pydantic schemas for the odoo-instances management endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class OdooInstanceCreate(BaseModel):
    name: str
    callback_url: str = ""
    callback_api_key: str = ""  # plaintext outbound key; encrypted before storage


class OdooInstanceUpdate(BaseModel):
    name: str | None = None
    callback_url: str | None = None
    callback_api_key: str | None = None  # plaintext; re-encrypted when present
    active: bool | None = None


class TaskCost(BaseModel):
    cost_usd: float
    input_tokens: int
    output_tokens: int
    count: int


class WindowCost(BaseModel):
    analysis: TaskCost
    issues: TaskCost


class OdooInstanceCost(BaseModel):
    lifetime: WindowCost
    last_24h: WindowCost
    last_30d: WindowCost


class OdooInstanceSummary(BaseModel):
    id: int
    name: str
    key_prefix: str
    callback_url: str
    active: bool
    created_at: datetime
    cost: OdooInstanceCost


class OdooInstancePage(BaseModel):
    items: list[OdooInstanceSummary]
    total: int


class OdooInstanceCreated(BaseModel):
    """Returned ONCE on create/rotate — carries the plaintext inbound key."""

    id: int
    name: str
    key_prefix: str
    api_key: str  # plaintext, shown once
```

- [ ] **Step 3b: Write the route module**

Create `api/app/routes/v1/odoo_instances.py`:

```python
"""Admin (master-key) CRUD for Odoo instances.

Mints a per-instance inbound key (stored hashed, returned once), and encrypts
the outbound callback key at rest under REVA_SECRET_KEY.
"""

from __future__ import annotations

import hashlib
import secrets

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.dependencies import actor_from_request, get_db
from app.queries import odoo_instances as q
from app.schemas.odoo_instances import (
    OdooInstanceCost,
    OdooInstanceCreate,
    OdooInstanceCreated,
    OdooInstancePage,
    OdooInstanceSummary,
    OdooInstanceUpdate,
)
from reva import secrets_crypto
from reva.db import writers
from reva.db.engine import Database

router = APIRouter()
logger = structlog.get_logger()


def mint_inbound_key() -> tuple[str, str, str]:
    """Return (plaintext, sha256-hex, display-prefix) for a fresh inbound key."""
    plaintext = "reva_odoo_" + secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(plaintext.encode()).hexdigest()
    return plaintext, key_hash, plaintext[:16]


def _seal_outbound(plaintext: str) -> str:
    try:
        return secrets_crypto.encrypt(plaintext)
    except RuntimeError as exc:  # REVA_SECRET_KEY missing
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/odoo-instances", response_model=OdooInstancePage)
def list_instances(db: Database = Depends(get_db)) -> dict:
    items = q.list_odoo_instances(db)
    return {
        "items": [OdooInstanceSummary.model_validate(i) for i in items],
        "total": len(items),
    }


@router.post("/odoo-instances", status_code=201, response_model=OdooInstanceCreated)
def create_instance(
    body: OdooInstanceCreate, request: Request, db: Database = Depends(get_db)
) -> dict:
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="name is required")
    callback_api_key_enc = _seal_outbound(body.callback_api_key)
    plaintext, key_hash, key_prefix = mint_inbound_key()
    instance_id = writers.create_odoo_instance(
        db, name=name, key_hash=key_hash, key_prefix=key_prefix,
        callback_url=body.callback_url.strip(),
        callback_api_key_enc=callback_api_key_enc,
    )
    writers.record_admin_action(
        db, action="create_odoo_instance", actor=actor_from_request(request),
        target=name, detail={"instance_id": instance_id},
    )
    return {"id": instance_id, "name": name, "key_prefix": key_prefix, "api_key": plaintext}


@router.get("/odoo-instances/{instance_id}/cost", response_model=OdooInstanceCost)
def instance_cost(instance_id: int, db: Database = Depends(get_db)) -> dict:
    if writers.get_odoo_instance(db, instance_id) is None:
        raise HTTPException(status_code=404, detail="Odoo instance not found")
    return q.get_odoo_instance_cost(db, instance_id)


@router.post(
    "/odoo-instances/{instance_id}/rotate-key", response_model=OdooInstanceCreated
)
def rotate_key(
    instance_id: int, request: Request, db: Database = Depends(get_db)
) -> dict:
    plaintext, key_hash, key_prefix = mint_inbound_key()
    if not writers.rotate_odoo_instance_key(
        db, instance_id, key_hash=key_hash, key_prefix=key_prefix
    ):
        raise HTTPException(status_code=404, detail="Odoo instance not found")
    row = writers.get_odoo_instance(db, instance_id)
    writers.record_admin_action(
        db, action="rotate_odoo_instance_key", actor=actor_from_request(request),
        target=row["name"], detail={"instance_id": instance_id},
    )
    return {"id": instance_id, "name": row["name"], "key_prefix": key_prefix, "api_key": plaintext}


@router.patch("/odoo-instances/{instance_id}", status_code=200)
def update_instance(
    instance_id: int, body: OdooInstanceUpdate, request: Request,
    db: Database = Depends(get_db),
) -> dict:
    fields: dict[str, object] = {}
    if body.name is not None:
        fields["name"] = body.name.strip()
    if body.callback_url is not None:
        fields["callback_url"] = body.callback_url.strip()
    if body.callback_api_key is not None:
        fields["callback_api_key_enc"] = _seal_outbound(body.callback_api_key)
    if body.active is not None:
        fields["active"] = body.active
    if not fields:
        raise HTTPException(status_code=422, detail="no fields to update")
    if not writers.update_odoo_instance(db, instance_id, **fields):
        raise HTTPException(status_code=404, detail="Odoo instance not found")
    writers.record_admin_action(
        db, action="update_odoo_instance", actor=actor_from_request(request),
        target=str(instance_id), detail={k: v for k, v in fields.items() if k != "callback_api_key_enc"},
    )
    return {"id": instance_id, "updated": True}
```

> Note: `writers.record_admin_action` is the existing admin-audit writer used by `repos.py`. If its signature differs, match the call already in `api/app/routes/v1/repos.py::add_repo` verbatim.

- [ ] **Step 3c: Register the router (interim, under the master gate)**

In `api/app/routes/v1/__init__.py`, add `odoo_instances` to the import list and include it on the existing `router`:

```python
from app.routes.v1 import (
    admin,
    audits,
    failures,
    findings,
    metrics,
    odoo_instances,
    pending,
    repos,
    reviews,
    ticket_analyses,
    ticket_issues,
)
```

```python
router.include_router(odoo_instances.router)
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd api && .venv/bin/python -m pytest tests/test_v1_odoo_instances.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add api/app/schemas/odoo_instances.py api/app/routes/v1/odoo_instances.py api/app/routes/v1/__init__.py api/tests/test_v1_odoo_instances.py
git commit -m "feat(api): admin CRUD for /api/v1/odoo-instances + per-instance cost"
```

---

### Task 5: Inbound instance-key auth + router restructure (scoping)

**Files:**
- Modify: `api/app/dependencies.py` (add `ResolvedOdooInstance` + `require_odoo_instance`)
- Modify: `api/app/routes/v1/__init__.py` (split into master-gated + instance-gated routers)
- Modify: `api/app/routes/v1/ticket_analyses.py` (move POST create onto a `create_router`)
- Modify: `api/app/routes/v1/ticket_issues.py` (move POST create onto a `create_router`)
- Test: `api/tests/test_odoo_instance_auth.py`

**Interfaces:**
- Consumes: `resolve_odoo_instance_by_key` (Task 3).
- Produces: `ResolvedOdooInstance(id, name)`; `require_odoo_instance(request, db) -> ResolvedOdooInstance`; `ticket_analyses.create_router`; `ticket_issues.create_router`. (Create handlers still need to STAMP the id — that lands in Tasks 6 & 7. This task wires auth + routing and asserts the gating.)

- [ ] **Step 1: Write the failing test**

Create `api/tests/test_odoo_instance_auth.py`:

```python
"""Auth scoping: instance keys reach only the two create routes; master key is
rejected there but works everywhere else."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from app.dependencies import get_db, get_settings
from app.main import app
from app.settings import Settings
from reva.db import Base, Database, create_engine_from_url


@pytest.fixture()
def ctx(monkeypatch):
    monkeypatch.setenv("REVA_SECRET_KEY", Fernet.generate_key().decode())
    engine = create_engine_from_url(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = Database(engine)
    settings = Settings(
        database_url="sqlite:///:memory:", github_app_id=1,
        github_webhook_secret="x", github_private_key="x",
        redis_url="redis://localhost:6379/0",
        api_key="master-secret", require_api_key=True,
    )
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: settings

    class FakeQueue:
        def enqueue(self, *a, **k):
            class J:  # noqa: D401
                id = "rq:job:1"
            return J()

    prev = getattr(app.state, "rq_queue", None)
    app.state.rq_queue = FakeQueue()
    client = TestClient(app)
    # Mint an instance via the admin API (master key).
    h = {"Authorization": "Bearer master-secret"}
    key = client.post("/api/v1/odoo-instances", headers=h, json={
        "name": "ACME", "callback_url": "", "callback_api_key": "",
    }).json()["api_key"]
    yield client, key
    app.state.rq_queue = prev
    app.dependency_overrides.clear()


PAYLOAD = {"ticket_id": 7, "model_name": "helpdesk.ticket",
           "field_name": "x", "text": "hi"}


def test_instance_key_can_create(ctx):
    client, key = ctx
    r = client.post("/api/v1/ticket-analysis",
                    headers={"Authorization": f"Bearer {key}"}, json=PAYLOAD)
    assert r.status_code == 202


def test_master_key_rejected_on_create(ctx):
    client, _ = ctx
    r = client.post("/api/v1/ticket-analysis",
                    headers={"Authorization": "Bearer master-secret"}, json=PAYLOAD)
    assert r.status_code == 401


def test_instance_key_rejected_on_management(ctx):
    client, key = ctx
    r = client.get("/api/v1/odoo-instances", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 401


def test_instance_key_rejected_on_read_routes(ctx):
    client, key = ctx
    assert client.get("/api/v1/repos",
                      headers={"Authorization": f"Bearer {key}"}).status_code == 401
    assert client.get("/api/v1/ticket-analyses",
                      headers={"Authorization": f"Bearer {key}"}).status_code == 401
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd api && .venv/bin/python -m pytest tests/test_odoo_instance_auth.py -v`
Expected: FAIL — `test_master_key_rejected_on_create` fails (master key currently passes; create route still under master gate) and/or import errors for `create_router`.

- [ ] **Step 3a: Add the auth dependency**

In `api/app/dependencies.py`, add the import and dependency (the file already imports `Depends`, `HTTPException`, `Request`):

```python
from dataclasses import dataclass
```

```python
@dataclass(frozen=True)
class ResolvedOdooInstance:
    id: int
    name: str


def require_odoo_instance(
    request: Request, db: Database = Depends(get_db)
) -> ResolvedOdooInstance:
    """Resolve the calling Odoo instance from its Bearer key, or 401.

    The instance key IS the identity. The master key does not resolve here (it
    is not an instance), so it is correctly rejected on the create routes.
    """
    from app.queries import odoo_instances as q  # local import: avoid a cycle

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Odoo instance key")
    token = auth[len("Bearer ") :]
    resolved = q.resolve_odoo_instance_by_key(db, token)
    if resolved is None:
        raise HTTPException(status_code=401, detail="Invalid Odoo instance key")
    return ResolvedOdooInstance(id=resolved[0], name=resolved[1])
```

- [ ] **Step 3b: Move the create handlers onto `create_router`**

In `api/app/routes/v1/ticket_analyses.py`, just below `router = APIRouter()`, add:

```python
create_router = APIRouter()  # instance-key gated (see routes/v1/__init__.py)
```

Change the decorator on `submit_ticket_analysis` from `@router.post(` to `@create_router.post(`. Leave the GET/list/requeue handlers on `router`.

In `api/app/routes/v1/ticket_issues.py`, add the same `create_router = APIRouter()` line below `router = APIRouter()`, and change the decorator on `submit_create_issues` from `@router.post(` to `@create_router.post(`. Leave list/GET/requeue on `router`.

- [ ] **Step 3c: Restructure the v1 router into master + instance gates**

Replace the body of `api/app/routes/v1/__init__.py` with:

```python
"""Aggregate all /api/v1 sub-routers, split by auth gate.

- master gate (require_api_key): every admin/read/management route, incl. the
  ticket read/list/requeue handlers and the odoo-instances CRUD.
- instance gate (require_odoo_instance): ONLY the two Odoo create routes.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.dependencies import require_api_key, require_odoo_instance
from app.ratelimit import rate_limit
from app.routes.v1 import (
    admin,
    audits,
    failures,
    findings,
    metrics,
    odoo_instances,
    pending,
    repos,
    reviews,
    ticket_analyses,
    ticket_issues,
)

router = APIRouter()

_master = APIRouter(dependencies=[Depends(require_api_key), Depends(rate_limit)])
_master.include_router(reviews.router)
_master.include_router(findings.router)
_master.include_router(repos.router)
_master.include_router(failures.router)
_master.include_router(metrics.router)
_master.include_router(pending.router)
_master.include_router(admin.router)
_master.include_router(ticket_analyses.router)
_master.include_router(ticket_issues.router)
_master.include_router(audits.router)
_master.include_router(odoo_instances.router)

_instance = APIRouter(dependencies=[Depends(require_odoo_instance), Depends(rate_limit)])
_instance.include_router(ticket_analyses.create_router)
_instance.include_router(ticket_issues.create_router)

router.include_router(_master)
router.include_router(_instance)
```

- [ ] **Step 3d: Keep existing ticket tests green**

The existing `api/tests/test_v1_ticket_analyses.py` and `test_v1_ticket_issues.py` POST to the create routes with no auth. They now need an instance key. In each, change the fixture to set `REVA_SECRET_KEY`, create an instance via `POST /api/v1/odoo-instances`, and add an `Authorization: Bearer <key>` header to every create POST. Concretely, in `test_v1_ticket_analyses.py`'s `client_db_queue` fixture, after building `TestClient(app)`:

```python
    import os
    from cryptography.fernet import Fernet
    os.environ["REVA_SECRET_KEY"] = Fernet.generate_key().decode()
    tc = TestClient(app)
    key = tc.post("/api/v1/odoo-instances", json={
        "name": "test", "callback_url": "", "callback_api_key": "",
    }).json()["api_key"]
    yield tc, db, queue, {"Authorization": f"Bearer {key}"}
```

and update each `client.post("/api/v1/ticket-analysis", json=...)` call in that file to `client.post("/api/v1/ticket-analysis", json=..., headers=headers)`, unpacking the extra `headers` value from the fixture. Apply the equivalent change to `test_v1_ticket_issues.py`.

> These two files are auth-disabled today (`require_api_key=False`, no `api_key`), so the master gate doesn't block their GETs; only the create POSTs need the instance header.

- [ ] **Step 4: Run to verify it passes**

Run:
```
cd api && .venv/bin/python -m pytest tests/test_odoo_instance_auth.py tests/test_v1_ticket_analyses.py tests/test_v1_ticket_issues.py tests/test_auth.py -v
```
Expected: PASS (all). `test_auth.py` confirms master-gated read routes still behave.

- [ ] **Step 5: Commit**

```bash
git add api/app/dependencies.py api/app/routes/v1/__init__.py api/app/routes/v1/ticket_analyses.py api/app/routes/v1/ticket_issues.py api/tests/test_odoo_instance_auth.py api/tests/test_v1_ticket_analyses.py api/tests/test_v1_ticket_issues.py
git commit -m "feat(api): instance-key auth for Odoo create routes; master-only elsewhere"
```

---

### Task 6: Stamp `odoo_instance_id` on ticket-analysis create + scope dedup

**Files:**
- Modify: `reva/types.py` (`TicketJobParams.odoo_instance_id: int`)
- Modify: `reva/db/writers.py` (`record_ticket_analysis_created` sets the column; `get_pending_ticket_analysis` filters by instance)
- Modify: `api/app/routes/v1/ticket_analyses.py` (create + requeue pass `odoo_instance_id`)
- Test: extend `api/tests/test_odoo_instance_auth.py` with a stamping assertion

**Interfaces:**
- Consumes: `require_odoo_instance` (Task 5).
- Produces: `TicketJobParams.odoo_instance_id`; instance-scoped analysis dedup.

- [ ] **Step 1: Write the failing test**

Append to `api/tests/test_odoo_instance_auth.py`:

```python
def test_analysis_stamps_instance_id(ctx):
    client, key = ctx
    r = client.post("/api/v1/ticket-analysis",
                    headers={"Authorization": f"Bearer {key}"}, json=PAYLOAD)
    assert r.status_code == 202
    # The enqueued job params carry the resolved instance id.
    func_path, params, _ = app.state.rq_queue.enqueued[-1]
    assert params["odoo_instance_id"] >= 1
```

Update the `ctx` fixture's `FakeQueue` to record calls (mirror `test_v1_ticket_analyses.py`):

```python
    class FakeQueue:
        def __init__(self):
            self.enqueued = []

        def enqueue(self, func_path, params, **kwargs):
            self.enqueued.append((func_path, params, kwargs))
            class J:
                id = f"rq:job:{len(self.enqueued)}"
            return J()
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd api && .venv/bin/python -m pytest tests/test_odoo_instance_auth.py::test_analysis_stamps_instance_id -v`
Expected: FAIL — `KeyError: 'odoo_instance_id'` (params don't carry it yet).

- [ ] **Step 3a: Add the param field**

In `reva/types.py`, find `class TicketJobParams` and add the field (place it alongside `analysis_id`):

```python
    odoo_instance_id: int
```

- [ ] **Step 3b: Persist the column + scope dedup**

In `reva/db/writers.py`, in `record_ticket_analysis_created`, add `odoo_instance_id=params.odoo_instance_id,` to the `TicketAnalysis(...)` constructor.

Locate `get_pending_ticket_analysis` (the dedup read used by the route). Add an `odoo_instance_id` parameter and filter:

```python
def get_pending_ticket_analysis(
    db: Database, ticket_id: int, model_name: str, field_name: str, odoo_instance_id: int
) -> dict | None:
    """Return the pending analysis for (instance, ticket, model, field), or None."""
    with db.session() as s:
        row = s.execute(
            select(TicketAnalysis).where(
                TicketAnalysis.odoo_instance_id == odoo_instance_id,
                TicketAnalysis.ticket_id == ticket_id,
                TicketAnalysis.model_name == model_name,
                TicketAnalysis.field_name == field_name,
                TicketAnalysis.status == "pending",
            )
        ).scalars().first()
        if row is None:
            return None
        return {"id": row.id, "job_id": row.job_id}
```

> Match the existing return shape of `get_pending_ticket_analysis` — if it currently returns more keys, keep them. Only the signature (new arg) + the `odoo_instance_id` WHERE clause are added.

- [ ] **Step 3c: Wire the route**

In `api/app/routes/v1/ticket_analyses.py`:
- Add the dependency import: `from app.dependencies import get_db, require_odoo_instance, ResolvedOdooInstance`.
- In `submit_ticket_analysis`, add the parameter `instance: ResolvedOdooInstance = Depends(require_odoo_instance),`.
- Pass `odoo_instance_id=instance.id` into BOTH `TicketJobParams(...)` constructions (the stub and the real one).
- Update the dedup call: `existing = writers.get_pending_ticket_analysis(db, body.ticket_id, body.model_name, body.field_name, instance.id)`.
- In `requeue_ticket_analysis`, add `odoo_instance_id=row["odoo_instance_id"]` to the `TicketJobParams(...)`. (Ensure `get_ticket_analysis` returns `odoo_instance_id`; if not, add it to that reader's dict.)

- [ ] **Step 4: Run to verify it passes**

Run: `cd api && .venv/bin/python -m pytest tests/test_odoo_instance_auth.py tests/test_v1_ticket_analyses.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add reva/types.py reva/db/writers.py api/app/routes/v1/ticket_analyses.py api/tests/test_odoo_instance_auth.py
git commit -m "feat(api): stamp + scope ticket-analysis by odoo_instance_id"
```

---

### Task 7: Stamp `odoo_instance_id` on create-issues + per-instance callback in the worker

**Files:**
- Modify: `reva/types.py` (`TicketIssueJobParams.odoo_instance_id: int`)
- Modify: `reva/db/writers.py` (`record_ticket_issue_run_created` sets the column; `get_pending_ticket_issue_run` + `get_ticket_issue_run` + `update_ticket_issue_state` carry `odoo_instance_id`)
- Modify: `api/app/routes/v1/ticket_issues.py` (create + requeue pass `odoo_instance_id`)
- Modify: `worker/worker/runner.py` (add `build_odoo_client`; drop `odoo` from `WorkerContext` + its construction)
- Modify: `worker/worker/ticket_runner.py` (build client from `params.odoo_instance_id`)
- Modify: `worker/worker/ticket_issue_runner.py` (build client per run / per affected record)
- Test: `worker/tests/test_build_odoo_client.py`; update `worker/tests/test_ticket_runner.py`

**Interfaces:**
- Consumes: `secrets_crypto.decrypt` (Task 2), `writers.get_odoo_instance` (Task 3), `TicketJobParams.odoo_instance_id` (Task 6).
- Produces: `build_odoo_client(ctx, odoo_instance_id) -> OdooCallbackClient`; `TicketIssueJobParams.odoo_instance_id`.

- [ ] **Step 1: Write the failing test (worker callback builder)**

Create `worker/tests/test_build_odoo_client.py`:

```python
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from reva.db import Base, Database, create_engine_from_url, writers
from reva.errors import PermanentError


@pytest.fixture()
def db(monkeypatch) -> Database:
    monkeypatch.setenv("REVA_SECRET_KEY", Fernet.generate_key().decode())
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def test_builds_client_from_instance(db, monkeypatch):
    from reva import secrets_crypto
    from worker.runner import WorkerContext, build_odoo_client

    iid = writers.create_odoo_instance(
        db, name="ACME", key_hash="h", key_prefix="p",
        callback_url="https://odoo.acme/write-field",
        callback_api_key_enc=secrets_crypto.encrypt("outbound-secret"),
    )
    ctx = WorkerContext.__new__(WorkerContext)  # only .db is needed by the builder
    object.__setattr__(ctx, "db", db)
    client = build_odoo_client(ctx, iid)
    assert client._callback_url == "https://odoo.acme/write-field"
    assert client._api_key == "outbound-secret"


def test_missing_instance_raises(db):
    from worker.runner import WorkerContext, build_odoo_client

    ctx = WorkerContext.__new__(WorkerContext)
    object.__setattr__(ctx, "db", db)
    with pytest.raises(PermanentError):
        build_odoo_client(ctx, 999)
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd worker && .venv/bin/python -m pytest tests/test_build_odoo_client.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_odoo_client'`.

- [ ] **Step 3a: Worker — add builder, drop the global odoo client**

In `worker/worker/runner.py`:
- Add imports: `from reva import secrets_crypto` and (already imported) `from reva.errors import PermanentError, TransientError`.
- Remove the `odoo: OdooCallbackClient` field from `@dataclass(frozen=True) class WorkerContext`.
- In `build_worker_context`, delete the `odoo = OdooCallbackClient(...)` block and the `odoo=odoo,` argument to `WorkerContext(...)`.
- Add the builder function:

```python
def build_odoo_client(ctx: "WorkerContext", odoo_instance_id: int) -> OdooCallbackClient:
    """Construct an OdooCallbackClient for one instance (decrypts its key)."""
    inst = writers.get_odoo_instance(ctx.db, odoo_instance_id)
    if inst is None:
        raise PermanentError(f"odoo_instance {odoo_instance_id} not found")
    api_key = secrets_crypto.decrypt(inst["callback_api_key_enc"])
    return OdooCallbackClient(callback_url=inst["callback_url"], api_key=api_key)
```

(Keep the `from reva.odoo_client import OdooCallbackClient` import — it's now used by `build_odoo_client`.)

- [ ] **Step 3b: Worker — ticket_runner uses the per-instance client**

In `worker/worker/ticket_runner.py`:
- Change the import `from worker.runner import get_context` to `from worker.runner import build_odoo_client, get_context`.
- At the top of `run_ticket_analysis`, after `params = TicketJobParams.model_validate(job_params)`, add:

```python
    odoo = build_odoo_client(ctx, params.odoo_instance_id)
```

- Replace every `ctx.odoo.` with `odoo.` (the `reset_status` and `write_field` calls).

- [ ] **Step 3c: Worker — ticket_issue_runner uses per-instance / per-record clients**

In `worker/worker/ticket_issue_runner.py`:
- Import `build_odoo_client` from `worker.runner` (add to the existing `get_context` import).
- In `run_ticket_issues`, after `params = TicketIssueJobParams.model_validate(job_params)`, add `odoo = build_odoo_client(ctx, params.odoo_instance_id)`. Replace `ctx.odoo.issues_created(...)` with `odoo.issues_created(...)`. In `_send_failed_callback`, build/accept the `odoo` client (pass it in, or rebuild via `build_odoo_client(ctx, params.odoo_instance_id)` inside it) and replace `ctx.odoo`.
- In `sync_ticket_issue_state`, each affected `record` carries its own instance. Replace `ctx.odoo.issue_state(...)` with a per-record client:

```python
        odoo = build_odoo_client(ctx, record["odoo_instance_id"])
        odoo.issue_state(
            ticket_id=record["ticket_id"],
            model_name=record["model_name"],
            number=number, state=state, issues=snapshot,
        )
```

- [ ] **Step 3d: Worker — params + writer + readers carry the id**

- `reva/types.py`: in `class TicketIssueJobParams`, add `odoo_instance_id: int`.
- `reva/db/writers.py`:
  - `record_ticket_issue_run_created`: add `odoo_instance_id=params.odoo_instance_id,` to the `TicketIssueRun(...)` constructor.
  - `get_pending_ticket_issue_run`: add an `odoo_instance_id` arg and `TicketIssueRun.odoo_instance_id == odoo_instance_id` to the WHERE clause.
  - `get_ticket_issue_run`: add `"odoo_instance_id": row.odoo_instance_id,` to the returned dict.
  - `update_ticket_issue_state`: include `"odoo_instance_id": <row>.odoo_instance_id` in each affected-record dict it returns.

- [ ] **Step 3e: API — create-issues route stamps the id**

In `api/app/routes/v1/ticket_issues.py`:
- Import `require_odoo_instance, ResolvedOdooInstance` from `app.dependencies`.
- In `submit_create_issues`, add `instance: ResolvedOdooInstance = Depends(require_odoo_instance),`.
- Pass `odoo_instance_id=instance.id` into both `TicketIssueJobParams(run_id=0, **body.model_dump())` → change to `TicketIssueJobParams(run_id=0, odoo_instance_id=instance.id, **body.model_dump())` and the real-params line likewise.
- Update both dedup calls to `writers.get_pending_ticket_issue_run(db, body.ticket_id, body.model_name, instance.id)`.
- In the requeue handler, build params with `odoo_instance_id=row["odoo_instance_id"]`.

- [ ] **Step 3f: Update the worker ticket_runner test**

In `worker/tests/test_ticket_runner.py`:
- Remove the `odoo=odoo,` argument from the `WorkerContext(...)` construction (the field no longer exists).
- Monkeypatch the builder so the FakeOdoo is used: at the top of each test (or via a fixture) `monkeypatch.setattr("worker.ticket_runner.build_odoo_client", lambda ctx, _id: odoo)`.
- Ensure each `TicketJobParams(...)` built in the test includes `odoo_instance_id=1`.

- [ ] **Step 4: Run to verify it passes**

Run:
```
cd worker && .venv/bin/python -m pytest tests/test_build_odoo_client.py tests/test_ticket_runner.py tests/test_ticket_issue_runner.py -v
cd api && .venv/bin/python -m pytest tests/test_v1_ticket_issues.py tests/test_odoo_instance_auth.py -v
```
Expected: PASS. (Fix any other test referencing `ctx.odoo` the same way: drop the field, monkeypatch `build_odoo_client`.)

- [ ] **Step 5: Commit**

```bash
git add reva/types.py reva/db/writers.py api/app/routes/v1/ticket_issues.py worker/worker/runner.py worker/worker/ticket_runner.py worker/worker/ticket_issue_runner.py worker/tests/
git commit -m "feat(worker,api): per-instance Odoo callbacks; stamp+scope create-issues"
```

---

### Task 8: Settings — add `REVA_SECRET_KEY`, remove `ODOO_CALLBACK_*`

**Files:**
- Modify: `worker/worker/settings.py`
- Modify: `api/app/settings.py`
- Modify: `README.md` / `.env.example` (document `REVA_SECRET_KEY`; drop the two removed vars) — fold doc edits into this task
- Test: `worker/tests/test_settings.py` if present (else assert via a quick import in an existing settings test)

**Interfaces:**
- Produces: neither `Settings` carries `odoo_callback_url` / `odoo_callback_api_key` any more. `REVA_SECRET_KEY` is read by `reva.secrets_crypto` directly from env (not threaded through `Settings`), so `Settings` needs no new field — this task is purely a removal + docs.

- [ ] **Step 1: Remove the fields from worker settings**

In `worker/worker/settings.py`: delete the `odoo_callback_url: str = ""` and `odoo_callback_api_key: str = ""` dataclass fields, and delete the two corresponding lines in `from_env()` (`odoo_callback_url=...`, `odoo_callback_api_key=...`).

- [ ] **Step 2: Remove the fields from api settings**

In `api/app/settings.py`: delete the `odoo_callback_url: str = ""` and `odoo_callback_api_key: str = ""` dataclass fields, and the two lines in `from_env()`.

> The api never used the Odoo client (only the worker did), so these were dead config in `api/app/settings.py` already; removing them is safe.

- [ ] **Step 3: Grep for stragglers**

Run: `grep -rn "odoo_callback\|ODOO_CALLBACK" reva api worker scheduler --include=*.py`
Expected after edits: only references inside `OdooCallbackClient` internals (`reva/odoo_client.py` constructor params are named `callback_url`/`api_key` — unrelated) and tests you've updated. Remove any remaining `settings.odoo_callback_*` reads. Add `REVA_SECRET_KEY` to `.env.example` / README env tables and delete `ODOO_CALLBACK_URL` / `ODOO_CALLBACK_API_KEY` rows there.

- [ ] **Step 4: Run the settings + boot tests**

Run:
```
cd worker && .venv/bin/python -m pytest tests/ -k "settings or context or main" -v
cd api && .venv/bin/python -m pytest tests/test_startup.py -v
```
Expected: PASS (no references to the removed fields).

- [ ] **Step 5: Commit**

```bash
git add worker/worker/settings.py api/app/settings.py README.md .env.example
git commit -m "chore(config): add REVA_SECRET_KEY; remove ODOO_CALLBACK_* env path"
```

---

### Task 9: TUI — API layer (types, iface, client, mock)

**Files:**
- Modify: `tui/internal/api/types.go` (add Odoo types)
- Modify: `tui/internal/api/iface.go` (add 4 methods)
- Modify: `tui/internal/api/client.go` (implement the 4 methods)
- Modify: `tui/internal/api/mock.go` (mock the 4 methods)
- Test: `tui/internal/api/client_test.go` (extend) or rely on `go build`/`go vet`

**Interfaces:**
- Produces: `OdooInstancePage`, `OdooInstanceSummary`, `OdooInstanceCost`, `WindowCost`, `TaskCost`, `OdooInstanceCreated`; `ClientIface.{OdooInstances, CreateOdooInstance, RotateOdooInstanceKey, SetOdooInstanceActive}`.

- [ ] **Step 1: Add the types**

In `tui/internal/api/types.go`, append:

```go
type TaskCost struct {
	CostUSD      float64 `json:"cost_usd"`
	InputTokens  int     `json:"input_tokens"`
	OutputTokens int     `json:"output_tokens"`
	Count        int     `json:"count"`
}

type WindowCost struct {
	Analysis TaskCost `json:"analysis"`
	Issues   TaskCost `json:"issues"`
}

type OdooInstanceCost struct {
	Lifetime WindowCost `json:"lifetime"`
	Last24h  WindowCost `json:"last_24h"`
	Last30d  WindowCost `json:"last_30d"`
}

type OdooInstanceSummary struct {
	ID          int              `json:"id"`
	Name        string           `json:"name"`
	KeyPrefix   string           `json:"key_prefix"`
	CallbackURL string           `json:"callback_url"`
	Active      bool             `json:"active"`
	CreatedAt   time.Time        `json:"created_at"`
	Cost        OdooInstanceCost `json:"cost"`
}

type OdooInstancePage struct {
	Items []OdooInstanceSummary `json:"items"`
	Total int                   `json:"total"`
}

type OdooInstanceCreated struct {
	ID        int    `json:"id"`
	Name      string `json:"name"`
	KeyPrefix string `json:"key_prefix"`
	APIKey    string `json:"api_key"`
}
```

- [ ] **Step 2: Add the interface methods**

In `tui/internal/api/iface.go`, add to `ClientIface`:

```go
	OdooInstances() (*OdooInstancePage, error)
	CreateOdooInstance(name, callbackURL, callbackKey string) (*OdooInstanceCreated, error)
	RotateOdooInstanceKey(id int) (*OdooInstanceCreated, error)
	SetOdooInstanceActive(id int, active bool) error
```

- [ ] **Step 3: Implement on the real client**

In `tui/internal/api/client.go`, add a JSON-returning POST helper and the four methods. (The existing `post(path)` returns no body; we need the created body, and PATCH.)

```go
// postJSON sends `body` as JSON and decodes the response into `out` (any 2xx).
func (c *Client) postJSON(method, path string, body any, out any, wantStatus int) error {
	var reader io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return err
		}
		reader = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, c.base+path, reader)
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	c.authHeader(req)
	resp, err := c.http.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != wantStatus {
		var e struct {
			Detail string `json:"detail"`
		}
		if json.NewDecoder(resp.Body).Decode(&e) == nil && e.Detail != "" {
			return fmt.Errorf("%s", e.Detail)
		}
		return fmt.Errorf("HTTP %d from %s", resp.StatusCode, path)
	}
	if out == nil {
		return nil
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

func (c *Client) OdooInstances() (*OdooInstancePage, error) {
	var p OdooInstancePage
	return &p, c.get("/odoo-instances", &p)
}

func (c *Client) CreateOdooInstance(name, callbackURL, callbackKey string) (*OdooInstanceCreated, error) {
	body := map[string]string{
		"name": name, "callback_url": callbackURL, "callback_api_key": callbackKey,
	}
	var out OdooInstanceCreated
	return &out, c.postJSON(http.MethodPost, "/odoo-instances", body, &out, http.StatusCreated)
}

func (c *Client) RotateOdooInstanceKey(id int) (*OdooInstanceCreated, error) {
	var out OdooInstanceCreated
	return &out, c.postJSON(
		http.MethodPost, fmt.Sprintf("/odoo-instances/%d/rotate-key", id), nil, &out, http.StatusOK,
	)
}

func (c *Client) SetOdooInstanceActive(id int, active bool) error {
	return c.postJSON(
		http.MethodPatch, fmt.Sprintf("/odoo-instances/%d", id),
		map[string]bool{"active": active}, nil, http.StatusOK,
	)
}
```

Ensure `client.go`'s import block includes `"bytes"`, `"io"`, `"encoding/json"`, `"fmt"`, `"net/http"` (add `"io"` if missing).

- [ ] **Step 4: Implement on the mock**

In `tui/internal/api/mock.go`, add:

```go
func (m *MockClient) OdooInstances() (*OdooInstancePage, error) {
	now := time.Now()
	mk := func(c float64, in, out, n int) TaskCost {
		return TaskCost{CostUSD: c, InputTokens: in, OutputTokens: out, Count: n}
	}
	items := []OdooInstanceSummary{
		{
			ID: 1, Name: "ACME Production", KeyPrefix: "reva_odoo_a1b2",
			CallbackURL: "https://odoo.acme.example/write-field", Active: true,
			CreatedAt: now.Add(-30 * 24 * time.Hour),
			Cost: OdooInstanceCost{
				Lifetime: WindowCost{Analysis: mk(12.40, 900000, 120000, 320), Issues: mk(8.10, 400000, 90000, 55)},
				Last24h:  WindowCost{Analysis: mk(0.42, 30000, 4000, 11), Issues: mk(0.15, 8000, 1500, 2)},
				Last30d:  WindowCost{Analysis: mk(6.20, 450000, 60000, 160), Issues: mk(3.90, 200000, 45000, 28)},
			},
		},
		{
			ID: 2, Name: "Beta Staging", KeyPrefix: "reva_odoo_c3d4",
			CallbackURL: "", Active: false, CreatedAt: now.Add(-3 * 24 * time.Hour),
			Cost: OdooInstanceCost{},
		},
	}
	return &OdooInstancePage{Items: items, Total: len(items)}, nil
}

func (m *MockClient) CreateOdooInstance(name, callbackURL, callbackKey string) (*OdooInstanceCreated, error) {
	return &OdooInstanceCreated{ID: 99, Name: name, KeyPrefix: "reva_odoo_new9", APIKey: "reva_odoo_DEMOKEYdonotuse"}, nil
}

func (m *MockClient) RotateOdooInstanceKey(id int) (*OdooInstanceCreated, error) {
	return &OdooInstanceCreated{ID: id, Name: "ACME Production", KeyPrefix: "reva_odoo_rot8", APIKey: "reva_odoo_ROTATEDdemo"}, nil
}

func (m *MockClient) SetOdooInstanceActive(id int, active bool) error { return nil }
```

- [ ] **Step 5: Build, vet, test, commit**

Run: `cd tui && go build ./... && go vet ./... && go test ./...`
Expected: PASS (compiles; `MockClient` still satisfies `ClientIface`).

```bash
git add tui/internal/api/
git commit -m "feat(tui): API client for /api/v1/odoo-instances"
```

---

### Task 10: TUI — "Odoo" tab (read + create/rotate/toggle + cost)

**Files:**
- Create: `tui/internal/ui/odoo.go`
- Modify: `tui/internal/ui/app.go` (add `viewOdoo`, the field, init, key `0`, routing, `capturingText`)
- Modify: `tui/internal/ui/messages.go` (add message types)
- Test: `tui/internal/ui/odoo_test.go`

**Interfaces:**
- Consumes: `ClientIface` Odoo methods (Task 9).

- [ ] **Step 1: Write the failing test**

Create `tui/internal/ui/odoo_test.go`:

```go
package ui

import (
	"testing"

	tea "github.com/charmbracelet/bubbletea"

	"reva-tui/internal/api"
)
```

> Use the module path that `tui/go.mod` declares (check `go.mod`; the import above assumes `reva-tui` — match the real module name as used by `repos_test.go`).

```go
func TestOdooLoadsAndShowsCost(t *testing.T) {
	o := newOdoo(&api.MockClient{})
	o.width, o.height = 140, 30
	data, _ := (&api.MockClient{}).OdooInstances()
	o, _ = o.update(odooLoadedMsg{data: data})
	if len(o.items) != 2 {
		t.Fatalf("expected 2 instances, got %d", len(o.items))
	}
	out := o.view(140, 30)
	if out == "" {
		t.Fatal("empty view")
	}
}

func TestOdooCreateFlowPostsAndShowsKey(t *testing.T) {
	o := newOdoo(&api.MockClient{})
	o.width, o.height = 140, 30
	o, _ = o.update(keyMsg("n")) // enter create mode
	if !o.creating {
		t.Fatal("n did not enter create mode")
	}
	for _, ch := range "ACME" { // field 0: name
		o, _ = o.update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{ch}})
	}
	o, _ = o.update(keyMsg("enter")) // advance to callback_url
	o, _ = o.update(keyMsg("enter")) // advance to outbound key (leave url blank)
	o, cmd := o.update(keyMsg("enter")) // submit
	if cmd == nil {
		t.Fatal("submit produced no command")
	}
	o, _ = o.update(cmd().(odooCreatedMsg))
	if o.newKey == "" {
		t.Fatal("minted key not surfaced after create")
	}
}
```

(`keyMsg` helper already exists in `repos_test.go`/`tickets_test.go` in the same package.)

- [ ] **Step 2: Run to verify it fails**

Run: `cd tui && go test ./internal/ui/ -run TestOdoo -v`
Expected: FAIL to compile — `newOdoo`, `odooLoadedMsg`, `odooCreatedMsg`, `creating`, `newKey` undefined.

- [ ] **Step 3a: Add message types**

In `tui/internal/ui/messages.go`, add:

```go
type odooLoadedMsg struct {
	data *api.OdooInstancePage
	err  error
}

type odooCreatedMsg struct {
	created *api.OdooInstanceCreated
	err     error
}

type odooActionMsg struct {
	err error
}
```

- [ ] **Step 3b: Create the tab model**

Create `tui/internal/ui/odoo.go` (models `repos.go`'s structure — manual key buffer, cursor nav, status line; adds a 3-field create form and a "minted key" banner):

```go
package ui

import (
	"fmt"
	"strings"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"

	"reva-tui/internal/api"
)

type Odoo struct {
	client    api.ClientIface
	items     []api.OdooInstanceSummary
	total     int
	err       error
	loading   bool
	cursor    int
	offset    int
	width     int
	height    int
	statusMsg string

	creating    bool      // capturing the create form
	createStep  int       // 0=name, 1=callback_url, 2=outbound key
	createName  string
	createURL   string
	createKey   string

	newKey string // plaintext key to display once after create/rotate
}

func newOdoo(client api.ClientIface) Odoo {
	return Odoo{client: client, loading: true}
}

func (o Odoo) load() tea.Cmd {
	client := o.client
	return func() tea.Msg {
		data, err := client.OdooInstances()
		return odooLoadedMsg{data: data, err: err}
	}
}

func (o Odoo) update(msg tea.Msg) (Odoo, tea.Cmd) {
	switch m := msg.(type) {
	case tickMsg:
		return o, o.load()

	case odooLoadedMsg:
		o.loading = false
		o.err = m.err
		if m.data != nil {
			o.items = m.data.Items
			o.total = m.data.Total
		}
		if o.cursor >= len(o.items) {
			o.cursor, o.offset = 0, 0
		}

	case odooCreatedMsg:
		if m.err != nil {
			o.statusMsg = styleStatusFailed.Render("create failed: " + m.err.Error())
			return o, nil
		}
		o.newKey = m.created.APIKey
		o.statusMsg = styleStatusCompleted.Render("created " + m.created.Name)
		return o, o.load()

	case odooActionMsg:
		if m.err != nil {
			o.statusMsg = styleStatusFailed.Render("action failed: " + m.err.Error())
			return o, nil
		}
		return o, o.load()

	case tea.KeyMsg:
		if o.newKey != "" { // dismiss the key banner on any key
			o.newKey = ""
			return o, nil
		}
		if o.creating {
			return o.updateCreate(m)
		}
		visibleRows := o.height - 6
		if visibleRows < 1 {
			visibleRows = 1
		}
		if c, off, ok := listNav(m.String(), o.cursor, o.offset, len(o.items), visibleRows); ok {
			o.cursor, o.offset = c, off
			return o, nil
		}
		switch m.String() {
		case "n":
			o.creating, o.createStep = true, 0
			o.createName, o.createURL, o.createKey, o.statusMsg = "", "", "", ""
		case "r":
			if o.cursor < len(o.items) {
				id := o.items[o.cursor].ID
				client := o.client
				o.statusMsg = styleSubtitle.Render("rotating key...")
				return o, func() tea.Msg {
					created, err := client.RotateOdooInstanceKey(id)
					return odooCreatedMsg{created: created, err: err}
				}
			}
		case "t":
			if o.cursor < len(o.items) {
				it := o.items[o.cursor]
				client := o.client
				o.statusMsg = styleSubtitle.Render("toggling active...")
				return o, func() tea.Msg {
					return odooActionMsg{err: client.SetOdooInstanceActive(it.ID, !it.Active)}
				}
			}
		case "R":
			o.loading, o.statusMsg = true, ""
			return o, o.load()
		}
	}
	return o, nil
}

func (o Odoo) updateCreate(m tea.KeyMsg) (Odoo, tea.Cmd) {
	switch m.Type {
	case tea.KeyEsc:
		o.creating = false
		return o, nil
	case tea.KeyEnter:
		if o.createStep < 2 {
			o.createStep++
			return o, nil
		}
		name, url, key := o.createName, o.createURL, o.createKey
		o.creating = false
		if strings.TrimSpace(name) == "" {
			o.statusMsg = styleStatusFailed.Render("name is required")
			return o, nil
		}
		client := o.client
		o.statusMsg = styleSubtitle.Render("creating " + name + " ...")
		return o, func() tea.Msg {
			created, err := client.CreateOdooInstance(name, url, key)
			return odooCreatedMsg{created: created, err: err}
		}
	case tea.KeyBackspace:
		o.editField(func(s string) string {
			if len(s) > 0 {
				return s[:len(s)-1]
			}
			return s
		})
	case tea.KeyRunes, tea.KeySpace:
		o.editField(func(s string) string { return s + string(m.Runes) })
	}
	return o, nil
}

func (o *Odoo) editField(f func(string) string) {
	switch o.createStep {
	case 0:
		o.createName = f(o.createName)
	case 1:
		o.createURL = f(o.createURL)
	case 2:
		o.createKey = f(o.createKey)
	}
}

func (o Odoo) view(w, h int) string {
	header := styleTitle.Padding(0, 1).Render(fmt.Sprintf("Odoo Instances (%d)", o.total))

	if o.newKey != "" {
		banner := lipgloss.JoinVertical(lipgloss.Left,
			styleStatusCompleted.Render("  New API key — copy it now, it will not be shown again:"),
			"",
			"    "+styleTitle.Render(o.newKey),
			"",
			styleSubtitle.Render("  press any key to dismiss"))
		return lipgloss.JoinVertical(lipgloss.Left, header, "", banner)
	}
	if o.creating {
		return lipgloss.JoinVertical(lipgloss.Left, header, "", o.createForm())
	}
	if o.loading && len(o.items) == 0 {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			lipgloss.Place(w, h-3, lipgloss.Center, lipgloss.Center, styleSubtitle.Render("Loading...")))
	}
	if o.err != nil {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			styleStatusFailed.Render("  Error: "+o.err.Error()))
	}
	if len(o.items) == 0 {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			lipgloss.Place(w, h-3, lipgloss.Center, lipgloss.Center,
				styleSubtitle.Render("No Odoo instances — press n to add one")))
	}

	colName, colPrefix, colHost, colA, colI, colW := 24, 16, 26, 10, 10, 9
	hdr := lipgloss.NewStyle().Bold(true).Foreground(colorMuted).Render(
		fmt.Sprintf("   %-*s  %-*s  %-*s  %*s  %*s  %*s  %*s",
			colName, "Name", colPrefix, "Key", colHost, "Callback",
			colA, "Life A$", colI, "Life I$", colW, "24h$", colW, "30d$"))

	visibleRows := h - 6
	if visibleRows < 1 {
		visibleRows = 1
	}
	end := o.offset + visibleRows
	if end > len(o.items) {
		end = len(o.items)
	}
	rows := []string{hdr}
	for i := o.offset; i < end; i++ {
		it := o.items[i]
		host := it.CallbackURL
		if host == "" {
			host = "—"
		}
		life := it.Cost.Lifetime
		d24 := it.Cost.Last24h.Analysis.CostUSD + it.Cost.Last24h.Issues.CostUSD
		d30 := it.Cost.Last30d.Analysis.CostUSD + it.Cost.Last30d.Issues.CostUSD
		active := "+"
		if !it.Active {
			active = "x"
		}
		line := fmt.Sprintf("  %s  %-*s  %-*s  %-*s  %*.2f  %*.2f  %*.2f  %*.2f",
			active,
			colName, truncate(it.Name, colName),
			colPrefix, truncate(it.KeyPrefix, colPrefix),
			colHost, truncate(host, colHost),
			colA, life.Analysis.CostUSD, colI, life.Issues.CostUSD,
			colW, d24, colW, d30)
		if i == o.cursor {
			line = styleSelected.Width(w - 2).Render(line)
		}
		rows = append(rows, line)
	}

	pos := styleSubtitle.Render(fmt.Sprintf("  %d/%d   n add · r rotate · t toggle · R refresh", o.cursor+1, len(o.items)))
	if o.statusMsg != "" {
		pos = "  " + o.statusMsg
	}
	return lipgloss.JoinVertical(lipgloss.Left, header, "", strings.Join(rows, "\n"), "", pos)
}

func (o Odoo) createForm() string {
	field := func(idx int, label, val string) string {
		cursor := ""
		if o.createStep == idx {
			cursor = "█"
		}
		return fmt.Sprintf("  %-14s %s%s", label+":", val, cursor)
	}
	return lipgloss.JoinVertical(lipgloss.Left,
		styleTitle.Render("  Add Odoo instance"),
		"",
		field(0, "Name", o.createName),
		field(1, "Callback URL", o.createURL),
		field(2, "Outbound key", o.createKey),
		"",
		styleSubtitle.Render("  [enter] next/submit   [esc] cancel"))
}
```

> If `tui/go.mod`'s module path is not `reva-tui`, replace the import path in both `odoo.go` and `odoo_test.go` to match (read `tui/go.mod`). `listNav`, `truncate`, `styleTitle`, `styleSubtitle`, `styleSelected`, `styleStatusFailed`, `styleStatusCompleted`, `colorMuted`, `tickMsg` are all existing helpers/styles in the `ui` package (used by `repos.go`).

- [ ] **Step 3c: Wire the tab into `app.go`**

In `tui/internal/ui/app.go`:
- Add `viewOdoo` to the `const (... view = iota)` block (after `viewFeedback`).
- Add `odoo Odoo` to the `App` struct.
- In `NewApp`, add `odoo: newOdoo(client),`.
- In `Init`, add `a.odoo.load(),` to the `tea.Batch(...)`.
- Add a key case `case "0": a.clearStatusMsgs(); a.active = viewOdoo; return a, nil` alongside the `"1"`..`"9"` cases.
- In the `capturingText()` switch, add `case viewOdoo: return a.odoo.creating`.
- In the text-capture routing block (the `if a.capturingText()` switch) add `case viewOdoo: a.odoo, cmd = a.odoo.update(msg)`.
- In the per-tab routing (the `if a.active == view... { a.X, cmd = a.X.update(msg) }` chain) add the `viewOdoo` branch routing to `a.odoo.update(msg)`.
- In the `View()` method's tab dispatch (where each `case viewX: return a.x.view(...)` is), add `case viewOdoo: return a.odoo.view(w, h)`. Also add "Odoo" to the tab-bar labels if the bar is rendered from a slice/format — match how tabs 1–9 are labeled.

> Read `app.go` fully before editing; mirror exactly how an existing tab (e.g. `viewTickets`) is wired in each of these locations.

- [ ] **Step 4: Run to verify it passes**

Run: `cd tui && go build ./... && go vet ./... && go test ./...`
Expected: PASS (incl. `TestOdooLoadsAndShowsCost`, `TestOdooCreateFlowPostsAndShowsKey`).

Manual smoke: `cd tui && go run . --demo` → press `0` → see two instances + cost columns; press `n` → fill the form → see the demo minted key banner.

- [ ] **Step 5: Commit**

```bash
git add tui/internal/ui/odoo.go tui/internal/ui/odoo_test.go tui/internal/ui/app.go tui/internal/ui/messages.go
git commit -m "feat(tui): Odoo instances tab — manage instances + per-instance cost"
```

---

## Final verification (run before declaring done)

```bash
# Python — all three services + lint (shared reva/ changed)
make test
ruff check reva worker/worker api/app scheduler/scheduler
# Go TUI
cd tui && go build ./... && go vet ./... && go test ./...
# Postgres-only migration + index (raw 018 SQL)
make test-integration   # or first staging boot
```

All must be green. State honestly which paths are unit-only (no live Claude CLI, no real Odoo HTTP, the raw `018` SQL only on Postgres).

## Self-Review (completed by plan author)

- **Spec coverage:** §1 data model → Task 1; §2 encryption → Task 2; §3 auth+scoping → Tasks 5–7; §4 worker callbacks → Task 7; §5 API endpoints → Task 4 (+ create-route auth in 5–7); §6 cost → Task 3 (queries) + Task 4 (endpoint) + Task 10 (TUI); §7 TUI → Tasks 9–10; §8 migration/settings → Task 1 + Task 8. All covered.
- **Type consistency:** `odoo_instance_id` is `int` on both job-params and used as the FK everywhere; `build_odoo_client(ctx, odoo_instance_id)`, `get_odoo_instance(...)→dict`, `resolve_odoo_instance_by_key(...)→tuple[int,str]`, `require_odoo_instance(...)→ResolvedOdooInstance` are referenced consistently across tasks. TUI `OdooInstanceCreated.APIKey` ↔ API `api_key` JSON tag matches.
- **Placeholders:** none — every code step shows full code. The two "match the existing signature" notes (`record_admin_action`, `get_pending_ticket_analysis` return shape) point at concrete existing call sites to copy, not unspecified work.
