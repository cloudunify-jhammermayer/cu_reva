"""Tests for the /repo-docs consultant docs surface."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from app.dependencies import get_db, get_github_client, get_settings
from app.main import app
from app.settings import Settings
from reva.db import Base, Database, create_engine_from_url, writers
from reva.db.models import Repository
from reva.errors import TransientError


class _FakeGitHub:
    def __init__(self, *, tree=None, files=None, raw=None, tree_error=None, branches=None):
        self.tree = tree if tree is not None else {"tree": [], "truncated": False}
        self.files = files or {}
        self.raw = raw or {}
        self.tree_error = tree_error
        self.branches = branches if branches is not None else [{"name": "main", "sha": "s1"}]

    def get_installation_token(self, installation_id):
        return "ghs_tok"

    def get_branches(self, token, owner, repo):
        return self.branches

    def get_tree(self, token, owner, repo, ref, recursive=True):
        if self.tree_error:
            raise self.tree_error
        return self.tree

    def get_file_content(self, token, owner, repo, path, ref):
        return self.files.get(path)

    def get_raw_file(self, token, owner, repo, path, ref):
        return self.raw.get(path)


@pytest.fixture()
def env():
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
        redis_url="redis://localhost:6379/0",
    )
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: settings
    yield TestClient(app), db, settings
    app.dependency_overrides.clear()


def _seed_repo(db, owner="acme", name="widgets", branch="main", enabled=True):
    repo_id = writers.upsert_repository(
        db, github_repository_id=hash((owner, name)) & 0xFFFF, owner=owner, name=name,
        default_branch=branch, installation_id=99,
    )
    if not enabled:
        with db.session() as s:
            s.get(Repository, repo_id).enabled = False
    return repo_id


def _use_github(fake):
    app.dependency_overrides[get_github_client] = lambda: fake


# --- GET /repo-docs/repos -----------------------------------------------------

def test_list_repos_returns_enabled_only(env):
    client, db, _ = env
    rid = _seed_repo(db, name="widgets", branch="develop")
    _seed_repo(db, name="archived", enabled=False)
    body = client.get("/repo-docs/repos").json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["id"] == rid
    assert item["full_name"] == "acme/widgets"
    assert item["default_branch"] == "develop"


# --- GET /repo-docs/repos/{id}/branches ---------------------------------------

def test_branches_default_first_then_alpha(env):
    client, db, _ = env
    rid = _seed_repo(db, branch="develop")
    _use_github(_FakeGitHub(branches=[
        {"name": "main", "sha": "s1"},
        {"name": "develop", "sha": "s2"},
        {"name": "feature/x", "sha": "s3"},
    ]))
    body = client.get(f"/repo-docs/repos/{rid}/branches").json()
    assert body["default_branch"] == "develop"
    assert body["items"][0] == {"name": "develop", "sha": "s2", "is_default": True}
    assert [b["name"] for b in body["items"]] == ["develop", "feature/x", "main"]


def test_branches_unknown_repo_404(env):
    client, _, _ = env
    _use_github(_FakeGitHub())
    assert client.get("/repo-docs/repos/9999/branches").status_code == 404


# --- GET /repo-docs/repos/{id}/tree -------------------------------------------

def test_tree_returns_only_markdown_under_custom_addons(env):
    client, db, _ = env
    rid = _seed_repo(db)
    _use_github(_FakeGitHub(tree={
        "tree": [
            {"path": "custom_addons/cu_x/docs/consultant.md", "type": "blob", "size": 10},
            {"path": "custom_addons/cu_x/README.md", "type": "blob", "size": 5},
            {"path": "custom_addons/cu_x/app.py", "type": "blob", "size": 99},  # not markdown
            {"path": "custom_addons/cu_x/docs", "type": "tree"},                # directory
            {"path": "docs/architecture.md", "type": "blob", "size": 7},        # out of scope
            {"path": "README.md", "type": "blob", "size": 3},                   # out of scope
        ],
        "truncated": False,
    }))
    body = client.get(f"/repo-docs/repos/{rid}/tree").json()
    assert [e["path"] for e in body["entries"]] == [
        "custom_addons/cu_x/README.md",
        "custom_addons/cu_x/docs/consultant.md",
    ]
    assert body["ref"] == "main"
    assert body["truncated"] is False


def test_tree_passes_through_truncated_and_custom_ref(env):
    client, db, _ = env
    rid = _seed_repo(db)
    _use_github(_FakeGitHub(tree={"tree": [], "truncated": True}))
    body = client.get(f"/repo-docs/repos/{rid}/tree?ref=v2").json()
    assert body["truncated"] is True
    assert body["ref"] == "v2"


def test_tree_unknown_repo_404(env):
    client, _, _ = env
    _use_github(_FakeGitHub())
    assert client.get("/repo-docs/repos/9999/tree").status_code == 404


def test_tree_upstream_transient_is_502(env):
    client, db, _ = env
    rid = _seed_repo(db)
    _use_github(_FakeGitHub(tree_error=TransientError("boom")))
    assert client.get(f"/repo-docs/repos/{rid}/tree").status_code == 502


# --- GET /repo-docs/repos/{id}/file -------------------------------------------

def test_file_returns_markdown(env):
    client, db, _ = env
    rid = _seed_repo(db)
    _use_github(_FakeGitHub(files={"docs/intro.md": "# Hello"}))
    body = client.get(f"/repo-docs/repos/{rid}/file?path=docs/intro.md").json()
    assert body["content"] == "# Hello"
    assert body["path"] == "docs/intro.md"


def test_file_missing_is_404(env):
    client, db, _ = env
    rid = _seed_repo(db)
    _use_github(_FakeGitHub(files={}))
    assert client.get(f"/repo-docs/repos/{rid}/file?path=docs/nope.md").status_code == 404


def test_file_non_markdown_is_415(env):
    client, db, _ = env
    rid = _seed_repo(db)
    _use_github(_FakeGitHub())
    assert client.get(f"/repo-docs/repos/{rid}/file?path=src/app.py").status_code == 415


def test_file_path_traversal_is_422(env):
    client, db, _ = env
    rid = _seed_repo(db)
    _use_github(_FakeGitHub())
    assert client.get(f"/repo-docs/repos/{rid}/file?path=../../etc/passwd.md").status_code == 422


# --- GET /repo-docs/repos/{id}/raw --------------------------------------------

def test_raw_returns_image_bytes_with_guessed_type(env):
    client, db, _ = env
    rid = _seed_repo(db)
    png = b"\x89PNG\r\n\x1a\n"
    _use_github(_FakeGitHub(raw={"docs/img/logo.png": png}))
    resp = client.get(f"/repo-docs/repos/{rid}/raw?path=docs/img/logo.png")
    assert resp.status_code == 200
    assert resp.content == png
    assert resp.headers["content-type"] == "image/png"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'none'" in resp.headers["content-security-policy"]


def test_raw_rejects_non_asset_extension(env):
    client, db, _ = env
    rid = _seed_repo(db)
    _use_github(_FakeGitHub())
    assert client.get(f"/repo-docs/repos/{rid}/raw?path=docs/intro.md").status_code == 415


def test_raw_missing_is_404(env):
    client, db, _ = env
    rid = _seed_repo(db)
    _use_github(_FakeGitHub(raw={}))
    assert client.get(f"/repo-docs/repos/{rid}/raw?path=docs/img/gone.png").status_code == 404
