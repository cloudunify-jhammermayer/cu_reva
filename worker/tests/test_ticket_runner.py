"""Tests for ticket_runner.run_ticket_analysis.

Real SQLite DB so writer + idempotency paths are exercised against SQL.
Fakes for TicketAnalyzer and OdooCallbackClient.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest
import structlog

from reva.db import Base, Database, create_engine_from_url, writers
from reva.db.models import OpsEvent, TicketAnalysis
from reva.errors import MalformedModelOutput, PermanentError, TransientError
from reva.ticket_knowledge import TicketKnowledge
from reva.types import (
    ClaudeResponse,
    MissingInfoItem,
    SourcedItem,
    TicketAnalysisResult,
    TicketJobParams,
)
from worker.runner import WorkerContext, set_context
from worker.ticket_runner import repo_core_version, run_ticket_analysis

_PROMPTS_DIR = str(Path(__file__).resolve().parents[2] / "prompts")


# --- Fakes -------------------------------------------------------------------


@dataclass
class FakeTicketAnalyzer:
    result: TicketAnalysisResult | None = None
    raise_exc: Exception | None = None
    raise_once: Exception | None = None  # raised on the first call only
    call_count: int = 0
    extra_blocks: list | None = None

    def analyze_with_response(
        self,
        params: TicketJobParams,
        extra_system_blocks: list | None = None,
    ) -> tuple[ClaudeResponse, TicketAnalysisResult]:
        self.call_count += 1
        self.extra_blocks = extra_system_blocks
        if self.raise_once:
            exc, self.raise_once = self.raise_once, None
            raise exc
        if self.raise_exc:
            raise self.raise_exc
        assert self.result is not None
        response = ClaudeResponse(
            model="claude-sonnet-4-6",
            stop_reason="tool_use",
            content=[],
            input_tokens=1000,
            output_tokens=300,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            tool_use_name=None,
            tool_use_input=None,
        )
        return response, self.result


@dataclass
class FakeOdoo:
    raise_exc: Exception | None = None
    call_count: int = 0
    calls: list[dict] = field(default_factory=list)
    reset_calls: list[dict] = field(default_factory=list)

    def reset_status(self, ticket_id, model_name, analysis_id):
        self.reset_calls.append(
            {"ticket_id": ticket_id, "model_name": model_name, "analysis_id": analysis_id}
        )

    def write_field(self, ticket_id, model_name, field_name, html):
        self.call_count += 1
        self.calls.append(
            {"ticket_id": ticket_id, "model_name": model_name,
             "field_name": field_name, "html": html}
        )
        if self.raise_exc:
            raise self.raise_exc


# --- Helpers -----------------------------------------------------------------


def _good_result() -> TicketAnalysisResult:
    return TicketAnalysisResult(
        summary="The ticket is well-written.",
        missing_info=[MissingInfoItem(text="User role unspecified")],
        odoo_notes=[SourcedItem(text="Affects helpdesk.ticket")],
    )


@pytest.fixture()
def ctx_and_fakes(monkeypatch):
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Database(engine)

    analyzer = FakeTicketAnalyzer(result=_good_result())
    odoo = FakeOdoo()
    ctx = WorkerContext(
        db=db,
        claude=None,  # type: ignore[arg-type]
        runner=None,  # type: ignore[arg-type]
        github=None,  # type: ignore[arg-type]
        reviewer=None,  # type: ignore[arg-type]
        auditor=None,  # type: ignore[arg-type]
        ticket_analyzer=analyzer,  # type: ignore[arg-type]
        verifier=None,  # type: ignore[arg-type] — unused in ticket tests
    )
    monkeypatch.setattr("worker.ticket_runner.build_odoo_client", lambda ctx, _id: odoo)
    set_context(ctx)
    return {"ctx": ctx, "db": db, "analyzer": analyzer, "odoo": odoo}


def _make_params(db: Database, github_url: str | None = None) -> dict:
    params = TicketJobParams(
        analysis_id=0,
        odoo_instance_id=1,
        ticket_id=42,
        model_name="helpdesk.ticket",
        field_name="description",
        text="Add a button to the form view.",
        github_url=github_url,
    )
    analysis_id = writers.record_ticket_analysis_created(db, params)
    writers.attach_ticket_job_id(db, analysis_id, "rq:job:test-123")
    return TicketJobParams(
        analysis_id=analysis_id,
        odoo_instance_id=1,
        ticket_id=params.ticket_id,
        model_name=params.model_name,
        field_name=params.field_name,
        text=params.text,
        github_url=github_url,
    ).model_dump()


# --- Tests -------------------------------------------------------------------


def test_happy_path(ctx_and_fakes):
    s = ctx_and_fakes
    params = _make_params(s["db"])

    out = run_ticket_analysis(params)

    assert out["status"] == "completed"
    assert s["analyzer"].call_count == 1
    assert s["odoo"].call_count == 1
    # The reset callback carries the correlation id for Odoo's staleness guard.
    assert s["odoo"].reset_calls == [
        {"ticket_id": 42, "model_name": "helpdesk.ticket",
         "analysis_id": out["analysis_id"]}
    ]

    row = writers.get_ticket_analysis(s["db"], out["analysis_id"])
    assert row is not None
    assert row["status"] == "completed"
    assert row["result_html"] is not None
    assert "<h2>Summary</h2>" in row["result_html"]
    assert row["estimated_cost_usd"] is not None and row["estimated_cost_usd"] > 0


def test_github_url_persisted_and_runner_unaffected(ctx_and_fakes):
    """github_url rides TicketJobParams (spec 2026-07-12): it's stamped on the
    row at create time, round-trips through model_dump/validate, and the
    analysis runner (repo-agnostic) still completes untouched."""
    s = ctx_and_fakes
    created = writers.record_ticket_analysis_created(
        s["db"],
        TicketJobParams(
            analysis_id=0, odoo_instance_id=1, ticket_id=42,
            model_name="helpdesk.ticket", field_name="description",
            text="Add a button.", github_url="https://github.com/acme/widgets",
        ),
    )
    params = TicketJobParams(
        analysis_id=created, odoo_instance_id=1, ticket_id=42,
        model_name="helpdesk.ticket", field_name="description",
        text="Add a button.", github_url="https://github.com/acme/widgets",
    ).model_dump()
    assert params["github_url"] == "https://github.com/acme/widgets"

    out = run_ticket_analysis(params)

    assert out["status"] == "completed"
    row = writers.get_ticket_analysis(s["db"], out["analysis_id"])
    assert row["github_url"] == "https://github.com/acme/widgets"


def test_html_stored_before_odoo_call(ctx_and_fakes):
    s = ctx_and_fakes
    s["odoo"].raise_exc = PermanentError("Odoo 404")
    params = _make_params(s["db"])

    with pytest.raises(PermanentError):
        run_ticket_analysis(params)

    # DB must have result_html despite Odoo failure
    row = writers.get_ticket_analysis(s["db"], params["analysis_id"])
    assert row["result_html"] is not None
    assert row["status"] == "completed"


def test_transient_error_from_analyzer(ctx_and_fakes):
    s = ctx_and_fakes
    s["analyzer"].raise_exc = TransientError("rate limited")
    params = _make_params(s["db"])

    with pytest.raises(TransientError):
        run_ticket_analysis(params)

    row = writers.get_ticket_analysis(s["db"], params["analysis_id"])
    assert row["status"] == "pending"
    assert s["odoo"].call_count == 0


def test_permanent_error_from_analyzer(ctx_and_fakes):
    s = ctx_and_fakes
    s["analyzer"].raise_exc = PermanentError("Invalid tool call")
    params = _make_params(s["db"])

    with pytest.raises(PermanentError):
        run_ticket_analysis(params)

    row = writers.get_ticket_analysis(s["db"], params["analysis_id"])
    assert row["status"] == "failed"
    assert s["odoo"].call_count == 0


def test_malformed_output_retried_once_then_completes(ctx_and_fakes):
    s = ctx_and_fakes
    s["analyzer"].raise_once = MalformedModelOutput("truncated at max_tokens=16384")
    params = _make_params(s["db"])

    out = run_ticket_analysis(params)

    assert out["status"] == "completed"
    assert s["analyzer"].call_count == 2
    row = writers.get_ticket_analysis(s["db"], params["analysis_id"])
    assert row["status"] == "completed"
    # The absorbed first attempt must be visible as an ops event.
    with s["db"].session() as session:
        event = session.query(OpsEvent).filter_by(event="malformed_output_retried").one()
        assert event.severity == "warning"


def test_malformed_output_twice_fails_permanently(ctx_and_fakes):
    s = ctx_and_fakes
    s["analyzer"].raise_exc = MalformedModelOutput("schema validation: summary missing")
    params = _make_params(s["db"])

    with pytest.raises(PermanentError):
        run_ticket_analysis(params)

    assert s["analyzer"].call_count == 2
    row = writers.get_ticket_analysis(s["db"], params["analysis_id"])
    assert row["status"] == "failed"
    assert s["odoo"].call_count == 0


def test_transient_error_from_odoo(ctx_and_fakes):
    s = ctx_and_fakes
    s["odoo"].raise_exc = TransientError("Odoo 503")
    params = _make_params(s["db"])

    with pytest.raises(TransientError):
        run_ticket_analysis(params)

    row = writers.get_ticket_analysis(s["db"], params["analysis_id"])
    assert row["status"] == "completed"
    assert row["result_html"] is not None


def test_permanent_error_from_odoo(ctx_and_fakes):
    s = ctx_and_fakes
    s["odoo"].raise_exc = PermanentError("Odoo 401")
    params = _make_params(s["db"])

    with pytest.raises(PermanentError):
        run_ticket_analysis(params)

    row = writers.get_ticket_analysis(s["db"], params["analysis_id"])
    assert row["status"] == "completed"
    assert row["result_html"] is not None


def test_retry_after_callback_failure_does_not_reanalyze(ctx_and_fakes):
    """H7: once the result is persisted, an RQ retry (the row is already
    completed) reuses the HTML and retries only the Odoo callback — it must never
    re-run the paid analyzer. This is what makes adding retry= safe."""
    s = ctx_and_fakes
    params = _make_params(s["db"])

    # First attempt: analysis persists, then the Odoo callback fails transiently.
    s["odoo"].raise_exc = TransientError("Odoo 503")
    with pytest.raises(TransientError):
        run_ticket_analysis(params)
    assert s["analyzer"].call_count == 1
    assert writers.get_ticket_analysis(s["db"], params["analysis_id"])["status"] == "completed"

    # RQ retry: callback succeeds now. The analyzer must NOT be invoked again.
    s["odoo"].raise_exc = None
    out = run_ticket_analysis(params)
    assert out["status"] == "completed"
    assert s["analyzer"].call_count == 1  # not re-analyzed (no re-pay)
    assert s["odoo"].call_count == 2      # callback was retried


def _callback_fields(db: Database, analysis_id: int) -> tuple:
    with db.session() as s:
        row = s.get(TicketAnalysis, analysis_id)
        return row.callback_sent_at, row.callback_error


def test_callback_sent_recorded_on_success(ctx_and_fakes):
    s = ctx_and_fakes
    params = _make_params(s["db"])

    run_ticket_analysis(params)

    sent_at, error = _callback_fields(s["db"], params["analysis_id"])
    assert sent_at is not None
    assert error is None


def test_callback_failed_recorded_on_odoo_error(ctx_and_fakes):
    """A failed Odoo callback leaves callback_sent_at NULL and records the error
    (so the tab reads 'not in Odoo'), in addition to the existing ops event."""
    s = ctx_and_fakes
    s["odoo"].raise_exc = TransientError("Odoo 503")
    params = _make_params(s["db"])

    with pytest.raises(TransientError):
        run_ticket_analysis(params)

    sent_at, error = _callback_fields(s["db"], params["analysis_id"])
    assert sent_at is None
    assert error is not None and "503" in error
    with s["db"].session() as session:
        assert session.query(OpsEvent).filter_by(event="write_field_failed").count() == 1


def test_successful_retry_clears_prior_callback_error(ctx_and_fakes):
    """H7 resume path: after a callback failure, a successful retry overwrites the
    failure with callback_sent_at and clears callback_error."""
    s = ctx_and_fakes
    params = _make_params(s["db"])

    s["odoo"].raise_exc = TransientError("Odoo 503")
    with pytest.raises(TransientError):
        run_ticket_analysis(params)
    assert _callback_fields(s["db"], params["analysis_id"])[1] is not None

    s["odoo"].raise_exc = None
    run_ticket_analysis(params)
    sent_at, error = _callback_fields(s["db"], params["analysis_id"])
    assert sent_at is not None
    assert error is None


def test_malformed_html_repaired_and_ops_event_recorded(ctx_and_fakes, monkeypatch):
    """A malformed render must not fail the job: it is repaired, delivered, and an
    ops event is recorded (degradations must be visible)."""
    s = ctx_and_fakes
    monkeypatch.setattr(
        "worker.ticket_runner.format_ticket_html", lambda result: "<p>oops"
    )
    params = _make_params(s["db"])

    out = run_ticket_analysis(params)

    assert out["status"] == "completed"
    # Odoo received the repaired (well-formed) HTML, not the broken input.
    assert s["odoo"].calls[-1]["html"] == "<p>oops</p>"
    with s["db"].session() as session:
        event = session.query(OpsEvent).filter_by(event="html_repaired").one()
        assert event.component == "ticket_analysis"
        assert event.severity == "warning"


def test_wellformed_html_records_no_repair_event(ctx_and_fakes):
    s = ctx_and_fakes
    params = _make_params(s["db"])

    run_ticket_analysis(params)

    with s["db"].session() as session:
        assert session.query(OpsEvent).filter_by(event="html_repaired").count() == 0


def test_dedup_pending(ctx_and_fakes):
    s = ctx_and_fakes
    params = _make_params(s["db"])

    existing = writers.get_pending_ticket_analysis(
        s["db"],
        ticket_id=42,
        model_name="helpdesk.ticket",
        field_name="description",
        odoo_instance_id=1,
    )
    assert existing is not None
    assert existing["id"] == params["analysis_id"]
    assert existing["status"] == "pending"


def test_instance_budget_gate_declines_before_paid_call(ctx_and_fakes, monkeypatch):
    """An over-budget instance's queued job fails fast: no paid analyzer call."""
    s = ctx_and_fakes
    monkeypatch.setattr(
        "worker.ticket_runner.instance_budget_exceeded", lambda ctx, iid: 12.5
    )
    params = _make_params(s["db"])

    with pytest.raises(PermanentError):
        run_ticket_analysis(params)

    row = writers.get_ticket_analysis(s["db"], params["analysis_id"])
    assert row["status"] == "failed"
    assert "budget" in row["error_message"].lower()
    assert s["analyzer"].call_count == 0


