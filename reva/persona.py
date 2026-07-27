"""Persona resolution for support-answer drafts.

Structured knobs are resolved per field, not per row: `default` row < repo
row (any NULL field on the repo row inherits the default's value) < the
additive, Odoo-authored `persona_context` free text, which is layered on top
and never overrides a knob. See
docs/superpowers/specs/2026-07-25-support-answers-design.md ("Hybrid persona
schema") for the rationale — `content_policy` is kept separate from
`style_notes` so it renders as a hard constraint rather than getting lost in
tone prose.
"""

from __future__ import annotations

from dataclasses import dataclass

from reva.db import writers
from reva.db.engine import Database

# The structured knobs, resolved independently of each other. `active` and
# the row-identity fields (id/scope/repo_full_name) are not knobs — they
# describe the row, not the tone.
_KNOB_FIELDS = (
    "language",
    "formality",
    "technical_depth",
    "length",
    "salutation",
    "sign_off",
    "style_notes",
    "content_policy",
)

# Used when there is no default persona row at all (fresh install, or the
# only default row was deactivated) — REVA must never emit an empty persona
# block.
_FALLBACK = {
    "language": "auto",
    "formality": "formal",
    "technical_depth": None,
    "length": "standard",
    "salutation": None,
    "sign_off": None,
    "style_notes": None,
    "content_policy": None,
}


@dataclass
class ResolvedPersona:
    """The knobs REVA renders into a support-answer system prompt."""

    language: str | None
    formality: str | None
    technical_depth: str | None
    length: str | None
    salutation: str | None
    sign_off: str | None
    style_notes: str | None
    content_policy: str | None
    # Additive, consultant-authored free text from the Odoo request. Layered
    # on top of the knobs above; never overrides any of them.
    persona_context: str | None = None


def resolve_persona(
    db: Database, repo_full_name: str | None, persona_context: str | None
) -> ResolvedPersona:
    """Resolve the persona to use for one support-answer turn.

    Per-field resolution: start from the default row, then overlay any
    non-NULL field from the repo row (a NULL field on the repo row inherits
    the default's value — whole rows are never overlaid wholesale). An
    inactive row is treated the same as no row at all, so deactivating a
    persona (rather than deleting it) falls back the same way.
    """
    default = writers.get_default_persona(db)
    if default is not None and not default.get("active", True):
        default = None

    repo = writers.get_repo_persona(db, repo_full_name) if repo_full_name else None
    if repo is not None and not repo.get("active", True):
        repo = None

    base = default if default is not None else _FALLBACK
    resolved = {field: base.get(field) for field in _KNOB_FIELDS}
    if repo is not None:
        for field in _KNOB_FIELDS:
            value = repo.get(field)
            if value is not None:
                resolved[field] = value

    return ResolvedPersona(persona_context=persona_context, **resolved)


def render_persona_block(resolved: ResolvedPersona) -> str:
    """Render a resolved persona into system-prompt text.

    Deterministic: identical input must produce byte-identical output — this
    block sits in a `cache_control` system block, and any instability (e.g.
    non-deterministic ordering) would silently break prompt-cache hits.
    """
    lines = ["## Persona", "Match the following tone when drafting the answer."]

    knobs = [
        ("Language", resolved.language),
        ("Formality", resolved.formality),
        ("Technical depth", resolved.technical_depth),
        ("Length", resolved.length),
        ("Salutation", resolved.salutation),
        ("Sign-off", resolved.sign_off),
    ]
    for label, value in knobs:
        if value:
            lines.append(f"- {label}: {value}")

    if resolved.style_notes:
        lines += ["", "### Style notes", resolved.style_notes]

    if resolved.content_policy:
        lines += [
            "",
            "### Content policy (hard constraints — follow exactly, never soften)",
            resolved.content_policy,
        ]

    if resolved.persona_context:
        lines += [
            "",
            "### Additional instructions from the consultant",
            resolved.persona_context,
        ]

    return "\n".join(lines)
