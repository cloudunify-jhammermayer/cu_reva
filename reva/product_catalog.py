"""Pure rules behind the docs site's "Internal modules" page.

No GitHub access here: `api/app/routes/docs.py` fetches branches, trees,
manifests, `product.yml` and issues, and hands the text and payloads to these
functions. Keeping every rule about what a product module is in one
transport-free module is what makes the page unit-testable.

Spec: docs/superpowers/specs/archive/2026-10-07-product-modules-overview-design.md
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import yaml

from reva.odoo_manifest import parse_manifest

VERSION_BRANCH_RE = re.compile(r"^\d+\.0$")
MAX_VERSION_BRANCHES = 3          # Cloudunify does not backport; older branches are frozen
STATUSES = ("available", "planned", "discontinued")
PERIODS = ("month", "year")
WONT_DO_WINDOW = timedelta(days=90)
MAX_ISSUES = 50
# `#12` in a PR title/body, but not `acme/other#12` (cross-repo) or `a#12`.
_ISSUE_REF_RE = re.compile(r"(?<![\w/])#(\d+)\b")
_STATE_RANK = {"in_progress": 0, "planned": 1, "wont_do": 2}


# --- branches -----------------------------------------------------------------

def is_version_branch(name: str) -> bool:
    return bool(VERSION_BRANCH_RE.match(name))


def select_version_branches(names: list[str]) -> tuple[list[str], list[str]]:
    """(read, ignored): the version branches sorted by major descending, the
    three highest read and the rest ignored."""
    versions = sorted(
        (n for n in names if is_version_branch(n)),
        key=lambda n: int(n.split(".")[0]),
        reverse=True,
    )
    return versions[:MAX_VERSION_BRANCHES], versions[MAX_VERSION_BRANCHES:]


# --- product.yml --------------------------------------------------------------

@dataclass
class ModuleMeta:
    """One `modules.<name>` entry of product.yml, already validated."""

    owner: str | None = None
    price: str | int | float | None = None   # "free" | number (EUR, one-time) | None
    subscription: int | float | None = None  # EUR per `per`; a module may carry both prices
    per: str | None = None                   # "month" | "year", required with subscription
    tldr: str | None = None
    features: list[str] = field(default_factory=list)
    status: str = "available"
    eta: str | None = None
    note: str | None = None


def _opt_str(value: object) -> str | None:
    # YAML turns `2026-12-01` into a date and `19.0` into a float; the page
    # shows these verbatim, so stringify any scalar instead of rejecting it.
    if value is None:
        return None
    return str(value).strip() or None


def parse_product_yml(text: str | None) -> tuple[dict[str, ModuleMeta], list[str]]:
    """Tolerant parse: every problem becomes a warning string, never an
    exception, and a bad field degrades to its default rather than dropping
    the whole entry."""
    if not text:
        return {}, []
    try:
        parsed = yaml.safe_load(text)
    except (yaml.YAMLError, ValueError) as exc:
        return {}, [f"product.yml: not valid YAML ({exc.__class__.__name__})"]
    if parsed is None:
        return {}, []
    if not isinstance(parsed, dict) or not isinstance(parsed.get("modules"), dict):
        return {}, ["product.yml: expected a top-level `modules` mapping"]

    entries: dict[str, ModuleMeta] = {}
    warnings: list[str] = []
    for key, raw in parsed["modules"].items():
        name = str(key)
        raw = {} if raw is None else raw
        if not isinstance(raw, dict):
            warnings.append(f"product.yml: entry `{name}` is not a mapping; ignored")
            continue
        meta = ModuleMeta(
            owner=_opt_str(raw.get("owner")),
            tldr=_opt_str(raw.get("tldr")),
            eta=_opt_str(raw.get("eta")),
            note=_opt_str(raw.get("note")),
        )
        price = raw.get("price")
        if price is None or (isinstance(price, (int, float)) and not isinstance(price, bool)):
            meta.price = price
        elif isinstance(price, str) and price.strip().lower() == "free":
            meta.price = "free"
        else:
            warnings.append(f"product.yml: `{name}.price` must be a number or `free`; ignored")
        sub, per = raw.get("subscription"), raw.get("per")
        if sub is not None:
            if not isinstance(sub, (int, float)) or isinstance(sub, bool):
                warnings.append(f"product.yml: `{name}.subscription` must be a number; ignored")
            elif per not in PERIODS:
                warnings.append(
                    f"product.yml: `{name}.per` must be `month` or `year` with a subscription; ignored"
                )
            else:
                meta.subscription, meta.per = sub, per
        features = raw.get("features")
        if isinstance(features, list):
            # `- Text: more text` is a one-key mapping to YAML, not a sentence;
            # keep the row readable and tell the author to quote the line.
            meta.features = [str(f) for f in features if f is not None]
            if any(isinstance(f, dict) for f in features):
                warnings.append(
                    f"product.yml: `{name}.features` has an entry with an unquoted colon; quote it"
                )
        elif features is not None:
            warnings.append(f"product.yml: `{name}.features` must be a list; ignored")
        status = raw.get("status")
        if status is not None:
            if status in STATUSES:
                meta.status = status
            else:
                warnings.append(
                    f"product.yml: `{name}.status` `{status}` is unknown; treated as available"
                )
        entries[name] = meta
    return entries, warnings


# --- modules per branch -------------------------------------------------------

def module_dirs(paths: list[str]) -> list[str]:
    """Top-level directories carrying a manifest, from a flat list of blob paths."""
    return sorted(
        p.split("/")[0] for p in paths if p.count("/") == 1 and p.endswith("/__manifest__.py")
    )


@dataclass
class BranchModule:
    module: str
    name: str | None
    summary: str | None
    meta: ModuleMeta | None   # None = no product.yml entry on this branch
    version: dict             # ModuleVersion shape (see the spec)


def _version(status: str, version: str | None, meta: ModuleMeta | None,
             manifest_error: str | None, readme_path: str | None,
             depends: list[str] = (), python_deps: list[str] = (),
             updated_at: str | None = None) -> dict:
    return {
        "status": status,
        "version": version,
        "eta": meta.eta if meta else None,
        "note": meta.note if meta else None,
        "manifest_error": manifest_error,
        "readme_path": readme_path,
        "depends": list(depends),
        "python_deps": list(python_deps),
        "updated_at": updated_at,
    }


def build_branch(
    branch: str,
    manifests: dict[str, str | None],
    readmes: set[str],
    yml: dict[str, ModuleMeta],
    updated: dict[str, str | None] | None = None,
) -> tuple[dict[str, BranchModule], list[str]]:
    """Rows for one version branch. `manifests` maps module dir -> manifest
    text (None when the fetch failed); `readmes` holds the blob paths of the
    README.md files present on the branch; `updated` maps module dir -> ISO
    date of the last commit touching it (None when unknown)."""
    updated = updated or {}
    rows: dict[str, BranchModule] = {}
    warnings: list[str] = []
    for module, text in manifests.items():
        data = parse_manifest(text) if text is not None else None
        meta = yml.get(module)
        readme = f"{module}/README.md"
        rows[module] = BranchModule(
            module=module,
            name=data.name if data else None,
            summary=data.summary if data else None,
            meta=meta,
            version=_version(
                meta.status if meta else "available",
                data.version if data else None,
                meta,
                None if data else "manifest could not be parsed",
                readme if readme in readmes else None,
                depends=data.depends if data else (),
                python_deps=data.python_deps if data else (),
                updated_at=updated.get(module),
            ),
        )
    for module, meta in yml.items():
        if module in rows:
            continue
        if meta.status == "planned":
            rows[module] = BranchModule(module, None, None, meta,
                                        _version("planned", None, meta, None, None))
        else:
            warnings.append(
                f"product.yml on {branch}: `{module}` has no module directory; ignored"
            )
    return rows, warnings


def merge_repo(
    branches: list[tuple[str, dict[str, BranchModule]]],
) -> tuple[list[dict], list[str]]:
    """One ProductModule dict per technical name across the branches given
    highest first. Owner, price, TL;DR and features come from the highest
    branch with a yml entry; name from the highest branch with a manifest;
    TL;DR falls back to the manifest summary. Owner/price/subscription drift on a lower
    branch is reported, not silently overridden."""
    modules: dict[str, dict] = {}
    source: dict[str, str] = {}   # module -> branch its yml values came from
    warnings: list[str] = []
    for branch, rows in branches:
        for module, bm in rows.items():
            row = modules.setdefault(module, {
                "module": module, "name": None, "tldr": None, "owner": None, "price": None,
                "subscription": None, "per": None,
                "features": [], "has_yml_entry": False, "versions": {},
            })
            row["versions"][branch] = bm.version
            if row["name"] is None and bm.name:
                row["name"] = bm.name
            if bm.meta is not None:
                if not row["has_yml_entry"]:
                    row["has_yml_entry"] = True
                    row["owner"], row["price"] = bm.meta.owner, bm.meta.price
                    row["subscription"], row["per"] = bm.meta.subscription, bm.meta.per
                    row["features"] = list(bm.meta.features)
                    source[module] = branch
                    if row["tldr"] is None:
                        row["tldr"] = bm.meta.tldr
                else:
                    for key in ("owner", "price", "subscription", "per"):
                        if getattr(bm.meta, key) != row[key]:
                            warnings.append(
                                f"`{module}`: {key} differs between {source[module]} and {branch}"
                            )
            if row["tldr"] is None and bm.summary:
                row["tldr"] = bm.summary
    return [modules[m] for m in sorted(modules)], warnings


# --- issues -------------------------------------------------------------------

def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def classify_issues(issues: list[dict], open_prs: list[dict], now: datetime) -> list[dict]:
    """RepoIssue dicts from raw GitHub issue payloads. PRs (entries carrying
    `pull_request`) are dropped; closed-completed issues are dropped; closed
    as not planned is kept for WONT_DO_WINDOW. Sorted in_progress, planned,
    wont_do, newest first within each state; capped at MAX_ISSUES."""
    linked: dict[int, int] = {}
    for pr in open_prs:
        text = f"{pr.get('title') or ''}\n{pr.get('body') or ''}"
        for m in _ISSUE_REF_RE.finditer(text):
            linked.setdefault(int(m.group(1)), pr["number"])

    out: list[dict] = []
    for it in issues:
        if "pull_request" in it:
            continue
        number = it["number"]
        assignees = it.get("assignees") or []
        assignee = (assignees[0].get("login") if assignees else None) or (
            (it.get("assignee") or {}).get("login")
        )
        row = {
            "number": number,
            "title": it.get("title") or "",
            "url": it.get("html_url") or "",
            "assignee": assignee,
            "pr_number": linked.get(number),
            "milestone": (it.get("milestone") or {}).get("title"),
            "created_at": it.get("created_at") or "",
            "closed_at": it.get("closed_at"),
        }
        if it.get("state") == "open":
            row["state"] = "in_progress" if (assignee or row["pr_number"]) else "planned"
        elif it.get("state_reason") == "not_planned":
            closed = _parse_ts(it.get("closed_at"))
            if closed is None or now - closed > WONT_DO_WINDOW:
                continue
            row["state"] = "wont_do"
        else:
            continue
        out.append(row)
    out.sort(key=lambda r: r["created_at"], reverse=True)   # ISO strings sort by time
    out.sort(key=lambda r: _STATE_RANK[r["state"]])          # stable: keeps newest-first
    return out[:MAX_ISSUES]