def test_knowledge_blocks_passed_and_spend_recorded(ctx_and_fakes, monkeypatch):
    s = ctx_and_fakes
    block = {
        "type": "text",
        "text": "Retrieved Odoo knowledge ...",
        "cache_control": {"type": "ephemeral"},
    }
    monkeypatch.setattr(
        "worker.ticket_runner.build_ticket_knowledge",
        lambda *a, **k: TicketKnowledge(
            blocks=[block], planner_cost=0.002, repo_docs_sections=2
        ),
    )
    params = _make_params(s["db"])

    out = run_ticket_analysis(params)

    assert out["status"] == "completed"
    assert s["analyzer"].extra_blocks == [block]
    # The injected repo-doc section count is persisted for the TUI.
    row = writers.get_ticket_analysis(s["db"], out["analysis_id"])
    assert row["repo_docs_sections_used"] == 2

    from datetime import datetime, timedelta, timezone

    since = datetime.now(timezone.utc) - timedelta(hours=1)
    assert writers.sum_estimated_cost_since(s["db"], since) >= 0.002


def test_repo_docs_error_records_ops_event(ctx_and_fakes, monkeypatch):
    s = ctx_and_fakes
    monkeypatch.setattr(
        "worker.ticket_runner.build_ticket_knowledge",
        lambda *a, **k: TicketKnowledge(
            blocks=[], repo_docs_error="invalid github_url: 'x'", repo_docs_sections=0
        ),
    )
    params = _make_params(s["db"])

    out = run_ticket_analysis(params)

    assert out["status"] == "completed"
    with s["db"].session() as session:
        event = session.query(OpsEvent).filter_by(event="retrieval_failed").one()
        assert event.component == "repo_docs"
        assert event.severity == "warning"
    row = writers.get_ticket_analysis(s["db"], out["analysis_id"])
    assert row["repo_docs_sections_used"] == 0


