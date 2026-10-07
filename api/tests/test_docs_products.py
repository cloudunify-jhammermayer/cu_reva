"""Tests for the docs site's product page endpoints (/repo-docs/products)."""
# ruff: noqa: F811

from __future__ import annotations

from tests.test_docs import _FakeGitHub, _seed_repo, _use_github, env  # noqa: F401  (env is a fixture; F811 on its use is expected)

from reva.db.models import OpsEvent
from reva.errors import TransientError

MANIFEST = "{'name': 'Helpdesk SLA', 'summary': 'SLA timers', 'version': '%s.1.0.0'}"


class _ProductGitHub(_FakeGitHub):
    """Per-ref trees and files, plus issues/PRs. `files` keys are `(ref, path)`
    with a plain `path` fallback so a file can be the same on every branch."""

    def __init__(self, *, trees=None, files=None, branches=None, issues=None, prs=None,
                 tree_errors=None, file_errors=()):
        super().__init__(branches=branches)
        self.trees = trees or {}
        self.files = files or {}
        self.issues = issues or []
        self.prs = prs or []
        self.tree_errors = tree_errors or {}
        self.file_errors = set(file_errors)
        self.only_repo = None   # when set, other repos have no files at all

    def get_tree(self, token, owner, repo, ref, recursive=True):
        if ref in self.tree_errors:
            raise self.tree_errors[ref]
        return self.trees.get(ref, {"tree": [], "truncated": False})

    def get_file_content(self, token, owner, repo, path, ref):
        if self.only_repo and repo != self.only_repo:
            return None
        if path in self.file_errors:
            raise TransientError("boom")
        if (ref, path) in self.files:
            return self.files[(ref, path)]
        return self.files.get(path)

    def list_issues(self, token, owner, repo, *, state, since=None):
        return [i for i in self.issues if i["state"] == state]

    def list_open_pull_requests(self, token, owner, repo):
        return self.prs


def _blobs(*paths):
    return {"tree": [{"path": p, "type": "blob", "size": 1} for p in paths], "truncated": False}


def _product_repo(db, name="cu-helpdesk", branches=("19.0", "18.0")):
    rid = _seed_repo(db, owner="Cloudunify", name=name, branch="main")
    gh = _ProductGitHub(
        branches=[{"name": "main", "sha": "m"}] + [{"name": b, "sha": b} for b in branches],
        trees={b: _blobs("cu_helpdesk_sla/__manifest__.py", "cu_helpdesk_sla/README.md",
                         "cu_helpdesk_sla/docs/guide.html", "product.yml", "README.md")
               for b in branches},
        files={".claude-review.yml": "product: true\n",
               **{(b, "cu_helpdesk_sla/__manifest__.py"): MANIFEST % b for b in branches},
               "product.yml": "modules:\n  cu_helpdesk_sla:\n    owner: J\n    price: 1200\n"},
    )
    return rid, gh


# --- GET /repo-docs/products ---------------------------------------------------

def test_products_list_only_product_repos(env):
    client, db, _ = env
    rid, gh = _product_repo(db)
    _seed_repo(db, owner="Cloudunify", name="customer-x")   # no product flag
    gh.only_repo = "cu-helpdesk"
    _use_github(gh)
    body = client.get("/repo-docs/products").json()
    assert [r["repository_id"] for r in body["items"]] == [rid]
    assert body["items"][0]["full_name"] == "Cloudunify/cu-helpdesk"
    assert body["items"][0]["html_url"] == "https://github.com/Cloudunify/cu-helpdesk"


def test_products_list_falls_back_to_version_branch_config(env):
    client, db, _ = env
    rid, gh = _product_repo(db)
    del gh.files[".claude-review.yml"]
    gh.files[("19.0", ".claude-review.yml")] = "product: true\n"
    _use_github(gh)
    assert [r["repository_id"] for r in client.get("/repo-docs/products").json()["items"]] == [rid]


def test_products_list_skips_repo_on_github_failure_with_ops_event(env):
    client, db, _ = env
    rid, gh = _product_repo(db)
    gh.file_errors.add(".claude-review.yml")
    _use_github(gh)
    assert client.get("/repo-docs/products").json()["items"] == []
    with db.session() as s:
        events = [(e.event, e.detail["step"]) for e in s.query(OpsEvent).all()]
    assert events == [("product_catalog_degraded", "config")]


