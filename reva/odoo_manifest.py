"""Pure structural checks for an Odoo module's `__manifest__.py`.

The model already reasons about manifests (`prompts/odoo19.md`), but the diff/delta
review paths never traverse the clone, so for a PR that only edits one module the
manifest is exactly where LLM-only review is weakest. This module adds a
deterministic floor for the unambiguous, keyword-free failure classes a regex
*can* decide — a `data`/`demo` file listed in the manifest that does not exist on
disk, a `data` list that loads views before security, and a malformed version —
analogous to `_ODOO_SEVERITY_RULES` in `worker/worker/reviewer.py`. The
taxonomy-dependent checks (used-but-undeclared `depends`) stay LLM-driven.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable
from dataclasses import dataclass

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+\.\d+\.\d+$")  # Odoo 5-part SERIES.x.y.z
_GLOB_CHARS = ("*", "?", "[")
_MAX_FILE_CHECKS = 50  # bound the GitHub contents-API calls per manifest


@dataclass(frozen=True)
class ManifestData:
    version: str | None
    depends: list[str]
    data: list[str]
    demo: list[str]


@dataclass(frozen=True)
class ManifestIssue:
    kind: str          # "missing_file" | "data_order" | "version_format"
    message: str       # human-readable, surfaced to the model
    severity_floor: str  # suggested severity: "major" | "minor"


def parse_manifest(text: str) -> ManifestData | None:
    """Parse the dict literal in a `__manifest__.py` via `ast.literal_eval`.

    Never `exec` — the manifest is untrusted repo content (SECU-1). `literal_eval`
    evaluates only literals and raises on names/calls/comprehensions, so a manifest
    with computed values (e.g. `version = SERIES + ".1.0"`) degrades to None.
    """
    try:
        node = ast.literal_eval(text)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return None
    if not isinstance(node, dict):
        return None

    def _strlist(key: str) -> list[str]:
        val = node.get(key)
        return [x for x in val if isinstance(x, str)] if isinstance(val, list) else []

    version = node.get("version")
    return ManifestData(
        version=version if isinstance(version, str) else None,
        depends=_strlist("depends"),
        data=_strlist("data"),
        demo=_strlist("demo"),
    )


def _is_view(path: str) -> bool:
    return "views/" in path or path.endswith("_views.xml")


def _is_security(path: str) -> bool:
    return "security/" in path or path.endswith("ir.model.access.csv")


def _check_data_order(data_files: list[str]) -> ManifestIssue | None:
    """Flag a `data` list that loads a view before a security file — access rules
    must exist before the views that reference the model."""
    first_view = next((i for i, p in enumerate(data_files) if _is_view(p)), None)
    last_security = max(
        (i for i, p in enumerate(data_files) if _is_security(p)), default=None
    )
    if first_view is not None and last_security is not None and first_view < last_security:
        return ManifestIssue(
            "data_order",
            "a view is loaded before a security file in `data` — load "
            "`security/` / `ir.model.access.csv` before views so access rules "
            "exist when the views reference the model",
            "minor",
        )
    return None


def audit_manifest(
    manifest_dir: str,
    data_files: list[str],
    demo_files: list[str],
    file_exists: Callable[[str], bool],
) -> list[ManifestIssue]:
    """Deterministic checks over a parsed manifest. `file_exists` is injected
    (so the module stays pure/testable) and receives the full repo-relative path."""
    issues: list[ManifestIssue] = []
    checked = 0
    for entry in [*data_files, *demo_files]:
        if checked >= _MAX_FILE_CHECKS:
            break
        if any(c in entry for c in _GLOB_CHARS):
            continue  # a glob like data/*.xml can't be stat'd — skip, don't flag
        checked += 1
        full = f"{manifest_dir}/{entry}" if manifest_dir else entry
        if not file_exists(full):
            issues.append(ManifestIssue(
                "missing_file",
                f"`{entry}` is listed in the manifest but does not exist in the module",
                "major",
            ))
    order = _check_data_order(data_files)
    if order:
        issues.append(order)
    return issues


def check_version_format(version: str | None) -> ManifestIssue | None:
    """Flag a present `version` that isn't Odoo's 5-part `SERIES.x.y.z` form
    (e.g. `19.0.1.0.0`). A missing version is not flagged here (the model handles it)."""
    if not version or _VERSION_RE.match(version):
        return None
    return ManifestIssue(
        "version_format",
        f"version `{version}` is not the Odoo 5-part SERIES.x.y.z form (e.g. 19.0.1.0.0)",
        "minor",
    )