def test_resume_path_leaves_repo_docs_column_untouched(ctx_and_fakes, monkeypatch):
    """Resume (completed row + persisted HTML) never re-runs retrieval, so the
    section count set on the first completion is not clobbered."""
    s = ctx_and_fakes
    monkeypatch.setattr(
        "worker.ticket_runner.build_ticket_knowledge",
        lambda *a, **k: TicketKnowledge(blocks=[], repo_docs_sections=3),
    )
    params = _make_params(s["db"])

    # First run: callback fails, but the row is persisted with the section count.
    s["odoo"].raise_exc = TransientError("Odoo 503")
    with pytest.raises(TransientError):
        run_ticket_analysis(params)
    assert writers.get_ticket_analysis(s["db"], params["analysis_id"])[
        "repo_docs_sections_used"
    ] == 3

    # Resume: analyzer not re-run; the column keeps its first value.
    s["odoo"].raise_exc = None
    run_ticket_analysis(params)
    assert s["analyzer"].call_count == 1
    assert writers.get_ticket_analysis(s["db"], params["analysis_id"])[
        "repo_docs_sections_used"
    ] == 3


# --- repo_core_version: version comes from the TARGET repo, not the instance --


@dataclass
class FakeCoreKnowledge:
    """resolve() mimics CoreKnowledge: a version is usable only if provisioned."""
    provisioned: set[str]
    resolve_calls: list[str | None] = field(default_factory=list)

    def resolve(self, version):
        self.resolve_calls.append(version)
        return version if version in self.provisioned else None


