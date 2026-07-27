"""Tests for the persona endpoints.

The privilege boundary is the point of most of these: a persona decides what
REVA says to a customer, so an Odoo instance key must never reach them.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from app.dependencies import get_db, get_settings
from app.main import app
from app.settings import Settings
from reva.db import Base, Database, create_engine_from_url, writers

MASTER = {"Authorization": "Bearer master-key"}


@pytest.fixture()
def client_db(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("REVA_SECRET_KEY", Fernet.generate_key().decode())
    engine = create_engine_from_url(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = Database(engine)
    settings = Settings(
        database_url="sqlite:///:memory:", github_app_id=1,
        github_webhook_secret="x", github_private_key="x",
        redis_url="redis://localhost:6379/0", api_key="master-key",
    )
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: settings
    tc = TestClient(app)
    yield tc, db
    app.dependency_overrides.clear()


# --- privilege boundary -------------------------------------------------------


def test_instance_key_cannot_reach_personas(client_db):
    """A persona decides what REVA says to that customer. An instance being
    able to rewrite its own tone — or read another customer's — is a privilege
    boundary, not a convenience."""
    client, _ = client_db
    key = client.post("/api/v1/odoo-instances", json={
        "name": "inst", "callback_url": "", "callback_api_key": "",
    }, headers=MASTER).json()["api_key"]
    instance = {"Authorization": f"Bearer {key}"}

    assert client.get("/api/v1/personas", headers=instance).status_code == 401
    assert client.post("/api/v1/personas", json={"scope": "default"},
                       headers=instance).status_code == 401
    assert client.get("/api/v1/personas/resolved", headers=instance).status_code == 401


def test_unauthenticated_is_rejected(client_db):
    client, _ = client_db
    assert client.get("/api/v1/personas").status_code == 401


# --- CRUD ---------------------------------------------------------------------


def test_create_and_list_default_first(client_db):
    client, _ = client_db
    client.post("/api/v1/personas", json={
        "scope": "repo", "repo_full_name": "acme/widgets", "formality": "informal",
    }, headers=MASTER)
    r = client.post("/api/v1/personas", json={
        "scope": "default", "formality": "formal", "language": "auto",
    }, headers=MASTER)
    assert r.status_code == 201

    items = client.get("/api/v1/personas", headers=MASTER).json()["items"]
    assert items[0]["scope"] == "default"
    assert {i["scope"] for i in items} == {"default", "repo"}


def test_repo_scope_requires_a_repo_name(client_db):
    client, _ = client_db
    r = client.post("/api/v1/personas", json={"scope": "repo"}, headers=MASTER)
    assert r.status_code == 422


def test_default_scope_rejects_a_repo_name(client_db):
    client, _ = client_db
    r = client.post("/api/v1/personas", json={
        "scope": "default", "repo_full_name": "acme/widgets",
    }, headers=MASTER)
    assert r.status_code == 422


def test_second_persona_for_one_repo_replaces_rather_than_duplicating(client_db):
    client, db = client_db
    for formality in ("formal", "informal"):
        client.post("/api/v1/personas", json={
            "scope": "repo", "repo_full_name": "acme/widgets", "formality": formality,
        }, headers=MASTER)
    items = client.get("/api/v1/personas", headers=MASTER).json()["items"]
    assert len(items) == 1
    assert items[0]["formality"] == "informal"


def test_patch_updates_knobs(client_db):
    client, _ = client_db
    created = client.post("/api/v1/personas", json={
        "scope": "default", "formality": "formal",
    }, headers=MASTER).json()
    r = client.patch(f"/api/v1/personas/{created['id']}", json={
        "scope": "default", "formality": "informal", "content_policy": "no prices",
    }, headers=MASTER)
    assert r.status_code == 200
    assert r.json()["formality"] == "informal"
    assert r.json()["content_policy"] == "no prices"


def test_patch_unknown_id_is_404(client_db):
    client, _ = client_db
    r = client.patch("/api/v1/personas/999", json={"scope": "default"}, headers=MASTER)
    assert r.status_code == 404


# --- validation ---------------------------------------------------------------


@pytest.mark.parametrize("field,value", [
    ("formality", "casual"),
    ("technical_depth", "extreme"),
    ("length", "epic"),
])
def test_unknown_enum_values_are_422(client_db, field, value):
    client, _ = client_db
    r = client.post("/api/v1/personas", json={"scope": "default", field: value},
                    headers=MASTER)
    assert r.status_code == 422


def test_language_outside_the_answer_schema_is_rejected(client_db):
    """SupportAnswerResult.language is Literal["de","en"] under a strict tool
    schema. A persona pinning "fr" would produce a prompt the model honours and
    a field it cannot report truthfully — so the write path forbids it."""
    client, _ = client_db
    assert client.post("/api/v1/personas", json={
        "scope": "default", "language": "fr",
    }, headers=MASTER).status_code == 422
    for ok in ("auto", "de", "en"):
        assert client.post("/api/v1/personas", json={
            "scope": "default", "language": ok,
        }, headers=MASTER).status_code == 201


# --- resolution view ----------------------------------------------------------


def test_resolved_shows_per_field_inheritance(client_db):
    """The resolved view is what actually gets used; a repo row with a NULL
    knob inherits the default's rather than blanking it."""
    client, _ = client_db
    client.post("/api/v1/personas", json={
        "scope": "default", "formality": "formal", "language": "de",
        "content_policy": "never quote prices",
    }, headers=MASTER)
    client.post("/api/v1/personas", json={
        "scope": "repo", "repo_full_name": "acme/widgets", "formality": "informal",
    }, headers=MASTER)

    r = client.get("/api/v1/personas/resolved?repo_full_name=acme/widgets",
                   headers=MASTER).json()
    assert r["formality"] == "informal"          # repo wins
    assert r["language"] == "de"                 # inherited from default
    assert r["content_policy"] == "never quote prices"
    assert "Persona" in r["rendered_block"]


def test_resolved_without_any_rows_returns_the_hardcoded_fallback(client_db):
    """REVA must never draft with an empty persona block."""
    client, _ = client_db
    r = client.get("/api/v1/personas/resolved", headers=MASTER).json()
    assert r["formality"] == "formal"
    assert r["rendered_block"].strip()


def test_deactivating_a_repo_persona_falls_back_to_default(client_db):
    """`active: false` means "as if absent" in the resolver — so the API's
    deactivate is a real operation, not a silent no-op."""
    client, db = client_db
    client.post("/api/v1/personas", json={
        "scope": "default", "formality": "formal",
    }, headers=MASTER)
    created = client.post("/api/v1/personas", json={
        "scope": "repo", "repo_full_name": "acme/widgets", "formality": "informal",
    }, headers=MASTER).json()

    before = client.get("/api/v1/personas/resolved?repo_full_name=acme/widgets",
                        headers=MASTER).json()
    assert before["formality"] == "informal"

    client.patch(f"/api/v1/personas/{created['id']}", json={
        "scope": "repo", "repo_full_name": "acme/widgets",
        "formality": "informal", "active": False,
    }, headers=MASTER)

    after = client.get("/api/v1/personas/resolved?repo_full_name=acme/widgets",
                       headers=MASTER).json()
    assert after["formality"] == "formal"       # fell back to the default row
