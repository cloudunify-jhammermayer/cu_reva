"""Deterministic Odoo core registry extractor."""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import structlog

from reva.db.engine import Database
from reva.db.models import (
    CoreKnowledgeVersion,
    OdooCoreField,
    OdooCoreModel,
    OdooCoreModule,
    OdooDocsSection,
)

logger = structlog.get_logger()


@dataclass
class ModelInfo:
    model: str
    kind: str
    source_path: str
    description: str | None = None


@dataclass
class FieldInfo:
    model: str
    field: str
    ftype: str | None
    string: str | None = None
    compute: str | None = None
    related: str | None = None


@dataclass
class ModuleInfo:
    module: str
    source: str
    category: str | None = None
    summary: str | None = None
    depends: list[str] = field(default_factory=list)
    models: list[ModelInfo] = field(default_factory=list)
    fields: list[FieldInfo] = field(default_factory=list)
    parse_errors: int = 0


@dataclass
class DocSection:
    path: str
    anchor: str | None
    title: str
    body: str


def iter_addon_dirs(root: Path) -> Iterator[Path]:
    """Yield addon dirs containing ``__manifest__.py`` directly under root."""
    if not root.is_dir():
        return
    for entry in sorted(root.iterdir()):
        if entry.is_dir() and (entry / "__manifest__.py").is_file():
            yield entry


