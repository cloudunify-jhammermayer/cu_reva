"""Pydantic schemas for the persona endpoints.

The enums are narrower than the DB columns (plain TEXT) on purpose: this is the
only write path, so validating here is what keeps the resolver's inputs sane.

`language` is deliberately constrained to the same set the answer schema can
report. `SupportAnswerResult.language` is `Literal["de", "en"]` under a strict
tool schema, so a persona pinning a third language would produce a prompt the
model honours and a field it is forbidden to report truthfully. Widening means
changing both.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

PersonaLanguage = Literal["auto", "de", "en"]
PersonaFormality = Literal["formal", "informal"]  # the Sie/du axis
PersonaDepth = Literal["low", "medium", "high"]
PersonaLength = Literal["brief", "standard", "detailed"]


class PersonaBody(BaseModel):
    """Create/replace payload. Every knob is optional: a repo persona leaves
    unset knobs NULL so they inherit from the default row (per-field
    resolution, not whole-row)."""

    scope: Literal["default", "repo"]
    repo_full_name: str | None = Field(
        default=None,
        description='Lowercased "owner/repo"; required when scope is "repo", '
        'must be absent when scope is "default"',
    )
    language: PersonaLanguage | None = None
    formality: PersonaFormality | None = None
    technical_depth: PersonaDepth | None = None
    length: PersonaLength | None = None
    salutation: str | None = None
    sign_off: str | None = None
    style_notes: str | None = Field(
        default=None, description="Free-text tone guidance"
    )
    content_policy: str | None = Field(
        default=None,
        description="Hard constraints rendered separately from style_notes, "
        'e.g. "never quote prices", "never commit to a delivery date"',
    )
    active: bool = True


class PersonaOut(BaseModel):
    id: int
    scope: str
    repo_full_name: str | None
    language: str | None
    formality: str | None
    technical_depth: str | None
    length: str | None
    salutation: str | None
    sign_off: str | None
    style_notes: str | None
    content_policy: str | None
    active: bool


class PersonaPage(BaseModel):
    items: list[PersonaOut]
    total: int


class ResolvedPersonaOut(BaseModel):
    """What a support answer for this repo would actually be written with —
    the resolved knobs, not any single row. This is the view that matters when
    debugging "why did REVA answer in that tone"."""

    repo_full_name: str | None
    language: str | None
    formality: str | None
    technical_depth: str | None
    length: str | None
    salutation: str | None
    sign_off: str | None
    style_notes: str | None
    content_policy: str | None
    rendered_block: str
    resolved_at: datetime
