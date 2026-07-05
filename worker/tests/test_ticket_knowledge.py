"""Ticket knowledge pipeline: planner to retrieval to system block."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from reva.core_knowledge import CoreKnowledge
from reva.db import Base, Database, create_engine_from_url
from reva.odoo_registry import load_version
from reva.ticket_knowledge import build_knowledge_block
from reva.types import ClaudeResponse

FIXTURES = Path(__file__).parent / "fixtures" / "core"
_PROMPTS = str(Path(__file__).resolve().parents[2] / "prompts")


@dataclass
class FakeClaude:
    tool_input: dict | None = None
    raise_exc: Exception | None = None
    calls: list = field(default_factory=list)

    def review(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_exc:
            raise self.raise_exc
        return ClaudeResponse(
            model="claude-haiku-4-5",
            stop_reason="tool_use",
            tool_use_input=self.tool_input,
            input_tokens=500,
            output_tokens=80,
        )


@pytest.fixture()
def core(tmp_path):
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Database(engine)
    version_dir = tmp_path / "19.0"
    shutil.copytree(FIXTURES / "odoo", version_dir / "odoo")
    shutil.copytree(FIXTURES / "documentation", version_dir / "documentation")
    (version_dir / "enterprise").mkdir()
    load_version(db, version_dir, "19.0")
    return CoreKnowledge(db, str(tmp_path), ["19.0"])


def test_happy_path_builds_cached_block(core):
    fake = FakeClaude(tool_input={
        "worth_checking": True,
        "terms": ["quotation", "template"],
        "modules": ["sale"],
    })
    block, cost, error = build_knowledge_block(
        fake, core, _PROMPTS, "19.0", "Kunde möchte Angebotsvorlagen"
    )
    assert error is None
    assert cost > 0
    assert block["cache_control"] == {"type": "ephemeral"}
    assert "Quotation templates" in block["text"]
    assert "Retrieved Odoo knowledge" in block["text"]
    call = fake.calls[0]
    assert call["tool_choice"]["name"] == "submit_core_queries"
    assert "UNTRUSTED" in call["user_prompt"]


def test_not_worth_checking_returns_none(core):
    fake = FakeClaude(tool_input={"worth_checking": False, "terms": [], "modules": []})
    block, cost, error = build_knowledge_block(
        fake, core, _PROMPTS, "19.0", "Bitte Zugang fuer neuen Mitarbeiter"
    )
    assert block is None and error is None and cost > 0


def test_planner_failure_degrades(core):
    from reva.errors import TransientError

    fake = FakeClaude(raise_exc=TransientError("429"))
    block, cost, error = build_knowledge_block(fake, core, _PROMPTS, "19.0", "text")
    assert block is None
    assert cost == 0.0
    assert error is not None and "429" in error


def test_no_retrieval_hits_returns_none(core):
    fake = FakeClaude(tool_input={
        "worth_checking": True,
        "terms": ["zzzznope"],
        "modules": [],
    })
    block, cost, error = build_knowledge_block(fake, core, _PROMPTS, "19.0", "text")
    assert block is None and error is None and cost > 0