def test_products_list_invalid_config_records_ops_event(env):
    client, db, _ = env
    rid, gh = _product_repo(db)
    gh.files[".claude-review.yml"] = "block_on_severity: nonsense\n"
    gh.branches = [{"name": "main", "sha": "m"}]
    _use_github(gh)
    assert client.get("/repo-docs/products").json()["items"] == []
    with db.session() as s:
        events = [(e.event, e.detail["step"]) for e in s.query(OpsEvent).all()]
    assert events == [("product_catalog_degraded", "config_invalid")]


# --- product repos in the docs tree -------------------------------------------

def test_tree_lists_root_addon_docs_for_product_repo(env):
    client, db, _ = env
    rid, gh = _product_repo(db)
    _use_github(gh)
    body = client.get(f"/repo-docs/repos/{rid}/tree?ref=19.0").json()
    assert [e["path"] for e in body["entries"]] == [
        "cu_helpdesk_sla/README.md", "cu_helpdesk_sla/docs/guide.html",
    ]


def test_tree_keeps_old_scope_for_non_product_repo(env):
    client, db, _ = env
    rid, gh = _product_repo(db)
    gh.files[".claude-review.yml"] = "max_diff_lines: 5\n"
    gh.branches = [{"name": "main", "sha": "m"}]   # no version-branch fallback either
    _use_github(gh)
    assert client.get(f"/repo-docs/repos/{rid}/tree?ref=19.0").json()["entries"] == []


def test_file_serves_addon_html_doc_for_product_repo(env):
    client, db, _ = env
    rid, gh = _product_repo(db)
    gh.files["cu_helpdesk_sla/docs/guide.html"] = "<h1>Guide</h1>"
    _use_github(gh)
    r = client.get(f"/repo-docs/repos/{rid}/file?path=cu_helpdesk_sla/docs/guide.html&ref=19.0")
    assert r.status_code == 200 and r.json()["content"] == "<h1>Guide</h1>"


def test_file_addon_html_502_when_github_fails_on_flag(env):
    client, db, _ = env
    rid, gh = _product_repo(db)
    gh.file_errors.add(".claude-review.yml")
    _use_github(gh)
    r = client.get(f"/repo-docs/repos/{rid}/file?path=cu_helpdesk_sla/docs/guide.html&ref=19.0")
    assert r.status_code == 502


def test_file_non_doc_extension_415_before_repo_lookup(env):
    client, _, _ = env
    _use_github(_FakeGitHub())
    assert client.get("/repo-docs/repos/9999/file?path=x.py").status_code == 415


# --- GET /repo-docs/products/{id} ---------------------------------------------

def test_product_detail_modules_versions_and_issues(env):
    client, db, _ = env
    rid, gh = _product_repo(db, branches=("19.0", "18.0", "17.0", "16.0"))
    gh.files[("18.0", "product.yml")] = (
        "modules:\n  cu_helpdesk_sla:\n    owner: Markus\n    price: 1200\n"
        "  cu_helpdesk_kb:\n    status: planned\n    eta: Q1\n"
    )
    gh.issues = [
        {"number": 1, "title": "[feature] 6811 - holidays", "html_url": "u1", "state": "open",
         "created_at": "2026-10-01T00:00:00Z", "assignees": [{"login": "joseph"}],
         "milestone": {"title": "19.0.2"}, "closed_at": None, "state_reason": None},
        {"number": 2, "title": "[fix] 6754 - pivot", "html_url": "u2", "state": "open",
         "created_at": "2026-10-02T00:00:00Z", "assignees": [], "milestone": None,
         "closed_at": None, "state_reason": None},
        {"number": 3, "title": "done thing", "html_url": "u3", "state": "closed",
         "created_at": "2026-09-01T00:00:00Z", "assignees": [], "milestone": None,
         "closed_at": "2026-10-01T00:00:00Z", "state_reason": "completed"},
    ]
    gh.prs = [{"number": 9, "title": "Fix pivot, closes #2", "body": ""}]
    _use_github(gh)
    body = client.get(f"/repo-docs/products/{rid}").json()
    assert body["branches"] == ["19.0", "18.0", "17.0"]
    assert body["ignored_branches"] == ["16.0"]
    assert body["html_url"] == "https://github.com/Cloudunify/cu-helpdesk"
    assert body["loaded_at"]
    mods = {m["module"]: m for m in body["modules"]}
    sla = mods["cu_helpdesk_sla"]
    assert (sla["name"], sla["owner"], sla["price"], sla["tldr"]) == (
        "Helpdesk SLA", "J", 1200, "SLA timers",
    )
    assert sla["has_yml_entry"] is True
    assert sla["versions"]["19.0"]["version"] == "19.0.1.0.0"
    assert sla["versions"]["19.0"]["readme_path"] == "cu_helpdesk_sla/README.md"
    assert sla["versions"]["17.0"]["status"] == "available"
    kb = mods["cu_helpdesk_kb"]
    assert list(kb["versions"]) == ["18.0"]
    assert kb["versions"]["18.0"] == {"status": "planned", "version": None, "eta": "Q1",
                                      "note": None, "manifest_error": None, "readme_path": None}
    assert [(i["number"], i["state"]) for i in body["issues"]] == [
        (2, "in_progress"), (1, "in_progress"),
    ]
    assert body["issues"][0]["pr_number"] == 9
    assert body["issues"][1]["assignee"] == "joseph"
    assert body["issues"][1]["milestone"] == "19.0.2"
    assert body["warnings"] == ["`cu_helpdesk_sla`: owner differs between 19.0 and 18.0"]