@dataclass
class FakeGithub:
    """Just enough of the GitHub client for repo_core_version + load_repo_config."""
    claude_review_yml: str | None
    default_branch: str = "main"

    def get_repo_installation_id(self, owner, repo):
        return 1

    def get_installation_token(self, installation_id):
        return "tok"

    def get_repo(self, token, owner, repo):
        return {"default_branch": self.default_branch}

    def get_file_content(self, token, owner, name, path, ref):
        assert path == ".claude-review.yml"
        return self.claude_review_yml


def _core_ctx(db, *, provisioned, yml):
    core = FakeCoreKnowledge(provisioned=set(provisioned))
    github = FakeGithub(claude_review_yml=yml)
    ctx = SimpleNamespace(db=db, github=github, core_knowledge=core)
    return ctx, core, github


@pytest.fixture()
def db():
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def _ops_events(db):
    with db.session() as s:
        return [(e.component, e.event, dict(e.detail)) for e in s.query(OpsEvent).all()]


def test_repo_core_version_uses_target_repo_config(db):
    """The version is read from the ticket's target repo .claude-review.yml —
    the instance is never consulted. Provisioned version resolves cleanly."""
    ctx, core, _ = _core_ctx(db, provisioned=["19.0"], yml="odoo: true\nodoo_version: '19.0'\n")

    version = repo_core_version(
        ctx, "https://github.com/cloudunify/ast-odoo", analysis_id=7, log=structlog.get_logger()
    )

    assert version == "19.0"
    assert core.resolve_calls == ["19.0"]
    assert _ops_events(db) == []  # clean resolve — no degradation


