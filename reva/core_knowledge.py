"""Read-side seam over the Odoo core-knowledge layer."""

from __future__ import annotations

import re
from itertools import zip_longest
from pathlib import Path

from sqlalchemy import or_, select, text

from reva.db.engine import Database
from reva.db.models import (
    CoreKnowledgeVersion,
    OdooCoreField,
    OdooCoreModel,
    OdooCoreModule,
    OdooDocsSection,
)

_WORKTREES = ("odoo", "enterprise", "documentation")
_MAX_HINTS = 10

_ADDED_NAME_RE = re.compile(r"^\+\s*_name\s*=\s*[\"']([\w.]+)[\"']")
_ADDED_INHERIT_RE = re.compile(r"^\+\s*_inherit\s*=\s*[\"']([\w.]+)[\"']")
_ADDED_FIELD_RE = re.compile(r"^\+\s*(\w+)\s*=\s*fields\.(\w+)\(")


def extract_added_definitions(diff: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Extract added Odoo model names and fields from unified diff additions."""
    models: list[str] = []
    fields: list[tuple[str, str]] = []
    current_model: str | None = None
    for line in diff.splitlines():
        if match := _ADDED_NAME_RE.match(line):
            current_model = match.group(1)
            models.append(current_model)
        elif match := _ADDED_INHERIT_RE.match(line):
            current_model = match.group(1)
        elif match := _ADDED_FIELD_RE.match(line):
            if current_model:
                fields.append((current_model, match.group(1)))
    return models, fields


class CoreKnowledge:
    def __init__(self, db: Database, base_dir: str, versions: list[str]) -> None:
        self._db = db
        self._base = Path(base_dir)
        self._versions = versions

    def validate_startup(self) -> None:
        """Fail startup when an enabled configured version is not provisioned."""
        problems: list[str] = []
        with self._db.session() as s:
            loaded = {
                row.odoo_version
                for row in s.execute(select(CoreKnowledgeVersion)).scalars()
            }
        for version in self._versions:
            version_dir = self._base / version
            for worktree in _WORKTREES:
                path = version_dir / worktree
                if not path.is_dir():
                    problems.append(f"{version}: missing worktree {path}")
            catalog = version_dir / "catalog"
            if not catalog.is_dir() or not any(catalog.glob("*.md")):
                problems.append(f"{version}: catalog empty or missing at {catalog}")
            if version not in loaded:
                problems.append(f"{version}: no registry rows loaded; run scripts/core_sync.sh")
        if problems:
            raise RuntimeError(
                "core knowledge misconfigured (REVA_CORE_KNOWLEDGE_ENABLED=true):\n  "
                + "\n  ".join(problems)
            )

    def resolve(self, version: str | None) -> str | None:
        if not version or version not in self._versions:
            return None
        with self._db.session() as s:
            loaded = s.get(CoreKnowledgeVersion, version)
        return version if loaded is not None else None

    def core_paths(self, version: str) -> list[str]:
        version_dir = self._base / version
        return [str(version_dir / worktree) for worktree in _WORKTREES if (version_dir / worktree).is_dir()]

    def catalog_path(self, version: str) -> str:
        return str(self._base / version / "catalog")

    def _is_postgres(self, session) -> bool:
        return session.get_bind().dialect.name == "postgresql"

    def search_docs(self, version: str, terms: list[str], limit: int = 8) -> list[dict]:
        """Rank official Odoo doc sections against English search terms.

        OR-of-terms semantics, matching `repo_docs.search_repo_docs`: a section
        matches when it matches ANY term, ranked by how well it matches overall
        (`ts_rank` over the OR'd query). The planner hands us up to 13
        terms+modules — ANDing them all into one ``plainto_tsquery`` demanded
        every term appear in one section and near-never matched. Words WITHIN a
        term ("quotation template") stay ANDed — one concept.
        """
        terms = [term.strip() for term in terms if term.strip()]
        if not terms:
            return []
        with self._db.session() as s:
            if self._is_postgres(s):
                # One tsquery per term, OR'd. Only numbered placeholders are
                # interpolated into the SQL structure; term values stay bound
                # parameters (no injection surface).
                tsq = " || ".join(
                    f"plainto_tsquery('english', :t{i})" for i in range(len(terms))
                )
                rows = s.execute(
                    text(
                        "SELECT path, anchor, title, body FROM odoo_docs_sections "
                        "WHERE odoo_version = :version AND "
                        f"to_tsvector('english', title || ' ' || body) @@ ({tsq}) "
                        f"ORDER BY ts_rank(to_tsvector('english', title || ' ' || body), "
                        f"({tsq})) DESC LIMIT :limit"
                    ),
                    {
                        "version": version,
                        "limit": limit,
                        **{f"t{i}": term for i, term in enumerate(terms)},
                    },
                ).all()
                return [
                    {"path": row[0], "anchor": row[1], "title": row[2], "body": row[3]}
                    for row in rows
                ]

            clauses = [
                or_(
                    OdooDocsSection.title.ilike(f"%{term}%"),
                    OdooDocsSection.body.ilike(f"%{term}%"),
                )
                for term in terms
            ]
            rows = s.execute(
                select(OdooDocsSection)
                .where(OdooDocsSection.odoo_version == version, or_(*clauses))
                .limit(limit)
            ).scalars().all()
            return [
                {"path": row.path, "anchor": row.anchor, "title": row.title, "body": row.body}
                for row in rows
            ]

    def search_registry(self, version: str, terms: list[str], limit: int = 8) -> list[dict]:
        """Rank core modules, models and FIELDS against the planner's terms.

        Fields are searched too, and the three kinds are interleaved rather than
        concatenated. Both matter: a stock field is often the whole answer to
        "does standard Odoo already do this?", and a generic term like
        "optional" matches enough module summaries to fill `limit` on its own.
        Concatenating modules → models → fields under one `output[:limit]` made
        field rows unreachable in practice — Odoo 19's
        `sale.order.line.is_optional` sat in this table while a support answer
        denied the feature existed (ticket 6743, 2026-07-28).
        """
        terms = [term.strip() for term in terms if term.strip()]
        if not terms:
            return []
        modules_out: list[dict] = []
        models_out: list[dict] = []
        fields_out: list[dict] = []
        with self._db.session() as s:
            module_clauses = [
                or_(
                    OdooCoreModule.module.ilike(f"%{term}%"),
                    OdooCoreModule.summary.ilike(f"%{term}%"),
                    OdooCoreModule.category.ilike(f"%{term}%"),
                )
                for term in terms
            ]
            module_rows = s.execute(
                select(OdooCoreModule)
                .where(OdooCoreModule.odoo_version == version, or_(*module_clauses))
                .limit(limit)
            ).scalars()
            for module_row in module_rows:
                modules_out.append({
                    "kind": "module",
                    "name": module_row.module,
                    "module": module_row.module,
                    "summary": module_row.summary or "",
                })

            model_clauses = [
                or_(
                    OdooCoreModel.model.ilike(f"%{term}%"),
                    OdooCoreModel.description.ilike(f"%{term}%"),
                )
                for term in terms
            ]
            model_rows = s.execute(
                select(OdooCoreModel)
                .where(
                    OdooCoreModel.odoo_version == version,
                    OdooCoreModel.kind == "name",
                    or_(*model_clauses),
                )
                .limit(limit)
            ).scalars()
            for model_row in model_rows:
                models_out.append({
                    "kind": "model",
                    "name": model_row.model,
                    "module": model_row.module,
                    "summary": model_row.description or "",
                })

            field_clauses = [
                or_(
                    OdooCoreField.field.ilike(f"%{term}%"),
                    OdooCoreField.string.ilike(f"%{term}%"),
                )
                for term in terms
            ]
            field_rows = s.execute(
                select(OdooCoreField)
                .where(OdooCoreField.odoo_version == version, or_(*field_clauses))
                .limit(limit)
            ).scalars()
            for field_row in field_rows:
                label = f"{field_row.ftype or 'field'}"
                if field_row.string:
                    label += f' "{field_row.string}"'
                fields_out.append({
                    "kind": "field",
                    "name": f"{field_row.model}.{field_row.field}",
                    "module": field_row.module,
                    "summary": f"{label}, module {field_row.module}",
                })

        # Round-robin so no kind can starve another out of the `limit`.
        output: list[dict] = []
        for group in zip_longest(fields_out, models_out, modules_out):
            output.extend(hit for hit in group if hit is not None)
        return output[:limit]

    def core_overlap(
        self,
        version: str,
        added_models: list[str],
        added_fields: list[tuple[str, str]],
    ) -> list[str]:
        hints: list[str] = []
        with self._db.session() as s:
            for model, field_name in added_fields:
                row = s.execute(
                    select(OdooCoreField).where(
                        OdooCoreField.odoo_version == version,
                        OdooCoreField.model == model,
                        OdooCoreField.field == field_name,
                    )
                ).scalars().first()
                if row is not None:
                    detail = f"module {row.module}, type {row.ftype}"
                    if row.string:
                        detail += f', string "{row.string}"'
                    hints.append(
                        f"field `{field_name}` added on `{model}` already exists in core "
                        f"({detail}); check whether the custom field duplicates it"
                    )
            for model in added_models:
                prefix = model.rsplit(".", 1)[0] if "." in model else model
                rows = s.execute(
                    select(OdooCoreModel)
                    .where(
                        OdooCoreModel.odoo_version == version,
                        OdooCoreModel.kind == "name",
                        OdooCoreModel.model.like(f"{prefix}%"),
                    )
                    .limit(3)
                ).scalars().all()
                for row in rows:
                    hints.append(
                        f"new model `{model}` is close to core model `{row.model}` "
                        f"(module {row.module}, \"{row.description or ''}\"); check "
                        "whether extending it or a stock feature covers the need"
                    )
        return hints[:_MAX_HINTS]
