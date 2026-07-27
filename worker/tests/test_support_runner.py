"""Tests for support_runner.run_support_answer.

Real SQLite DB so the writer + idempotency paths run against SQL. Fakes for
the answerer, the Claude Code runner, GitHub, and the Odoo callback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from reva.db import Base, Database, create_engine_from_url, writers
from reva.db.models import OpsEvent
from reva.errors import PermanentError, TransientError
from reva.ticket_knowledge import TicketKnowledge
from reva.types import ClaudeResponse, SupportAnswerResult, SupportJobParams
from worker.runner import WorkerContext, set_context
from worker.support_runner import run_support_answer

_PROMPTS_DIR = str(Path(__file__).resolve().parents[2] / "prompts")

_RESULT = SupportAnswerResult(
    request_kind="question",
    answer_status="answered",
    answer="Enable it under Settings.",
    language="en",
    confidence="high",
)


_BLOCK = {"type": "text", "text": "## Retrieved Odoo knowledge",
          "cache_control": {"type": "ephemeral"}}


def _response() -> ClaudeResponse:
    return ClaudeResponse(
        model="claude-sonnet-5", stop_reason="tool_use",
        tool_use_input=_RESULT.model_dump(mode="json"),
        input_tokens=2000, output_tokens=300,
        cache_read_tokens=0, cache_creation_tokens=0,
    )


@dataclass
class FakeAnswerer:
    calls: int = 0

    def answer_with_response(self, params, persona_block, prior_turns,
                             extra_system_blocks=None):
        self.calls += 1
        self.persona_block = persona_block
        self.prior_turns = prior_turns
        return _response(), _RESULT


@dataclass
class FakeCodeRunner:
    """Stands in for ClaudeCodeRunner: records lock/clone/review calls."""

    lock_calls: list = field(default_factory=list)
    review_calls: list = field(default_factory=list)
    lock_busy: bool = False

    def repo_lock(self, owner, name, wait_budget=None):
        if self.lock_busy:
            raise TransientError(f"repo_lock for {owner}/{name} busy")
        self.lock_calls.append((owner, name))

        class _Ctx:
            def __enter__(self_inner):
                return None

            def __exit__(self_inner, *exc):
                return False

        return _Ctx()

    def ensure_repo(self, owner, name, head_sha, token):
        return f"/repos/{owner}/{name}"

    def review(self, repo_path, skill, params, model=None, odoo=False, extra_dirs=None):
        self.review_calls.append({"skill": skill, "params": params, "odoo": odoo})
        return _response()


@dataclass
class FakeGitHub:
    installed: bool = True

    def get_repo_installation_id(self, owner, repo):
        if not self.installed:
            raise PermanentError("App not installed")
        return 1

    def get_installation_token(self, installation_id):
        return "tok"

    def get_repo(self, token, owner, repo):
        return {"default_branch": "main"}

    def get_file_content(self, token, owner, repo, path, ref):
        return None  # no .claude-review.yml -> default RepoConfig


@dataclass
class FakeOdoo:
    written: list = field(default_factory=list)
    raise_exc: Exception | None = None

    def write_field(self, ticket_id, model_name, field_name, html):
        if self.raise_exc:
            raise self.raise_exc
        self.written.append({"ticket_id": ticket_id, "field_name": field_name,
                             "html": html})


@pytest.fixture()
def db() -> Database:
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


@pytest.fixture()
def env(db, monkeypatch):
    """Wire a WorkerContext plus a thread+turn, and stub the shared helpers."""
    answerer = FakeAnswerer()
    code_runner = FakeCodeRunner()
    github = FakeGitHub()
    odoo = FakeOdoo()

    instance_id = writers.create_odoo_instance(
        db, name="inst", key_hash="h", key_prefix="p",
        callback_url="https://odoo.example", callback_api_key_enc="enc",
    ) if hasattr(writers, "create_odoo_instance") else None

    ctx = WorkerContext(
        db=db, claude=SimpleNamespace(), runner=code_runner, github=github,
        reviewer=SimpleNamespace(), auditor=SimpleNamespace(),
        ticket_analyzer=SimpleNamespace(), verifier=SimpleNamespace(),
        support_answerer=answerer, prompts_dir=_PROMPTS_DIR,
    )
    set_context(ctx)
    monkeypatch.setattr("worker.support_runner.build_odoo_client", lambda c, i: odoo)
    monkeypatch.setattr("worker.support_runner.instance_budget_exceeded",
                        lambda c, i: None)
    monkeypatch.setattr("worker.support_runner.budget_exceeded", lambda c: None)
    monkeypatch.setattr(
        "worker.support_runner.build_ticket_knowledge",
        lambda *a, **k: TicketKnowledge(planner_cost=0.01, blocks=[_BLOCK]),
    )

    thread_id = writers.get_or_create_support_thread(
        db, odoo_instance_id=instance_id, ticket_id=4711,
        model_name="helpdesk.ticket", field_name="reva_support_answer",
        github_url="https://github.com/acme/widgets",
    )
    turn_id = writers.record_support_turn_created(db, thread_id, instance_id, "Wie?")
    return SimpleNamespace(
        db=db, ctx=ctx, answerer=answerer, code_runner=code_runner,
        github=github, odoo=odoo, thread_id=thread_id, turn_id=turn_id,
        instance_id=instance_id, monkeypatch=monkeypatch,
    )


def _params(env, **over) -> dict:
    base = dict(
        turn_id=env.turn_id, thread_id=env.thread_id,
        odoo_instance_id=env.instance_id or 1, ticket_id=4711,
        model_name="helpdesk.ticket", field_name="reva_support_answer",
        subject="Rechnungslauf", question="Wie?",
        github_url="https://github.com/acme/widgets", chatter=[],
    )
    base.update(over)
    return SupportJobParams(**base).model_dump(mode="json")


def _ops(db) -> list[str]:
    with db.session() as s:
        return [e.event for e in s.query(OpsEvent).all()]


def _needs_code(env, value: bool):
    env.monkeypatch.setattr(
        "worker.support_runner.build_ticket_knowledge",
        lambda *a, **k: TicketKnowledge(
            planner_cost=0.01, blocks=[_BLOCK], needs_repo_code=value
        ),
    )


# --- docs path ---------------------------------------------------------------


def test_docs_path_answers_and_delivers(env):
    out = run_support_answer(_params(env))
    assert out["status"] == "completed"
    assert env.answerer.calls == 1
    assert env.code_runner.review_calls == []      # no escalation
    assert env.odoo.written[0]["field_name"] == "reva_support_answer"

    row = writers.get_support_turn(env.db, env.turn_id)
    assert row["status"] == "completed"
    assert row["grounding_level"] == "docs"
    assert row["answer_status"] == "answered"
    assert row["callback_sent_at"] is not None


def test_persona_block_is_passed_to_the_answerer(env):
    writers.upsert_persona(env.db, scope="default", formality="formal")
    run_support_answer(_params(env))
    assert "Persona" in env.answerer.persona_block


# --- code path ---------------------------------------------------------------


def test_code_path_takes_the_lock_and_runs_the_skill(env):
    _needs_code(env, True)
    run_support_answer(_params(env))

    assert env.code_runner.lock_calls == [("acme", "widgets")]
    assert env.code_runner.review_calls[0]["skill"] == "reva-support-answer"
    assert env.answerer.calls == 0                 # CLI replaces the API call
    assert writers.get_support_turn(env.db, env.turn_id)["grounding_level"] == "code"


def test_no_github_url_never_escalates(env):
    """Project-less requests can't be code-grounded — no repo to read."""
    _needs_code(env, True)
    run_support_answer(_params(env, github_url=None))
    assert env.code_runner.lock_calls == []
    assert env.answerer.calls == 1