def test_repo_core_version_drift_records_ops_event(db):
    """A repo requesting a version /core doesn't carry (e.g. dmu on 17.0 before
    it's provisioned) degrades to no core knowledge AND records it."""
    ctx, core, _ = _core_ctx(db, provisioned=["19.0"], yml="odoo_version: '17.0'\n")

    version = repo_core_version(
        ctx, "https://github.com/cloudunify/dmu-gmbh-odoo-sh", analysis_id=8,
        log=structlog.get_logger(),
    )

    assert version is None
    assert core.resolve_calls == ["17.0"]
    assert _ops_events(db) == [
        ("core_knowledge", "version_unavailable",
         {"repo": "cloudunify/dmu-gmbh-odoo-sh", "requested": "17.0", "analysis_id": 8}),
    ]


def test_repo_core_version_defaults_to_19_when_unset(db):
    """A repo that sets no odoo_version inherits the org baseline (19.0), so
    grounding still happens — and it's still keyed off the repo, never the
    instance. (This is also what keeps the old per-analysis noise gone.)"""
    ctx, core, _ = _core_ctx(db, provisioned=["19.0"], yml="odoo: true\n")

    version = repo_core_version(
        ctx, "https://github.com/cloudunify/aurium-systems", analysis_id=9,
        log=structlog.get_logger(),
    )

    assert version == "19.0"
    assert core.resolve_calls == ["19.0"]
    assert _ops_events(db) == []


def test_repo_core_version_explicit_null_disables(db):
    """A repo opts out of core grounding with `odoo_version:` (null) in its
    .claude-review.yml — a silent skip, no resolve, no ops event."""
    ctx, core, _ = _core_ctx(db, provisioned=["19.0"], yml="odoo_version:\n")

    version = repo_core_version(
        ctx, "https://github.com/cloudunify/aurium-systems", analysis_id=9,
        log=structlog.get_logger(),
    )

    assert version is None
    assert core.resolve_calls == []
    assert _ops_events(db) == []


