"""Tests for reva.persona: per-field persona resolution + block rendering.

Uses SQLite in-memory, same fixture pattern as test_db.py.
"""

from __future__ import annotations

import pytest

from reva.db import Base, Database, create_engine_from_url, writers
from reva.persona import ResolvedPersona, render_persona_block, resolve_persona


@pytest.fixture()
def db() -> Database:
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


# --- resolution --------------------------------------------------------------


def test_repo_row_with_null_field_inherits_default(db: Database) -> None:
    writers.upsert_persona(db, scope="default", formality="formal", language="de")
    writers.upsert_persona(db, scope="repo", repo_full_name="acme/widgets", language="en")

    resolved = resolve_persona(db, "acme/widgets", None)

    assert resolved.language == "en"  # repo's own value wins
    assert resolved.formality == "formal"  # NULL on repo row -> inherits default


def test_repo_row_set_value_beats_default(db: Database) -> None:
    writers.upsert_persona(db, scope="default", formality="formal")
    writers.upsert_persona(db, scope="repo", repo_full_name="acme/widgets", formality="informal")

    resolved = resolve_persona(db, "acme/widgets", None)

    assert resolved.formality == "informal"


def test_no_repo_row_uses_pure_default(db: Database) -> None:
    writers.upsert_persona(
        db, scope="default", formality="formal", language="de", length="short"
    )

    resolved = resolve_persona(db, "acme/no-such-repo", None)

    assert resolved.formality == "formal"
    assert resolved.language == "de"
    assert resolved.length == "short"


def test_no_default_and_no_repo_uses_hardcoded_fallback(db: Database) -> None:
    resolved = resolve_persona(db, "acme/widgets", None)

    assert resolved.formality == "formal"
    assert resolved.language == "auto"
    assert resolved.length == "standard"

    block = render_persona_block(resolved)
    assert block.strip() != ""


def test_no_repo_full_name_uses_default(db: Database) -> None:
    writers.upsert_persona(db, scope="default", formality="formal", language="de")

    resolved = resolve_persona(db, None, None)

    assert resolved.formality == "formal"
    assert resolved.language == "de"


def test_inactive_default_row_falls_back_to_hardcoded(db: Database) -> None:
    writers.upsert_persona(db, scope="default", formality="informal", active=False)

    resolved = resolve_persona(db, None, None)

    assert resolved.formality == "formal"
    assert resolved.language == "auto"


def test_inactive_repo_row_falls_back_to_default(db: Database) -> None:
    writers.upsert_persona(db, scope="default", formality="formal")
    writers.upsert_persona(
        db, scope="repo", repo_full_name="acme/widgets", formality="informal", active=False
    )

    resolved = resolve_persona(db, "acme/widgets", None)

    assert resolved.formality == "formal"


# --- persona_context and content_policy separation ----------------------------


def test_persona_context_appends_and_does_not_clear_content_policy(db: Database) -> None:
    writers.upsert_persona(
        db, scope="default", content_policy="Never quote a delivery date."
    )

    resolved = resolve_persona(db, None, "Customer is on the enterprise plan.")

    assert resolved.content_policy == "Never quote a delivery date."
    assert resolved.persona_context == "Customer is on the enterprise plan."

    block = render_persona_block(resolved)
    assert "Never quote a delivery date." in block
    assert "Customer is on the enterprise plan." in block


def test_content_policy_renders_in_section_distinct_from_style_notes() -> None:
    resolved = ResolvedPersona(
        language="en",
        formality="formal",
        technical_depth=None,
        length=None,
        salutation=None,
        sign_off=None,
        style_notes="Keep it warm and reassuring.",
        content_policy="Never commit to a delivery date.",
    )

    block = render_persona_block(resolved)

    style_idx = block.index("Keep it warm and reassuring.")
    policy_idx = block.index("Never commit to a delivery date.")
    assert style_idx != policy_idx

    # Distinct, labelled sections rather than one undifferentiated blob.
    assert "### Style notes" in block
    assert "### Content policy" in block
    style_header_idx = block.index("### Style notes")
    policy_header_idx = block.index("### Content policy")
    assert style_header_idx < style_idx < policy_header_idx < policy_idx


# --- determinism ---------------------------------------------------------------


def test_render_persona_block_is_deterministic() -> None:
    resolved = ResolvedPersona(
        language="de",
        formality="informal",
        technical_depth="high",
        length="short",
        salutation="Hallo",
        sign_off="Beste Grüße",
        style_notes="Direct, no filler.",
        content_policy="Never quote prices.",
        persona_context="Long-standing customer.",
    )

    first = render_persona_block(resolved)
    second = render_persona_block(resolved)

    assert first == second


def test_render_persona_block_never_empty_for_bare_fallback(db: Database) -> None:
    resolved = resolve_persona(db, None, None)
    block = render_persona_block(resolved)
    assert block.strip() != ""
    assert "auto" in block
    assert "formal" in block