def test_app_not_installed_falls_back_to_docs_with_ops_event(env):
    """Degradations must be visible, never silent."""
    _needs_code(env, True)
    env.github.installed = False
    run_support_answer(_params(env))

    assert env.code_runner.review_calls == []
    assert env.answerer.calls == 1
    assert writers.get_support_turn(env.db, env.turn_id)["grounding_level"] == "docs"
    assert "code_grounding_unavailable" in _ops(env.db)


def test_repo_lock_busy_raises_transient_for_rq_retry(env):
    _needs_code(env, True)
    env.code_runner.lock_busy = True
    with pytest.raises(TransientError):
        run_support_answer(_params(env))
    # no partial write: the turn stays pending so the retry redoes it
    assert writers.get_support_turn(env.db, env.turn_id)["status"] == "pending"


# --- budget, resume, delivery -------------------------------------------------


def test_over_instance_budget_declines_before_any_paid_call(env):
    env.monkeypatch.setattr(
        "worker.support_runner.instance_budget_exceeded", lambda c, i: 12.5
    )
    with pytest.raises(PermanentError):
        run_support_answer(_params(env))
    assert env.answerer.calls == 0
    assert writers.get_support_turn(env.db, env.turn_id)["status"] == "failed"


def test_completed_turn_resumes_without_re_paying(env):
    run_support_answer(_params(env))
    assert env.answerer.calls == 1
    run_support_answer(_params(env))          # RQ retry after a callback blip
    assert env.answerer.calls == 1            # no second paid call
    assert len(env.odoo.written) == 2         # but delivery is retried