def test_repo_core_version_skips_without_core_or_url(db):
    """No core-knowledge layer, no github_url, or an unparseable url → None,
    and no GitHub/DB work is attempted."""
    log = structlog.get_logger()

    # core disabled
    ctx = SimpleNamespace(db=db, github=FakeGithub(claude_review_yml=None), core_knowledge=None)
    assert repo_core_version(ctx, "https://github.com/x/y", 1, log) is None

    # no url
    ctx, core, _ = _core_ctx(db, provisioned=["19.0"], yml="odoo_version: '19.0'\n")
    assert repo_core_version(ctx, None, 1, log) is None
    assert core.resolve_calls == []

    # unparseable url
    assert repo_core_version(ctx, "not-a-github-url", 1, log) is None
    assert core.resolve_calls == []
    assert _ops_events(db) == []


def test_repo_core_version_github_error_degrades(db):
    """If resolving the repo's config raises (token/API failure), grounding
    degrades to no core knowledge rather than failing the analysis."""
    ctx, core, github = _core_ctx(db, provisioned=["19.0"], yml="odoo_version: '19.0'\n")

    def boom(owner, repo):
        raise RuntimeError("GitHub 500")

    github.get_repo_installation_id = boom

    version = repo_core_version(
        ctx, "https://github.com/cloudunify/ast-odoo", 10, log=structlog.get_logger()
    )

    assert version is None
    assert _ops_events(db) == []  # transient infra error, not config drift


# --- planner-gated code grounding (Task 10) ----------------------------------


class _FakeCodeRunner:
    """Stands in for ClaudeCodeRunner on the escalated ticket path."""

    def __init__(self, result: TicketAnalysisResult, lock_busy: bool = False):
        self.result = result
        self.lock_busy = lock_busy
        self.lock_calls: list = []
        self.review_calls: list = []

    def repo_lock(self, owner, name, wait_budget=None):
        if self.lock_busy:
            raise TransientError(f"repo_lock for {owner}/{name} busy")
        self.lock_calls.append((owner, name))
        import contextlib

        return contextlib.nullcontext()

    def ensure_repo(self, owner, name, head_sha, token):
        return f"/repos/{owner}/{name}"

    def review(self, repo_path, skill, params, model=None, odoo=False, extra_dirs=None):
        self.review_calls.append({"skill": skill, "params": params})
        return ClaudeResponse(
            model="claude-sonnet-5", stop_reason="tool_use",
            tool_use_input=self.result.model_dump(mode="json"),
            input_tokens=9000, output_tokens=1200,
        )


class _FakeGitHubRepo:
    def __init__(self, installed: bool = True):
        self.installed = installed

    def get_repo_installation_id(self, owner, repo):
        if not self.installed:
            raise PermanentError("App not installed")
        return 1

    def get_installation_token(self, installation_id):
        return "tok"

    def get_repo(self, token, owner, repo):
        return {"default_branch": "main"}

    def get_file_content(self, token, owner, repo, path, ref):
        return None


def _needs_code(monkeypatch, value=True):
    monkeypatch.setattr(
        "worker.ticket_runner.build_ticket_knowledge",
        lambda *a, **k: TicketKnowledge(planner_cost=0.002, needs_repo_code=value),
    )


def _wire_repo(s, code_runner, github):
    """WorkerContext is frozen — swap in the repo-aware collaborators and
    re-register the context."""
    import dataclasses

    ctx = dataclasses.replace(s["ctx"], runner=code_runner, github=github)
    set_context(ctx)
    return ctx


_GH_URL = "https://github.com/acme/widgets"


def test_code_grounded_analysis_runs_the_skill_under_the_lock(ctx_and_fakes, monkeypatch):
    s = ctx_and_fakes
    _needs_code(monkeypatch)
    code_runner = _FakeCodeRunner(s["analyzer"].result)
    _wire_repo(s, code_runner, _FakeGitHubRepo())

    out = run_ticket_analysis(_make_params(s["db"], github_url=_GH_URL))

    assert out["status"] == "completed"
    assert code_runner.lock_calls == [("acme", "widgets")]
    assert code_runner.review_calls[0]["skill"] == "reva-ticket-analysis"
    assert s["analyzer"].call_count == 0        # the CLI replaced the API call