def test_product_detail_404_for_non_product_repo(env):
    client, db, _ = env
    rid, gh = _product_repo(db)
    gh.files[".claude-review.yml"] = "odoo: true\n"
    gh.branches = [{"name": "main", "sha": "m"}]
    _use_github(gh)
    assert client.get(f"/repo-docs/products/{rid}").status_code == 404
    assert client.get("/repo-docs/products/9999").status_code == 404


def test_product_detail_invalid_config_is_404_with_ops_event(env):
    client, db, _ = env
    rid, gh = _product_repo(db)
    gh.files[".claude-review.yml"] = "block_on_severity: nonsense\n"
    gh.branches = [{"name": "main", "sha": "m"}]
    _use_github(gh)
    assert client.get(f"/repo-docs/products/{rid}").status_code == 404
    with db.session() as s:
        events = [(e.event, e.detail["step"]) for e in s.query(OpsEvent).all()]
    assert events == [("product_catalog_degraded", "config_invalid")]


def test_product_detail_degrades_per_step_with_ops_events(env):
    client, db, _ = env
    rid, gh = _product_repo(db, branches=("19.0", "18.0"))
    gh.tree_errors["18.0"] = TransientError("tree down")
    gh.file_errors.add("cu_helpdesk_sla/__manifest__.py")

    class _NoIssues(_ProductGitHub):
        def list_issues(self, *a, **kw):
            raise TransientError("issues down")

    gh.__class__ = _NoIssues
    _use_github(gh)
    body = client.get(f"/repo-docs/products/{rid}").json()
    assert body["branches"] == ["19.0", "18.0"]
    sla = body["modules"][0]
    assert sla["versions"]["19.0"]["manifest_error"]       # fetch failed -> unparsed
    assert "18.0" not in sla["versions"]                    # whole branch degraded
    assert body["issues"] == []
    assert len(body["warnings"]) == 3
    with db.session() as s:
        steps = sorted(e.detail["step"] for e in s.query(OpsEvent).all()
                       if e.event == "product_catalog_degraded")
    assert steps == ["issues", "manifests:19.0", "tree:18.0"]


def test_product_detail_warns_on_truncated_tree(env):
    client, db, _ = env
    rid, gh = _product_repo(db, branches=("19.0",))
    gh.trees["19.0"]["truncated"] = True
    _use_github(gh)
    body = client.get(f"/repo-docs/products/{rid}").json()
    assert any("truncated" in w for w in body["warnings"])


def test_product_detail_is_cached(env):
    client, db, _ = env
    rid, gh = _product_repo(db, branches=("19.0",))
    _use_github(gh)
    first = client.get(f"/repo-docs/products/{rid}").json()
    gh.issues = [{"number": 1, "title": "new", "html_url": "u", "state": "open",
                  "created_at": "2026-10-01T00:00:00Z", "assignees": [], "milestone": None,
                  "closed_at": None, "state_reason": None}]
    second = client.get(f"/repo-docs/products/{rid}").json()
    assert second == first


def test_product_detail_passes_subscription_through(env):
    client, db, _ = env
    rid, gh = _product_repo(db, branches=("19.0",))
    gh.files["product.yml"] = (
        "modules:\n  cu_helpdesk_sla:\n    price: 1200\n    subscription: 90\n    per: month\n"
    )
    _use_github(gh)
    sla = client.get(f"/repo-docs/products/{rid}").json()["modules"][0]
    assert (sla["price"], sla["subscription"], sla["per"]) == (1200, 90, "month")