def _const(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _class_models(cls: ast.ClassDef, rel_path: str) -> tuple[list[ModelInfo], str | None]:
    name = None
    description = None
    inherits: list[str] = []
    inherits_list: list[str] = []

    for stmt in cls.body:
        if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
            continue
        target = stmt.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if target.id == "_name":
            name = _const(stmt.value)
        elif target.id == "_description":
            description = _const(stmt.value)
        elif target.id == "_inherit":
            if isinstance(stmt.value, ast.List):
                inherits = [value for elt in stmt.value.elts if (value := _const(elt))]
            elif value := _const(stmt.value):
                inherits = [value]
        elif target.id == "_inherits" and isinstance(stmt.value, ast.Dict):
            inherits_list = [value for key in stmt.value.keys if (value := _const(key))]

    models: list[ModelInfo] = []
    if name:
        models.append(ModelInfo(
            model=name,
            kind="name",
            source_path=rel_path,
            description=description,
        ))
    for inherited in inherits:
        models.append(ModelInfo(model=inherited, kind="inherit", source_path=rel_path))
    for inherited in inherits_list:
        models.append(ModelInfo(model=inherited, kind="inherits", source_path=rel_path))

    attach_model = name or (inherits[0] if inherits else None)
    return models, attach_model


def _class_fields(cls: ast.ClassDef, attach_model: str | None) -> list[FieldInfo]:
    if attach_model is None:
        return []
    fields: list[FieldInfo] = []
    for stmt in cls.body:
        if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
            continue
        target = stmt.targets[0]
        call = stmt.value
        if not isinstance(target, ast.Name) or not isinstance(call, ast.Call):
            continue
        func = call.func
        if not (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "fields"
        ):
            continue
        kwargs = {kw.arg: kw.value for kw in call.keywords if kw.arg}
        fields.append(FieldInfo(
            model=attach_model,
            field=target.id,
            ftype=func.attr,
            string=_const(kwargs["string"]) if "string" in kwargs else None,
            compute=_const(kwargs["compute"]) if "compute" in kwargs else None,
            related=_const(kwargs["related"]) if "related" in kwargs else None,
        ))
    return fields


def parse_module(module_dir: Path, source: str) -> ModuleInfo:
    """Parse one addon manifest and its ``models/**/*.py`` files."""
    info = ModuleInfo(module=module_dir.name, source=source)

    try:
        manifest = ast.literal_eval((module_dir / "__manifest__.py").read_text(errors="replace"))
        if isinstance(manifest, dict):
            info.category = manifest.get("category")
            info.summary = manifest.get("summary") or manifest.get("name")
            depends = manifest.get("depends") or []
            info.depends = [dep for dep in depends if isinstance(dep, str)]
    except (OSError, SyntaxError, ValueError):
        info.parse_errors += 1
        logger.warning("core_manifest_parse_failed", module=info.module)

    for py_path in sorted(module_dir.glob("models/**/*.py")):
        try:
            tree = ast.parse(py_path.read_text(errors="replace"))
        except (OSError, SyntaxError):
            info.parse_errors += 1
            continue
        rel_path = str(py_path.relative_to(module_dir))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                models, attach_model = _class_models(node, rel_path)
                info.models.extend(models)
                info.fields.extend(_class_fields(node, attach_model))

    return info


_MAX_SECTION_CHARS = 2000
_UNDERLINE_CHARS = set("=-~^\"'#*+")


def iter_rst_files(docs_root: Path) -> Iterator[Path]:
    """Yield all ``.rst`` files under a documentation checkout."""
    content = docs_root / "content"
    root = content if content.is_dir() else docs_root
    if not root.is_dir():
        return
    yield from sorted(root.rglob("*.rst"))


def _slugify(title: str) -> str:
    return "".join(char if char.isalnum() else "-" for char in title.lower()).strip("-")


def _is_underline(line: str) -> bool:
    stripped = line.rstrip()
    return (
        len(stripped) >= 3
        and set(stripped) <= _UNDERLINE_CHARS
        and len(set(stripped)) == 1
    )


def split_rst_sections(path: Path, docs_root: Path) -> list[DocSection]:
    """Split one RST file into heading-delimited retrieval sections."""
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    rel_path = str(path.relative_to(docs_root))

    sections: list[DocSection] = []
    title: str | None = None
    body: list[str] = []

    def flush() -> None:
        if title is None:
            return
        sections.append(DocSection(
            path=rel_path,
            anchor=_slugify(title),
            title=title,
            body="\n".join(body).strip()[:_MAX_SECTION_CHARS],
        ))

    i = 0
    while i < len(lines):
        line = lines[i]
        next_line = lines[i + 1] if i + 1 < len(lines) else ""
        if line.strip() and _is_underline(next_line) and len(next_line.rstrip()) >= len(line.strip()):
            flush()
            title = line.strip()
            body = []
            i += 2
            continue
        if _is_underline(line) and title is None:
            i += 1
            continue
        body.append(line)
        i += 1
    flush()
    return sections


def _addon_roots(version_dir: Path) -> list[tuple[Path, str]]:
    return [
        (version_dir / "odoo" / "addons", "odoo"),
        (version_dir / "odoo" / "odoo" / "addons", "odoo"),
        (version_dir / "enterprise", "enterprise"),
    ]


def write_catalog(version_dir: Path, modules: list[ModuleInfo]) -> None:
    """Write greppable per-module Markdown catalogs next to a version worktree."""
    catalog = version_dir / "catalog"
    catalog.mkdir(exist_ok=True)
    for info in modules:
        lines = [
            f"# module: {info.module} ({info.source})",
            f"summary: {info.summary or ''}",
            f"category: {info.category or ''}",
            f"depends: {', '.join(info.depends)}",
            "",
        ]
        models_by_name = {model.model: model for model in info.models}
        for model_name, model in sorted(models_by_name.items()):
            lines.append(f"## model: {model_name} [{model.kind}] - {model.description or ''}")
            lines.append(f"   defined in {model.source_path}")
            for field_info in info.fields:
                if field_info.model != model_name:
                    continue
                extras = []
                if field_info.string:
                    extras.append(f'string="{field_info.string}"')
                if field_info.compute:
                    extras.append(f"compute={field_info.compute}")
                if field_info.related:
                    extras.append(f"related={field_info.related}")
                lines.append(
                    f"   field: {field_info.field} ({field_info.ftype}) {' '.join(extras)}".rstrip()
                )
            lines.append("")
        (catalog / f"{info.module}.md").write_text("\n".join(lines))


def load_version(db: Database, version_dir: Path, version: str) -> dict:
    """Parse a ``/core/<version>`` dir and replace that version's registry rows."""
    modules: list[ModuleInfo] = []
    parse_errors = 0
    for root, source in _addon_roots(version_dir):
        for addon in iter_addon_dirs(root):
            info = parse_module(addon, source=source)
            parse_errors += info.parse_errors
            modules.append(info)

    sections: list[DocSection] = []
    docs_root = version_dir / "documentation"
    if docs_root.is_dir():
        for rst_path in iter_rst_files(docs_root):
            sections.extend(split_rst_sections(rst_path, docs_root))

    write_catalog(version_dir, modules)

    model_count = sum(len(info.models) for info in modules)
    field_count = sum(len(info.fields) for info in modules)
    with db.session() as s:
        for table in (OdooCoreModule, OdooCoreModel, OdooCoreField, OdooDocsSection):
            s.query(table).filter_by(odoo_version=version).delete()
        for info in modules:
            s.add(OdooCoreModule(
                odoo_version=version,
                module=info.module,
                source=info.source,
                category=info.category,
                summary=info.summary,
                depends=info.depends,
            ))
            for model in info.models:
                s.add(OdooCoreModel(
                    odoo_version=version,
                    model=model.model,
                    module=info.module,
                    kind=model.kind,
                    source_path=model.source_path,
                    description=model.description,
                ))
            for field_info in info.fields:
                s.add(OdooCoreField(
                    odoo_version=version,
                    model=field_info.model,
                    field=field_info.field,
                    ftype=field_info.ftype,
                    module=info.module,
                    string=field_info.string,
                    compute=field_info.compute,
                    related=field_info.related,
                ))
        for section in sections:
            s.add(OdooDocsSection(
                odoo_version=version,
                path=section.path,
                anchor=section.anchor,
                title=section.title,
                body=section.body,
            ))
        existing = s.get(CoreKnowledgeVersion, version)
        if existing is None:
            existing = CoreKnowledgeVersion(odoo_version=version)
            s.add(existing)
        existing.loaded_at = datetime.now(timezone.utc)
        existing.modules = len(modules)
        existing.models = model_count
        existing.fields = field_count
        existing.sections = len(sections)

    counts = {
        "modules": len(modules),
        "models": model_count,
        "fields": field_count,
        "sections": len(sections),
        "parse_errors": parse_errors,
    }
    logger.info("core_registry_loaded", version=version, **counts)
    return counts


def _main() -> None:
    import argparse
    import os

    from reva.db.engine import create_engine_from_url
    from reva.logging import configure_logging

    configure_logging()
    parser = argparse.ArgumentParser(prog="python -m reva.odoo_registry")
    subparsers = parser.add_subparsers(dest="cmd", required=True)
    load_parser = subparsers.add_parser("load", help="parse a /core/<version> dir")
    load_parser.add_argument("version_dir", type=Path)
    load_parser.add_argument("--version", required=True)
    args = parser.parse_args()

    db = Database(create_engine_from_url(os.environ["DATABASE_URL"]))
    counts = load_version(db, args.version_dir, args.version)
    print(counts)


if __name__ == "__main__":
    _main()