def test_project_less_ticket_never_escalates(ctx_and_fakes, monkeypatch):
    """github_url is None for bare project.task records — common, and there is
    no repo to read, so the gate must stay shut without an ops event."""
    s = ctx_and_fakes
    _needs_code(monkeypatch)
    code_runner = _FakeCodeRunner(s["analyzer"].result)
    _wire_repo(s, code_runner, _FakeGitHubRepo())

    run_ticket_analysis(_make_params(s["db"]))          # no github_url

    assert code_runner.review_calls == []
    assert s["analyzer"].call_count == 1
    with s["db"].session() as sess:
        events = [e.event for e in sess.query(OpsEvent).all()]
    assert "code_grounding_unavailable" not in events


def test_app_not_installed_degrades_with_ops_event(ctx_and_fakes, monkeypatch):
    s = ctx_and_fakes
    _needs_code(monkeypatch)
    code_runner = _FakeCodeRunner(s["analyzer"].result)
    _wire_repo(s, code_runner, _FakeGitHubRepo(installed=False))

    run_ticket_analysis(_make_params(s["db"], github_url=_GH_URL))

    assert code_runner.review_calls == []
    assert s["analyzer"].call_count == 1
    with s["db"].session() as sess:
        events = [e.event for e in sess.query(OpsEvent).all()]
    assert "code_grounding_unavailable" in events


def test_per_repo_kill_switch_keeps_the_ticket_on_docs(ctx_and_fakes, monkeypatch):
    """`code_grounding: false` in .claude-review.yml is the per-repo brake."""
    s = ctx_and_fakes
    _needs_code(monkeypatch)
    code_runner = _FakeCodeRunner(s["analyzer"].result)
    _wire_repo(s, code_runner, _FakeGitHubRepo())
    monkeypatch.setattr(
        "worker.ticket_runner.code_grounding_allowed", lambda config: False
    )

    run_ticket_analysis(_make_params(s["db"], github_url=_GH_URL))

    assert code_runner.review_calls == []
    assert s["analyzer"].call_count == 1
    with s["db"].session() as sess:
        events = [e.event for e in sess.query(OpsEvent).all()]
    assert "code_grounding_disabled" in events


def test_repo_lock_busy_retries_rather_than_downgrading(ctx_and_fakes, monkeypatch):
    """The planner said this ticket needs code. A busy lock must retry, not
    quietly produce a weaker docs-only analysis and call it done."""
    s = ctx_and_fakes
    _needs_code(monkeypatch)
    _wire_repo(s, _FakeCodeRunner(s["analyzer"].result, lock_busy=True),
               _FakeGitHubRepo())

    with pytest.raises(TransientError):
        run_ticket_analysis(_make_params(s["db"], github_url=_GH_URL))
    assert s["analyzer"].call_count == 0


def test_code_grounded_skill_forbids_code_identifiers_in_the_analysis():
    """The output prohibition is the whole risk of this path: a model that just
    read the repository will want to cite it, but the analysis is for a product
    owner who does not read code. Assert the skill states the rule explicitly —
    a regression here makes the analysis unusable for its actual audience."""
    from pathlib import Path

    skill = (
        Path(__file__).resolve().parents[2]
        / "prompts" / "skills" / "reva-ticket-analysis.md"
    ).read_text()
    collapsed = " ".join(skill.split()).lower()

    assert "evidence, never output" in collapsed
    # Names the concrete artifacts it must not emit, not just a vague "no code".
    for artifact in ("field", "method", "xml view", "file path"):
        assert artifact in collapsed, artifact
    # And preserves the existing carve-out for consultant-level addon names.
    assert "addon" in collapsed


def test_code_grounded_run_passes_ticket_text_not_the_analysis_prompt(
    ctx_and_fakes, monkeypatch
):
    """The CLI path gets the ticket as a fenced task parameter (review() wraps
    every value); it must not be handed a pre-rendered Messages-API prompt."""
    s = ctx_and_fakes
    _needs_code(monkeypatch)
    code_runner = _FakeCodeRunner(s["analyzer"].result)
    _wire_repo(s, code_runner, _FakeGitHubRepo())

    run_ticket_analysis(_make_params(s["db"], github_url=_GH_URL))

    skill_params = code_runner.review_calls[0]["params"]
    assert skill_params["ticket_text"] == "Add a button to the form view."