def test_callback_failure_records_error_and_reraises(env):
    env.odoo.raise_exc = TransientError("odoo down")
    with pytest.raises(TransientError):
        run_support_answer(_params(env))
    row = writers.get_support_turn(env.db, env.turn_id)
    assert row["status"] == "completed"       # the answer is not lost
    assert row["callback_error"] is not None
    assert "write_field_failed" in _ops(env.db)


def test_grounding_level_none_when_retrieval_found_nothing(env):
    """'none' is a real, distinct outcome from 'docs' — an answer written with
    no retrieved knowledge at all should be visible as such in the TUI."""
    env.monkeypatch.setattr(
        "worker.support_runner.build_ticket_knowledge",
        lambda *a, **k: TicketKnowledge(planner_cost=0.01),
    )
    run_support_answer(_params(env))
    assert writers.get_support_turn(env.db, env.turn_id)["grounding_level"] == "none"


def test_code_grounding_over_global_budget_degrades_to_docs(env):
    """The escalation is the expensive leg — decline IT, not the whole turn, so
    the consultant still gets a docs-grounded draft."""
    _needs_code(env, True)
    env.monkeypatch.setattr("worker.support_runner.budget_exceeded", lambda c: 99.0)
    run_support_answer(_params(env))

    assert env.code_runner.review_calls == []
    assert env.answerer.calls == 1
    assert writers.get_support_turn(env.db, env.turn_id)["grounding_level"] == "docs"
    assert "code_grounding_over_budget" in _ops(env.db)


def test_code_path_keeps_public_and_internal_chatter_in_separate_params(env):
    """The never-quote rule lives in the skill and is only enforceable if the
    two groups arrive as distinguishable task parameters."""
    from reva.types import ChatterEntry
    from datetime import datetime, timezone

    _needs_code(env, True)
    chatter = [
        ChatterEntry(id=1, posted_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
                     author="Kunde", author_kind="customer", visibility="public",
                     body="PUBLIC_MSG"),
        ChatterEntry(id=2, posted_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
                     author="Dev", author_kind="internal", visibility="internal",
                     body="INTERNAL_SECRET"),
    ]
    run_support_answer(_params(env, chatter=[c.model_dump(mode="json") for c in chatter]))

    skill_params = env.code_runner.review_calls[0]["params"]
    assert "PUBLIC_MSG" in skill_params["public_chatter"]
    assert "INTERNAL_SECRET" not in skill_params["public_chatter"]
    assert "INTERNAL_SECRET" in skill_params["internal_notes"]
    assert "PUBLIC_MSG" not in skill_params["internal_notes"]


def test_terminal_failure_marks_the_turn_not_just_the_log(env):
    """A permanent error used to leave the turn in `pending`, so the
    one-pending-turn dedup rejected every future request for that record — one
    bad run took the whole ticket out of service. Reproduced in production by
    an invalid tool schema (Claude 400)."""
    def _boom(*a, **k):
        raise PermanentError("Claude 400: bad schema")

    env.answerer.answer_with_response = _boom
    with pytest.raises(PermanentError):
        run_support_answer(_params(env))

    row = writers.get_support_turn(env.db, env.turn_id)
    assert row["status"] == "failed"
    assert "bad schema" in row["error_message"]
    assert "answer_failed" in _ops(env.db)
    # the record is usable again: no pending turn blocking a new request
    assert writers.get_pending_support_turn(env.db, env.thread_id) is None
