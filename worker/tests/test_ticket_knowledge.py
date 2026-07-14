"""Ticket knowledge pipeline: one planner call feeds core + repo-docs blocks."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from reva.core_knowledge import CoreKnowledge
from reva.db import Base, Database, create_engine_from_url
from reva.odoo_registry import load_version
from reva.ticket_knowledge import build_ticket_knowledge
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


class FakeGitHub:
    """Serves a one-file default-branch tree for the repo-docs sync."""

    def __init__(self, files: dict[str, str]):
        self._files = files

    def get_repo_installation_id(self, owner, repo):
        return 1

    def get_installation_token(self, installation_id):
        return "tok"

    def get_repo(self, token, owner, repo):
        return {"default_branch": "main"}

    def get_tree(self, token, owner, repo, ref, recursive=True):
        return {
            "sha": "s1", "truncated": False,
            "tree": [{"path": p, "type": "blob", "size": 1} for p in self._files],
        }

    def get_file_content(self, token, owner, repo, path, ref):
        return self._files.get(path)


@pytest.fixture()
def env(tmp_path):
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Database(engine)
    version_dir = tmp_path / "19.0"
    shutil.copytree(FIXTURES / "odoo", version_dir / "odoo")
    shutil.copytree(FIXTURES / "documentation", version_dir / "documentation")
    (version_dir / "enterprise").mkdir()
    load_version(db, version_dir, "19.0")
    core = CoreKnowledge(db, str(tmp_path), ["19.0"])
    return SimpleNamespace(core=core, db=db)


_QUOTE_PLAN = {"worth_checking": True, "terms": ["quotation"], "modules": []}
_REPO_FILES = {
    "custom_addons/cu_sale/README.md": (
        "# Custom quotation layout\nWe customized the quotation PDF output.\n"
    ),
}


# ---- core-only path (ported) ------------------------------------------------


def test_happy_path_builds_core_block(env):
    fake = FakeClaude(tool_input={
        "worth_checking": True, "terms": ["quotation", "template"], "modules": ["sale"],
    })
    k = build_ticket_knowledge(
        fake, _PROMPTS, "Kunde möchte Angebotsvorlagen", core=env.core, version="19.0",
    )
    assert k.planner_error is None
    assert k.planner_cost > 0
    assert len(k.blocks) == 1
    block = k.blocks[0]
    assert block["cache_control"] == {"type": "ephemeral"}
    assert "Quotation templates" in block["text"]
    assert "Retrieved Odoo knowledge" in block["text"]
    assert k.repo_docs_sections is None  # repo path not attempted
    assert len(fake.calls) == 1
    assert fake.calls[0]["tool_choice"]["name"] == "submit_core_queries"
    assert "UNTRUSTED" in fake.calls[0]["user_prompt"]


def test_not_worth_checking_returns_no_blocks(env):
    fake = FakeClaude(tool_input={"worth_checking": False, "terms": [], "modules": []})
    k = build_ticket_knowledge(
        fake, _PROMPTS, "Bitte Zugang fuer neuen Mitarbeiter", core=env.core, version="19.0",
    )
    assert k.blocks == [] and k.planner_error is None and k.planner_cost > 0


def test_planner_failure_degrades(env):
    from reva.errors import TransientError

    fake = FakeClaude(raise_exc=TransientError("429"))
    k = build_ticket_knowledge(fake, _PROMPTS, "text", core=env.core, version="19.0")
    assert k.blocks == []
    assert k.planner_cost == 0.0
    assert k.planner_error is not None and "429" in k.planner_error


def test_no_core_hits_returns_no_blocks(env):
    fake = FakeClaude(tool_input={"worth_checking": True, "terms": ["zzzznope"], "modules": []})
    k = build_ticket_knowledge(fake, _PROMPTS, "text", core=env.core, version="19.0")
    assert k.blocks == [] and k.planner_error is None and k.planner_cost > 0


# ---- repo-docs path ---------------------------------------------------------


def test_single_planner_call_feeds_both_blocks(env):
    fake = FakeClaude(tool_input=_QUOTE_PLAN)
    gh = FakeGitHub(_REPO_FILES)
    k = build_ticket_knowledge(
        fake, _PROMPTS, "Angebotsvorlage", core=env.core, version="19.0",
        db=env.db, github=gh, github_url="https://github.com/acme/widgets",
    )
    assert len(fake.calls) == 1                      # ONE planner call feeds both
    assert len(k.blocks) == 2
    assert "Retrieved Odoo knowledge" in k.blocks[0]["text"]       # core first
    repo_block = k.blocks[1]["text"]
    assert "Retrieved project documentation (acme/widgets)" in repo_block
    assert "quotation PDF output" in repo_block
    assert "UNTRUSTED" in repo_block and "<project_docs_" in repo_block  # nonce-fenced
    assert k.repo_docs_sections == 1
    assert k.repo_docs_error is None


def test_core_unavailable_repo_only(env):
    fake = FakeClaude(tool_input=_QUOTE_PLAN)
    gh = FakeGitHub(_REPO_FILES)
    k = build_ticket_knowledge(
        fake, _PROMPTS, "Angebotsvorlage", core=env.core, version=None,
        db=env.db, github=gh, github_url="https://github.com/acme/widgets",
    )
    assert len(k.blocks) == 1
    assert "Retrieved project documentation" in k.blocks[0]["text"]
    assert k.repo_docs_sections == 1


def test_url_none_core_only(env):
    fake = FakeClaude(tool_input=_QUOTE_PLAN)
    k = build_ticket_knowledge(
        fake, _PROMPTS, "Angebotsvorlage", core=env.core, version="19.0",
        db=env.db, github=FakeGitHub(_REPO_FILES), github_url=None,
    )
    assert len(k.blocks) == 1
    assert "Retrieved Odoo knowledge" in k.blocks[0]["text"]
    assert k.repo_docs_sections is None


def test_invalid_url_sets_error_core_still_built(env):
    fake = FakeClaude(tool_input=_QUOTE_PLAN)
    k = build_ticket_knowledge(
        fake, _PROMPTS, "Angebotsvorlage", core=env.core, version="19.0",
        db=env.db, github=FakeGitHub(_REPO_FILES), github_url="not a url",
    )
    assert len(k.blocks) == 1  # core block still built
    assert k.repo_docs_error is not None and "invalid github_url" in k.repo_docs_error


def test_repo_no_hits_sections_zero(env):
    fake = FakeClaude(tool_input={"worth_checking": True, "terms": ["nomatchword"], "modules": []})
    gh = FakeGitHub(_REPO_FILES)
    k = build_ticket_knowledge(
        fake, _PROMPTS, "text", core=env.core, version="19.0",
        db=env.db, github=gh, github_url="https://github.com/acme/widgets",
    )
    # No repo block (terms don't match the doc), but retrieval WAS attempted.
    assert all("project documentation" not in b["text"] for b in k.blocks)
    assert k.repo_docs_sections == 0


def test_neither_source_skips_planner(env):
    fake = FakeClaude(tool_input=_QUOTE_PLAN)
    k = build_ticket_knowledge(fake, _PROMPTS, "text", core=None, version=None)
    assert len(fake.calls) == 0  # planner never runs when there's nothing to search
    assert k.blocks == [] and k.planner_cost == 0.0
    assert k.repo_docs_sections is None
