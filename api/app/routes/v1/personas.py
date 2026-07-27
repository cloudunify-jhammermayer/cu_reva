"""Persona endpoints — the tone REVA drafts support answers in.

MASTER KEY ONLY. These routes are mounted on the master gate and must never be
reachable with an Odoo instance key: a persona decides what REVA says to that
customer, so an instance being able to rewrite its own tone (or read another
customer's) is a privilege boundary, not a convenience.

GET    /api/v1/personas                  — list, default row first
POST   /api/v1/personas                  — create or replace one scope
PATCH  /api/v1/personas/{persona_id}     — partial update
GET    /api/v1/personas/resolved         — what a given repo actually resolves to
"""

from __future__ import annotations

from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, status

from app.dependencies import get_db
from app.schemas.personas import (
    PersonaBody,
    PersonaOut,
    PersonaPage,
    ResolvedPersonaOut,
)
from reva.db import writers
from reva.db.engine import Database
from reva.persona import render_persona_block, resolve_persona

router = APIRouter()
logger = structlog.get_logger()

_KNOBS = (
    "language", "formality", "technical_depth", "length",
    "salutation", "sign_off", "style_notes", "content_policy", "active",
)


@router.get("/personas", response_model=PersonaPage)
def list_personas(db: Database = Depends(get_db)) -> dict:
    items = writers.list_personas(db)
    return {"items": items, "total": len(items)}


@router.get("/personas/resolved", response_model=ResolvedPersonaOut)
def get_resolved_persona(
    repo_full_name: str | None = None,
    db: Database = Depends(get_db),
) -> dict:
    """Resolve the persona a support answer for `repo_full_name` would use.

    Answers "why did REVA write it like that" directly, instead of making
    someone mentally merge the default row with the repo row.
    """
    resolved = resolve_persona(db, repo_full_name, None)
    return {
        "repo_full_name": repo_full_name,
        "language": resolved.language,
        "formality": resolved.formality,
        "technical_depth": resolved.technical_depth,
        "length": resolved.length,
        "salutation": resolved.salutation,
        "sign_off": resolved.sign_off,
        "style_notes": resolved.style_notes,
        "content_policy": resolved.content_policy,
        "rendered_block": render_persona_block(resolved),
        "resolved_at": datetime.now(timezone.utc),
    }


@router.post("/personas", status_code=status.HTTP_201_CREATED, response_model=PersonaOut)
def create_persona(body: PersonaBody, db: Database = Depends(get_db)) -> dict:
    """Create or replace the persona for a scope.

    Upsert rather than 409-on-exists: there is at most one default row and one
    row per repo (partial unique indexes), so "create" and "replace" are the
    same operation from the caller's side.
    """
    if body.scope == "repo" and not body.repo_full_name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='repo_full_name is required when scope is "repo"',
        )
    if body.scope == "default" and body.repo_full_name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='repo_full_name must be omitted when scope is "default"',
        )

    fields = {knob: getattr(body, knob) for knob in _KNOBS}
    persona_id = writers.upsert_persona(
        db, scope=body.scope, repo_full_name=body.repo_full_name, **fields
    )
    logger.info("persona_upserted", persona_id=persona_id, scope=body.scope,
                repo=body.repo_full_name)
    return _get_or_404(db, persona_id)


@router.patch("/personas/{persona_id}", response_model=PersonaOut)
def update_persona(
    persona_id: int,
    body: PersonaBody,
    db: Database = Depends(get_db),
) -> dict:
    """Replace the knobs on an existing persona.

    Deactivating (`active: false`) is a real operation, not a soft no-op:
    `resolve_persona` treats an inactive row as absent, so an inactive repo
    persona falls back to the default and an inactive default falls back to
    REVA's hardcoded persona.
    """
    existing = _get_or_404(db, persona_id)
    fields = {knob: getattr(body, knob) for knob in _KNOBS}
    writers.upsert_persona(
        db,
        scope=existing["scope"],
        repo_full_name=existing["repo_full_name"],
        **fields,
    )
    return _get_or_404(db, persona_id)


def _get_or_404(db: Database, persona_id: int) -> dict:
    for persona in writers.list_personas(db):
        if persona["id"] == persona_id:
            return persona
    raise HTTPException(status_code=404, detail="Persona not found")
