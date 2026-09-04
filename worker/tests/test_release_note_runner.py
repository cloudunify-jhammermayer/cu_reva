"""Tests for release_note_runner.run_release_note (spec 2026-09-04, R2)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import pytest
from sqlalchemy import select

from reva import config
from reva.db import Base, Database, create_engine_from_url, writers
from reva.db.models import OdooInstance, OpsEvent, Repository
from reva.errors import PermanentError, TransientError
from reva.release_log import release_slug
from worker.release_note_runner import run_release_note
from worker.runner import WorkerContext, set_context

PAGE = '<div class="rl-page"><header class="rl-masthead"><h1>Lollipop</h1></header></div>\n'


@dataclass
class FakeGitHub:
    # (owner, repo, path, ref) -> content; a missing key is a 404 (None)
    files: dict[tuple[str, str, str, str], str] = field(default_factory=dict)
    raise_exc: Exception | None = None
    # repo names whose every read fails permanently (a 403, a deleted repo)
    fail_repos: set[str] = field(default_factory=set)
    # repo names whose release-log page read fails permanently (config reads still work)
    fail_pages: set[str] = field(default_factory=set)
    reads: list[tuple[str, str, str]] = field(default_factory=list)

    def get_installation_token(self, installation_id: int) -> str:
        return f"tok-{installation_id}"

    def get_file_content(self, token, owner, repo, path, ref):
        if self.raise_exc is not None:
            raise self.raise_exc
        if repo in self.fail_repos:
            raise PermanentError(f"GitHub 403 for {owner}/{repo}")
        if repo in self.fail_pages and path.startswith("docs/releases/"):
            raise PermanentError(f"GitHub 403 for {owner}/{repo} {path}")
        self.reads.append((owner, repo, path))
        return self.files.get((owner, repo, path, ref))


@dataclass
class FakeOdoo:
    raise_exc: Exception | None = None
    on_call: Callable[[], None] | None = None
    calls: list[dict] = field(default_factory=list)

    def release_note(self, **kwargs):
        self.calls.append(kwargs)
        if self.on_call is not None:
            self.on_call()
        if self.raise_exc is not None:
            raise self.raise_exc


@pytest.fixture()
def env(monkeypatch):
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Database(engine)
    with db.session() as s:
        s.add(OdooInstance(
            id=1, name="wenatex", key_hash="h1", key_prefix="p1",
            callback_url="https://odoo.example.com/api/reva", callback_api_key_enc="",
        ))
    github = FakeGitHub()
    odoo = FakeOdoo()
    ctx = WorkerContext(
        db=db,
        claude=None,  # type: ignore[arg-type]
        runner=None,  # type: ignore[arg-type]
        github=github,  # type: ignore[arg-type]
        reviewer=None,  # type: ignore[arg-type]
        auditor=None,  # type: ignore[arg-type]
        ticket_analyzer=None,  # type: ignore[arg-type]
        verifier=None,  # type: ignore[arg-type]
    )
    monkeypatch.setattr("worker.release_note_runner.build_odoo_client", lambda ctx, _id: odoo)
    monkeypatch.setattr(config, "DOCS_SITE_URL", "https://reva.example.com")
    set_context(ctx)
    return {"db": db, "github": github, "odoo": odoo}


def _repo(db, rid, full_name, enabled=True):
    owner, name = full_name.split("/")
    with db.session() as s:
        s.add(Repository(
            id=rid, github_repository_id=1000 + rid, owner=owner, name=name,
            full_name=full_name, default_branch="main", installation_id=7, enabled=enabled,
        ))


def _map(github, full_name, instance="wenatex"):
    owner, name = full_name.split("/")
    github.files[(owner, name, ".claude-review.yml", "main")] = f"odoo_instance: {instance}\n"


def _page(github, full_name, slug="lollipop", content=PAGE):
    owner, name = full_name.split("/")
    github.files[(owner, name, f"docs/releases/{slug}.html", "main")] = content


def _params(db, name="Lollipop", github_url=None):
    note_id = writers.record_release_note_created(
        db, odoo_instance_id=1, release_id=3275, release_name=name, slug=release_slug(name)
    )
    params = {"note_id": note_id, "odoo_instance_id": 1, "release_id": 3275,
              "release_name": name, "slug": release_slug(name)}
    if github_url is not None:
        params["github_url"] = github_url
    return params


def _ops_events(db):
    with db.session() as s:
        return [(e.component, e.severity, e.event) for e in s.execute(select(OpsEvent)).scalars()]


def test_hit_sends_url_fragment_and_css(env):
    db, gh, odoo = env["db"], env["github"], env["odoo"]
    _repo(db, 1, "acme/widgets")
    _map(gh, "acme/widgets")
    _page(gh, "acme/widgets")
    params = _params(db)

    out = run_release_note(params)

    assert out == {"status": "completed", "note_id": params["note_id"]}
    call = odoo.calls[0]
    assert (call["release_id"], call["note_id"], call["status"]) == (3275, params["note_id"], "completed")
    assert call["url"] == "https://reva.example.com/docs/?repo=1&path=docs/releases/lollipop.html"
    assert call["html"] == PAGE
    assert ".rl-page" in call["css"]
    assert call["error"] is None
    row = writers.get_release_note(db, params["note_id"])
    assert row["status"] == "completed"
    assert (row["source_repo_id"], row["source_path"]) == (1, "docs/releases/lollipop.html")
    assert row["url"] == call["url"]
    assert row["callback_sent_at"] is not None and row["completed_at"] is not None
    assert _ops_events(db) == []


def test_github_url_reads_the_named_repo_without_scanning(env):
    db, gh, odoo = env["db"], env["github"], env["odoo"]
    _repo(db, 1, "acme/widgets")             # no .claude-review.yml at all
    _repo(db, 2, "acme/other")
    _map(gh, "acme/other")
    _page(gh, "acme/widgets")
    _page(gh, "acme/other")
    params = _params(db, github_url="https://github.com/acme/widgets")

    out = run_release_note(params)

    assert out == {"status": "completed", "note_id": params["note_id"]}
    assert "repo=1" in odoo.calls[0]["url"]
    assert ("acme", "other", ".claude-review.yml") not in gh.reads
    assert _ops_events(db) == []


def test_github_url_is_case_insensitive_and_tolerates_git_suffix(env):
    db, gh, odoo = env["db"], env["github"], env["odoo"]
    _repo(db, 1, "acme/widgets")
    _page(gh, "acme/widgets")
    params = _params(db, github_url="https://github.com/ACME/Widgets.git")

    out = run_release_note(params)

    assert out == {"status": "completed", "note_id": params["note_id"]}
    assert "repo=1" in odoo.calls[0]["url"]


def test_unknown_github_url_fails_with_a_german_reason(env):
    db, odoo = env["db"], env["odoo"]
    _repo(db, 1, "acme/widgets")
    params = _params(db, github_url="https://github.com/acme/unknown")

    with pytest.raises(PermanentError):
        run_release_note(params)

    assert odoo.calls[0]["error"] == (
        "Repository https://github.com/acme/unknown ist in REVA nicht registriert oder deaktiviert"
    )
    row = writers.get_release_note(db, params["note_id"])
    assert row["status"] == "failed"


def test_disabled_repo_counts_as_unknown(env):
    db, odoo = env["db"], env["odoo"]
    _repo(db, 1, "acme/widgets", enabled=False)
    params = _params(db, github_url="https://github.com/acme/widgets")

    with pytest.raises(PermanentError):
        run_release_note(params)

    assert odoo.calls[0]["error"] == (
        "Repository https://github.com/acme/widgets ist in REVA nicht registriert oder deaktiviert"
    )


def test_named_repo_without_the_page_fails_naming_it(env):
    db, odoo = env["db"], env["odoo"]
    _repo(db, 1, "acme/widgets")
    params = _params(db, github_url="https://github.com/acme/widgets")

    with pytest.raises(PermanentError):
        run_release_note(params)

    assert odoo.calls[0]["error"] == "Kein Release-Log 'docs/releases/lollipop.html' in acme/widgets"


def test_without_github_url_the_scan_still_applies(env):
    db, gh, odoo = env["db"], env["github"], env["odoo"]
    _repo(db, 1, "acme/widgets")
    _map(gh, "acme/widgets")
    _page(gh, "acme/widgets")
    params = _params(db)

    assert "github_url" not in params
    out = run_release_note(params)

    assert out == {"status": "completed", "note_id": params["note_id"]}
    assert "repo=1" in odoo.calls[0]["url"]


def test_unmapped_and_disabled_repos_are_never_read(env):
    db, gh, odoo = env["db"], env["github"], env["odoo"]
    _repo(db, 1, "acme/widgets")
    _repo(db, 2, "acme/other")           # no odoo_instance key
    _repo(db, 3, "acme/off", enabled=False)
    _map(gh, "acme/widgets")
    _map(gh, "acme/off")
    for r in ("acme/widgets", "acme/other", "acme/off"):
        _page(gh, r)

    run_release_note(_params(db))

    assert ("acme", "other", "docs/releases/lollipop.html") not in gh.reads
    assert ("acme", "off", ".claude-review.yml") not in gh.reads
    assert odoo.calls[0]["url"].startswith("https://reva.example.com/docs/?repo=1&")


def test_other_instance_mapping_does_not_match(env):
    db, gh, odoo = env["db"], env["github"], env["odoo"]
    _repo(db, 1, "acme/widgets")
    _map(gh, "acme/widgets", instance="someone-else")
    _page(gh, "acme/widgets")
    params = _params(db)

    with pytest.raises(PermanentError):
        run_release_note(params)

    assert odoo.calls[0]["error"] == "Kein Repository mit `odoo_instance: wenatex` in .claude-review.yml"


def test_ambiguous_hit_takes_lowest_repo_id_and_records_event(env):
    db, gh, odoo = env["db"], env["github"], env["odoo"]
    _repo(db, 2, "acme/second")
    _repo(db, 1, "acme/first")
    for r in ("acme/first", "acme/second"):
        _map(gh, r)
        _page(gh, r)

    run_release_note(_params(db))

    assert odoo.calls[0]["url"] == "https://reva.example.com/docs/?repo=1&path=docs/releases/lollipop.html"
    assert ("release_log", "info", "release_doc_ambiguous") in _ops_events(db)


def test_no_release_log_sends_german_failure(env):
    db, gh, odoo = env["db"], env["github"], env["odoo"]
    _repo(db, 1, "acme/widgets")
    _repo(db, 2, "acme/other")
    _map(gh, "acme/widgets")
    _map(gh, "acme/other")
    params = _params(db, name="Big Bang 2")

    with pytest.raises(PermanentError):
        run_release_note(params)

    call = odoo.calls[0]
    assert call["status"] == "failed"
    assert call["error"] == "Kein Release-Log 'docs/releases/big-bang-2.html' in acme/widgets, acme/other"
    assert call["url"] is None and call["html"] is None and call["css"] is None
    row = writers.get_release_note(db, params["note_id"])
    assert row["status"] == "failed" and row["error"] == call["error"]
    assert row["callback_sent_at"] is not None
    assert _ops_events(db) == []   # a missing page is an outcome, not a degradation


def test_no_mapped_repo_names_the_missing_key(env):
    db, odoo = env["db"], env["odoo"]
    _repo(db, 1, "acme/widgets")   # no .claude-review.yml at all
    params = _params(db)

    with pytest.raises(PermanentError):
        run_release_note(params)

    assert odoo.calls[0]["error"] == "Kein Repository mit `odoo_instance: wenatex` in .claude-review.yml"
    assert writers.get_release_note(db, params["note_id"])["status"] == "failed"


def test_failed_callback_error_is_visible_but_does_not_mask_the_reason(env):
    db, gh, odoo = env["db"], env["github"], env["odoo"]
    _repo(db, 1, "acme/widgets")
    _map(gh, "acme/widgets")
    odoo.raise_exc = TransientError("Odoo 503")
    params = _params(db)

    with pytest.raises(PermanentError) as exc:
        run_release_note(params)

    assert "Kein Release-Log" in str(exc.value)
    row = writers.get_release_note(db, params["note_id"])
    assert row["status"] == "failed" and row["callback_sent_at"] is None
    assert ("odoo_callback", "error", "release_note_failed_callback_error") in _ops_events(db)


def test_transient_callback_keeps_row_pending_and_retries(env):
    db, gh, odoo = env["db"], env["github"], env["odoo"]
    _repo(db, 1, "acme/widgets")
    _map(gh, "acme/widgets")
    _page(gh, "acme/widgets")
    params = _params(db)
    odoo.raise_exc = TransientError("Odoo 503")

    with pytest.raises(TransientError):
        run_release_note(params)

    assert writers.get_release_note(db, params["note_id"])["status"] == "pending"
    assert ("odoo_callback", "error", "release_note_callback_failed") in _ops_events(db)

    odoo.raise_exc = None
    run_release_note(params)

    assert len(odoo.calls) == 2
    assert writers.get_release_note(db, params["note_id"])["status"] == "completed"


def test_permanent_callback_marks_failed_without_second_callback(env):
    db, gh, odoo = env["db"], env["github"], env["odoo"]
    _repo(db, 1, "acme/widgets")
    _map(gh, "acme/widgets")
    _page(gh, "acme/widgets")
    params = _params(db)
    odoo.raise_exc = PermanentError("Odoo /releases/release-note 409 (permanent): Stale note_id")

    with pytest.raises(PermanentError):
        run_release_note(params)

    row = writers.get_release_note(db, params["note_id"])
    assert row["status"] == "failed" and "409" in row["error"]
    assert len(odoo.calls) == 1
    assert ("odoo_callback", "error", "release_note_callback_failed") in _ops_events(db)


def test_resume_completed_does_not_call_odoo_again(env):
    db, gh, odoo = env["db"], env["github"], env["odoo"]
    _repo(db, 1, "acme/widgets")
    _map(gh, "acme/widgets")
    _page(gh, "acme/widgets")
    params = _params(db)
    run_release_note(params)

    out = run_release_note(params)

    assert out == {"status": "completed", "note_id": params["note_id"]}
    assert len(odoo.calls) == 1


def test_github_outage_is_transient_and_leaves_row_pending(env):
    db, gh, odoo = env["db"], env["github"], env["odoo"]
    _repo(db, 1, "acme/widgets")
    gh.raise_exc = TransientError("GitHub 502")
    params = _params(db)

    with pytest.raises(TransientError):
        run_release_note(params)

    assert odoo.calls == []
    assert writers.get_release_note(db, params["note_id"])["status"] == "pending"


def test_unreadable_config_is_skipped_with_ops_event(env):
    db, gh, odoo = env["db"], env["github"], env["odoo"]
    _repo(db, 1, "acme/broken")
    _repo(db, 2, "acme/widgets")
    _map(gh, "acme/widgets")
    _page(gh, "acme/widgets")
    gh.fail_repos.add("broken")

    run_release_note(_params(db))

    assert odoo.calls[0]["status"] == "completed"
    assert ("release_log", "warning", "config_fetch_failed") in _ops_events(db)


def test_all_repos_unreadable_is_reported_as_github_failure(env):
    db, gh, odoo = env["db"], env["github"], env["odoo"]
    _repo(db, 1, "acme/widgets")
    _repo(db, 2, "acme/second")
    gh.fail_repos.add("widgets")
    gh.fail_repos.add("second")
    params = _params(db)

    with pytest.raises(PermanentError):
        run_release_note(params)

    error = odoo.calls[0]["error"]
    assert error.startswith("GitHub-Zugriff fehlgeschlagen für alle Repositories:")
    assert "acme/widgets" in error and "acme/second" in error
    assert _ops_events(db).count(("release_log", "warning", "config_fetch_failed")) == 2


def test_malformed_config_records_parse_event(env):
    db, gh, odoo = env["db"], env["github"], env["odoo"]
    _repo(db, 1, "acme/widgets")
    gh.files[("acme", "widgets", ".claude-review.yml", "main")] = "odoo_instance: [unclosed\n"
    _page(gh, "acme/widgets")
    params = _params(db)

    with pytest.raises(PermanentError):
        run_release_note(params)

    assert odoo.calls[0]["error"] == "Kein Repository mit `odoo_instance: wenatex` in .claude-review.yml"
    assert ("release_log", "warning", "config_parse_failed") in _ops_events(db)


def test_unset_docs_site_url_is_relative_and_visible(env, monkeypatch):
    db, gh, odoo = env["db"], env["github"], env["odoo"]
    monkeypatch.setattr(config, "DOCS_SITE_URL", "")
    _repo(db, 1, "acme/widgets")
    _map(gh, "acme/widgets")
    _page(gh, "acme/widgets")

    run_release_note(_params(db))

    assert odoo.calls[0]["url"] == "/docs/?repo=1&path=docs/releases/lollipop.html"
    assert ("release_log", "warning", "docs_site_url_unset") in _ops_events(db)


def test_page_fetch_error_on_one_repo_does_not_hide_another(env):
    db, gh, odoo = env["db"], env["github"], env["odoo"]
    _repo(db, 1, "acme/widgets")
    _repo(db, 2, "acme/second")
    _map(gh, "acme/widgets")
    _map(gh, "acme/second")
    _page(gh, "acme/second")
    gh.fail_pages.add("widgets")

    run_release_note(_params(db))

    assert odoo.calls[0]["status"] == "completed"
    assert "repo=2" in odoo.calls[0]["url"]
    assert ("release_log", "warning", "page_fetch_failed") in _ops_events(db)


def test_resume_failed_row_raises_without_odoo_call(env):
    db, gh, odoo = env["db"], env["github"], env["odoo"]
    _repo(db, 1, "acme/widgets")
    _map(gh, "acme/widgets")
    params = _params(db)

    with pytest.raises(PermanentError):
        run_release_note(params)
    assert len(odoo.calls) == 1

    with pytest.raises(PermanentError):
        run_release_note(params)
    assert len(odoo.calls) == 1


def test_unbuildable_odoo_client_fails_the_row(env, monkeypatch):
    db, gh, odoo = env["db"], env["github"], env["odoo"]
    _repo(db, 1, "acme/widgets")
    _map(gh, "acme/widgets")
    _page(gh, "acme/widgets")
    monkeypatch.setattr(
        "worker.release_note_runner.build_odoo_client",
        lambda ctx, _id: (_ for _ in ()).throw(RuntimeError("bad callback url")),
    )
    params = _params(db)

    with pytest.raises(PermanentError):
        run_release_note(params)

    row = writers.get_release_note(db, params["note_id"])
    assert row["status"] == "failed"
    assert row["error"].startswith("Odoo-Client nicht konfigurierbar")
    assert ("odoo_callback", "error", "release_note_failed_callback_error") in _ops_events(db)


def test_unsafe_slug_in_job_params_fails_the_row(env):
    # Unreachable via the API (release_slug() never produces a path separator)
    # but reachable if a job is ever enqueued another way — the ValueError
    # from release_log.release_log_path() must land in the funnel, not escape.
    db, gh, odoo = env["db"], env["github"], env["odoo"]
    _repo(db, 1, "acme/widgets")
    _map(gh, "acme/widgets")
    note_id = writers.record_release_note_created(
        db, odoo_instance_id=1, release_id=3275, release_name="../x", slug="../x"
    )
    params = {
        "note_id": note_id, "odoo_instance_id": 1, "release_id": 3275,
        "release_name": "../x", "slug": "../x",
    }

    with pytest.raises(PermanentError):
        run_release_note(params)

    row = writers.get_release_note(db, note_id)
    assert row["status"] == "failed"
    assert row["error"].startswith("Unerwarteter Fehler")
    assert len(odoo.calls) == 1
    assert odoo.calls[0]["status"] == "failed"


def test_comment_only_config_is_not_reported(env):
    db, gh, odoo = env["db"], env["github"], env["odoo"]
    _repo(db, 1, "acme/widgets")
    gh.files[("acme", "widgets", ".claude-review.yml", "main")] = "# nothing here yet\n"
    params = _params(db)

    with pytest.raises(PermanentError):
        run_release_note(params)

    assert odoo.calls[0]["error"] == "Kein Repository mit `odoo_instance: wenatex` in .claude-review.yml"
    assert ("release_log", "warning", "config_parse_failed") not in _ops_events(db)


def test_superseded_row_keeps_its_reason_on_409(env):
    db, gh, odoo = env["db"], env["github"], env["odoo"]
    _repo(db, 1, "acme/widgets")
    _map(gh, "acme/widgets")
    _page(gh, "acme/widgets")
    params = _params(db)
    note_id = params["note_id"]
    odoo.on_call = lambda: writers.record_release_note_failed(
        db, note_id, "stale pending lookup superseded by re-submit"
    )
    odoo.raise_exc = PermanentError("Odoo /releases/release-note 409 (permanent): Stale note_id")

    with pytest.raises(PermanentError):
        run_release_note(params)

    row = writers.get_release_note(db, note_id)
    assert row["error"] == "stale pending lookup superseded by re-submit"
    assert ("release_log", "info", "release_note_superseded_delivery") in _ops_events(db)
    assert ("odoo_callback", "error", "release_note_callback_failed") not in _ops_events(db)
