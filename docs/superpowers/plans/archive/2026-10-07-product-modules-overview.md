# Product Modules Overview Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the "Internal modules" placeholder on the docs site with a live overview of every sellable Odoo module: per product repo, one row per module with owner, price, summary, features, a cell per Odoo version branch, an inline README, and the repo's open issues.

**Architecture:** A repo opts in with `product: true` in `.claude-review.yml` (which also implies `review_all_paths`). Two new `/repo-docs` endpoints read the three highest `NN.0` branches of each product repo live from GitHub (manifests, `product.yml`, README presence) plus the repo's issues, through pure helpers in a new `reva/product_catalog.py`. The Vue SPA fetches the list, then each repo's detail in parallel, and renders sections as they arrive. Nothing is stored in the database.

**Tech Stack:** Python 3.14, FastAPI, Pydantic 2, PyYAML, httpx MockTransport fakes; Vue 3 + Vite SPA (`docs-ui/`), markdown-it + DOMPurify already in the bundle.

**Spec:** `docs/superpowers/specs/archive/2026-10-07-product-modules-overview-design.md`

## Global Constraints

- Only `NN.0` branches count (`^\d+\.0$`); the three highest by major are read, older ones reported as ignored. No backports.
- Module status values: `available`, `planned`, `discontinued`. No "in development"; `installable` is ignored.
- `price` is the word `free` or a number (EUR); anything else is ignored with a warning.
- Every caught-and-degraded GitHub failure both logs and records an ops event `product_catalog_degraded` (component `docs`, severity `warning`) with `repository_id` and `step`.
- Untrusted repo content (yml strings, manifest strings, README) is only ever rendered through Vue interpolation or the existing DOMPurify pipeline; never `v-html` on a raw string.
- No database migration, no TUI change, no change to `in_scope` (grounding) or `_SCOPE_VERSION`.
- Python style: match `api/app/routes/docs.py` (ruff-clean, `from __future__ import annotations`, 100-column lines). Vue style: match `DocView.vue` (`<script setup>`, `api.js` client, `persist.js` for UI state).
- Git: **stage only, never commit.** Joseph makes one commit for the whole feature. Every "Stage" step is `git add` of the files named in that task.
- Definition of done: `make test` green (worker, api, scheduler), `ruff check reva worker/worker api/app scheduler/scheduler` clean, `cd docs-ui && npm run build` clean. A change to shared `reva/` touches all three services.

## Review Focus

1. **Branch `20.0` must sort above `9.0`.** Numeric major ordering, not string ordering. Pinned in Task 4 (`test_select_branches_sorts_numerically`).
2. **A product repo whose default branch has no `.claude-review.yml`** but whose `19.0` branch says `product: true` must still appear in the list. Pinned in Task 6 (`test_products_list_falls_back_to_version_branch_config`).
3. **A truncated GitHub tree** (repo over the Trees API cap) would silently drop modules. The detail must carry a warning. Pinned in Task 7 (`test_product_detail_warns_on_truncated_tree`).
4. **YAML surprises in `product.yml`:** a numeric module key, `eta: 2026-12-01` parsed as a date, `price: "1200"` as a string, `features:` as a string. Each must degrade to a warning or a coerced string, never a 500. Pinned in Task 4 (`test_parse_product_yml_coerces_and_warns`).
5. **A `wont_do` issue with no `closed_at`** or a `closed_at` older than the window must not be listed, and a closed-completed issue never is. Pinned in Task 4 (`test_classify_issues_wont_do_window`).

---

### Task 1: `product` flag on `RepoConfig` implies `review_all_paths`

**Files:**
- Modify: `reva/types.py` (class `RepoConfig`, after `review_all_paths` at line ~68; add a validator after `_normalize_odoo_version`)
- Test: `worker/tests/test_repo_config_product.py` (new)
- Test: `worker/tests/test_reviewer.py` (add one test next to `test_review_all_paths_yml_flag_reviews_outside_custom_addons`, line ~1281)

**Interfaces:**
- Produces: `RepoConfig.product: bool` (default `False`). After validation, `product=True` guarantees `review_all_paths=True`.

- [ ] **Step 1: Write the failing tests**

`worker/tests/test_repo_config_product.py`:

```python
"""`product: true` in .claude-review.yml marks a sellable-addons repo and widens
the review scope, because product repos keep their addons at the repo root."""

from __future__ import annotations

from reva.types import RepoConfig


def test_product_implies_review_all_paths():
    cfg = RepoConfig.model_validate({"product": True})
    assert cfg.product is True
    assert cfg.review_all_paths is True


def test_product_false_leaves_review_all_paths_alone():
    assert RepoConfig.model_validate({"product": False}).review_all_paths is False
    assert RepoConfig.model_validate({}).review_all_paths is False
    assert RepoConfig.model_validate({"review_all_paths": True}).product is False
```

Append to `worker/tests/test_reviewer.py`, right after `test_review_all_paths_yml_flag_reviews_outside_custom_addons`:

```python
def test_product_yml_flag_reviews_outside_custom_addons():
    # `product: true` alone widens the scope: product repos have no custom_addons/.
    github = FakeGitHub(
        diff=_OUTSIDE_DIFF,
        files=[{"filename": "scripts/deploy.py"}],
        file_contents={".claude-review.yml": "product: true\n"},
    )
    runner = FakeRunner(response=_claude_response_with_findings([]))
    reviewer, *_ = _make_reviewer(github=github, runner=runner)
    result = reviewer.execute(_params(review_mode="diff"))
    assert result.status == "completed"
    assert "scripts/deploy.py" in runner.last_params["diff"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd worker && .venv/bin/python -m pytest tests/test_repo_config_product.py tests/test_reviewer.py -k "product" -v`
Expected: FAIL. `cfg.product` raises `AttributeError` (unknown keys are ignored by `extra="ignore"`), and the reviewer test ends `declined`.

- [ ] **Step 3: Add the field and the validator**

In `reva/types.py`, inside `RepoConfig`, directly after the `review_all_paths` line:

```python
    # A Cloudunify product repo: Odoo addons sold to customers, one branch per
    # Odoo version, addons at the repo root (OCA layout). Lists the repo on the
    # docs site's "Internal modules" page and implies review_all_paths, since
    # the default custom_addons/ lock would review nothing in such a repo.
    product: bool = False
```

At the end of the class (after `_normalize_odoo_version`):

```python
    @model_validator(mode="after")
    def _product_widens_review_scope(self) -> "RepoConfig":
        if self.product:
            self.review_all_paths = True
        return self
```

`model_validator` is already imported at the top of `reva/types.py`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd worker && .venv/bin/python -m pytest tests/test_repo_config_product.py tests/test_reviewer.py -k "product or review_all" -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Stage**

```bash
git add reva/types.py worker/tests/test_repo_config_product.py worker/tests/test_reviewer.py
```

---

### Task 2: Move the config loader into `reva/`

**Files:**
- Create: `reva/repo_config.py`
- Modify: `worker/worker/repo_config.py` (keep `resolve_repo_context` and `code_grounding_allowed`; drop `load_repo_config` and `FileContentReader`)
- Modify: `worker/worker/reviewer.py:61`, `worker/worker/auditor.py:22`, `worker/worker/release_note_runner.py:26`, `worker/worker/ticket_runner.py:29-31`
- Test: existing worker suites (no new test; this is a move)

**Interfaces:**
- Produces: `reva.repo_config.load_repo_config(github, token, owner, name, ref, *, on_invalid=None) -> RepoConfig` and `reva.repo_config.FileContentReader` (Protocol with `get_file_content(token, owner, repo, path, ref) -> str | None`). Identical behaviour to today's worker function.

- [ ] **Step 1: Create `reva/repo_config.py`**

Copy lines 1 to 79 of `worker/worker/repo_config.py` (the module docstring, imports, `FileContentReader`, `load_repo_config`) verbatim into `reva/repo_config.py`. Change only the first docstring line to:

```python
"""Shared .claude-review.yml loading for every service.

Reviews load the config at the PR head SHA; audits and the docs site's
product page at a branch. All degrade to the empty (default) config on a
missing, malformed, or invalid file — a bad config must never fail a run.
"""
```

- [ ] **Step 2: Shrink the worker module**

In `worker/worker/repo_config.py` delete the `FileContentReader` protocol and the `load_repo_config` function, remove the now-unused imports (`Callable`, `Protocol`, `yaml`, `ValidationError`, `RepoConfig`, `structlog`/`logger` if nothing else uses them), and add at the top:

```python
from reva.repo_config import load_repo_config
```

(`resolve_repo_context` still calls it.) Update the docstring's first line to "Repo-context helpers for code-grounded worker runs."

- [ ] **Step 3: Point the four callers at `reva`**

```python
# worker/worker/reviewer.py, auditor.py, release_note_runner.py
from reva.repo_config import load_repo_config
```

In `worker/worker/ticket_runner.py` the import is a parenthesised group from `worker.repo_config`; keep the other names there and add a separate `from reva.repo_config import load_repo_config` line, removing `load_repo_config` from the group.

- [ ] **Step 4: Run the worker suite and ruff**

Run: `cd worker && .venv/bin/python -m pytest tests/ -q && cd .. && ruff check reva worker/worker`
Expected: all PASS, ruff clean (an unused import left behind in `worker/repo_config.py` shows up here).

- [ ] **Step 5: Stage**

```bash
git add reva/repo_config.py worker/worker/repo_config.py worker/worker/reviewer.py worker/worker/auditor.py worker/worker/release_note_runner.py worker/worker/ticket_runner.py
```

---

### Task 3: Manifest parser also yields `name` and `summary`

**Files:**
- Modify: `reva/odoo_manifest.py` (`ManifestData`, `parse_manifest`)
- Test: `worker/tests/test_odoo_manifest.py` (append; `parse_manifest` is already imported there)

**Interfaces:**
- Produces: `ManifestData.name: str | None`, `ManifestData.summary: str | None` (new fields with defaults, appended after `demo` so positional construction stays valid).

- [ ] **Step 1: Write the failing test**

Append to `worker/tests/test_odoo_manifest.py`:

```python
def test_parse_manifest_exposes_name_and_summary():
    data = parse_manifest(
        "{'name': 'Helpdesk SLA', 'summary': 'SLA timers', 'version': '19.0.1.3.0',"
        " 'depends': ['helpdesk']}"
    )
    assert data.name == "Helpdesk SLA"
    assert data.summary == "SLA timers"
    assert data.version == "19.0.1.3.0"


def test_parse_manifest_non_string_name_is_none():
    data = parse_manifest("{'name': 42, 'summary': None}")
    assert data.name is None
    assert data.summary is None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd worker && .venv/bin/python -m pytest tests/test_odoo_manifest.py -k "name_and_summary or non_string_name" -v`
Expected: FAIL with `AttributeError: 'ManifestData' object has no attribute 'name'`.

- [ ] **Step 3: Extend the dataclass and parser**

```python
@dataclass(frozen=True)
class ManifestData:
    version: str | None
    depends: list[str]
    data: list[str]
    demo: list[str]
    name: str | None = None
    summary: str | None = None
```

In `parse_manifest`, replace the `return ManifestData(...)` with:

```python
    version = node.get("version")
    name, summary = node.get("name"), node.get("summary")
    return ManifestData(
        version=version if isinstance(version, str) else None,
        depends=_strlist("depends"),
        data=_strlist("data"),
        demo=_strlist("demo"),
        name=name if isinstance(name, str) else None,
        summary=summary if isinstance(summary, str) else None,
    )
```

- [ ] **Step 4: Run the manifest tests**

Run: `cd worker && .venv/bin/python -m pytest tests/test_odoo_manifest.py -v`
Expected: PASS.

- [ ] **Step 5: Stage**

```bash
git add reva/odoo_manifest.py worker/tests/test_odoo_manifest.py
```

---

### Task 4: `reva/product_catalog.py`, the pure rules

**Files:**
- Create: `reva/product_catalog.py`
- Test: `worker/tests/test_product_catalog.py` (new)

**Interfaces:**
- Consumes: `reva.odoo_manifest.parse_manifest` (Task 3).
- Produces (all pure, no I/O):
  - `is_version_branch(name: str) -> bool`
  - `select_version_branches(names: list[str]) -> tuple[list[str], list[str]]` → `(read, ignored)`, both descending by major.
  - `ModuleMeta` dataclass: `owner, price, tldr, features, status, eta, note`.
  - `parse_product_yml(text: str | None) -> tuple[dict[str, ModuleMeta], list[str]]` → `(entries, warnings)`.
  - `module_dirs(paths: list[str]) -> list[str]` → sorted top-level dirs with a manifest.
  - `BranchModule` dataclass: `module, name, summary, meta: ModuleMeta | None, version: dict`.
  - `build_branch(branch: str, manifests: dict[str, str | None], readmes: set[str], yml: dict[str, ModuleMeta]) -> tuple[dict[str, BranchModule], list[str]]`.
  - `merge_repo(branches: list[tuple[str, dict[str, BranchModule]]]) -> tuple[list[dict], list[str]]` → `ProductModule` dicts (spec shape), highest branch first in the input.
  - `classify_issues(issues: list[dict], open_prs: list[dict], now: datetime) -> list[dict]` → `RepoIssue` dicts (spec shape).
  - Constants `MAX_VERSION_BRANCHES = 3`, `WONT_DO_WINDOW = timedelta(days=90)`, `MAX_ISSUES = 50`.

- [ ] **Step 1: Write the failing tests**

`worker/tests/test_product_catalog.py`:

```python
"""Pure rules behind the docs site's Internal modules page."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from reva.product_catalog import (
    BranchModule,
    ModuleMeta,
    build_branch,
    classify_issues,
    is_version_branch,
    merge_repo,
    module_dirs,
    parse_product_yml,
    select_version_branches,
)

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


# --- branches -----------------------------------------------------------------

def test_is_version_branch():
    assert is_version_branch("19.0")
    assert is_version_branch("20.0")
    assert not is_version_branch("19.1")
    assert not is_version_branch("saas-19.4")
    assert not is_version_branch("main")
    assert not is_version_branch("v19.0")


def test_select_branches_sorts_numerically():
    read, ignored = select_version_branches(["9.0", "main", "20.0", "18.0", "19.0", "17.0"])
    assert read == ["20.0", "19.0", "18.0"]
    assert ignored == ["17.0", "9.0"]


def test_select_branches_fewer_than_three():
    assert select_version_branches(["main", "19.0"]) == (["19.0"], [])
    assert select_version_branches(["main"]) == ([], [])


# --- product.yml --------------------------------------------------------------

def test_parse_product_yml_full_entry():
    entries, warnings = parse_product_yml(
        "modules:\n"
        "  cu_helpdesk_sla:\n"
        "    owner: Joseph H.\n"
        "    price: 1200\n"
        "    tldr: SLA timers\n"
        "    features:\n      - A\n      - B\n"
        "  cu_helpdesk_kb:\n"
        "    price: free\n    status: planned\n    eta: 2027-Q1\n"
        "  cu_helpdesk_chat:\n"
        "    status: discontinued\n    note: Replaced by core\n"
    )
    assert warnings == []
    sla = entries["cu_helpdesk_sla"]
    assert (sla.owner, sla.price, sla.tldr, sla.features, sla.status) == (
        "Joseph H.", 1200, "SLA timers", ["A", "B"], "available",
    )
    kb = entries["cu_helpdesk_kb"]
    assert (kb.price, kb.status, kb.eta) == ("free", "planned", "2027-Q1")
    assert entries["cu_helpdesk_chat"].note == "Replaced by core"


def test_parse_product_yml_tolerates_missing_and_malformed():
    assert parse_product_yml(None) == ({}, [])
    assert parse_product_yml("") == ({}, [])
    entries, warnings = parse_product_yml("modules: [\n")
    assert entries == {} and warnings and "not valid YAML" in warnings[0]
    entries, warnings = parse_product_yml("- just\n- a list\n")
    assert entries == {} and "modules" in warnings[0]
    entries, warnings = parse_product_yml("modules:\n  cu_x: just a string\n")
    assert entries == {} and "cu_x" in warnings[0]


def test_parse_product_yml_coerces_and_warns():
    entries, warnings = parse_product_yml(
        "modules:\n"
        "  19.0:\n    owner: Num Key\n"            # numeric key -> "19.0"
        "  cu_a:\n    eta: 2026-12-01\n"           # YAML date -> "2026-12-01"
        "  cu_b:\n    price: '1200'\n"             # string price -> warning, None
        "  cu_c:\n    features: not a list\n"      # -> warning, []
        "  cu_d:\n    status: in_development\n"    # unknown -> warning, available
        "  cu_e:\n    price: 99.5\n"               # float is fine
        "  cu_f:\n"                                # empty entry is fine
    )
    assert entries["19.0"].owner == "Num Key"
    assert entries["cu_a"].eta == "2026-12-01"
    assert entries["cu_b"].price is None
    assert entries["cu_c"].features == []
    assert entries["cu_d"].status == "available"
    assert entries["cu_e"].price == 99.5
    assert entries["cu_f"] == ModuleMeta()
    joined = "\n".join(warnings)
    assert "cu_b.price" in joined and "cu_c.features" in joined and "cu_d.status" in joined
    assert len(warnings) == 3


# --- modules per branch -------------------------------------------------------

def test_module_dirs_top_level_manifests_only():
    assert module_dirs([
        "cu_a/__manifest__.py", "cu_b/__manifest__.py", "cu_b/models/x.py",
        "nested/cu_c/__manifest__.py", "README.md", "cu_a/README.md",
    ]) == ["cu_a", "cu_b"]


def test_build_branch_merges_manifest_yml_and_readme():
    yml, _ = parse_product_yml(
        "modules:\n  cu_a:\n    owner: J\n    status: discontinued\n    note: gone\n"
        "  cu_planned:\n    status: planned\n    eta: Q1\n"
        "  cu_ghost:\n    owner: X\n"
    )
    rows, warnings = build_branch(
        "19.0",
        {"cu_a": "{'name': 'A', 'summary': 'Sum', 'version': '19.0.1.0.0'}", "cu_broken": "import os"},
        {"cu_a/README.md"},
        yml,
    )
    assert set(rows) == {"cu_a", "cu_broken", "cu_planned"}
    a = rows["cu_a"]
    assert (a.name, a.summary) == ("A", "Sum")
    assert a.version == {
        "status": "discontinued", "version": "19.0.1.0.0", "eta": None, "note": "gone",
        "manifest_error": None, "readme_path": "cu_a/README.md",
    }
    broken = rows["cu_broken"]
    assert broken.meta is None
    assert broken.version["manifest_error"] and broken.version["version"] is None
    assert broken.version["readme_path"] is None
    assert rows["cu_planned"].version == {
        "status": "planned", "version": None, "eta": "Q1", "note": None,
        "manifest_error": None, "readme_path": None,
    }
    assert warnings == ["product.yml on 19.0: `cu_ghost` has no module directory; ignored"]


def _bm(module, branch_meta=None, name=None, summary=None, version="19.0.1.0.0"):
    return BranchModule(
        module=module, name=name, summary=summary, meta=branch_meta,
        version={"status": branch_meta.status if branch_meta else "available", "version": version,
                 "eta": None, "note": None, "manifest_error": None, "readme_path": None},
    )


def test_merge_repo_highest_branch_wins_and_reports_drift():
    hi = ModuleMeta(owner="Joseph", price=1200, tldr="New words", features=["x"])
    lo = ModuleMeta(owner="Markus", price=1200, tldr="Old words", features=["y"])
    modules, warnings = merge_repo([
        ("19.0", {"cu_a": _bm("cu_a", hi, name="A"), "cu_only19": _bm("cu_only19", None, summary="S")}),
        ("18.0", {"cu_a": _bm("cu_a", lo, name="A old", version="18.0.1.0.0")}),
    ])
    assert [m["module"] for m in modules] == ["cu_a", "cu_only19"]
    a = modules[0]
    assert (a["name"], a["owner"], a["price"], a["tldr"], a["features"]) == (
        "A", "Joseph", 1200, "New words", ["x"],
    )
    assert a["has_yml_entry"] is True
    assert set(a["versions"]) == {"19.0", "18.0"}
    assert a["versions"]["18.0"]["version"] == "18.0.1.0.0"
    only = modules[1]
    assert only["has_yml_entry"] is False
    assert only["tldr"] == "S"          # manifest summary fallback
    assert only["owner"] is None and only["price"] is None
    assert "18.0" not in only["versions"]
    assert warnings == ["`cu_a`: owner differs between 19.0 and 18.0"]


def test_merge_repo_yml_tldr_beats_manifest_summary_same_branch():
    meta = ModuleMeta(tldr="From yml")
    modules, _ = merge_repo([("19.0", {"cu_a": _bm("cu_a", meta, summary="From manifest")})])
    assert modules[0]["tldr"] == "From yml"


# --- issues -------------------------------------------------------------------

def _issue(number, state="open", **kw):
    base = {
        "number": number, "title": f"[feature] 6{number:03d} - thing {number}",
        "html_url": f"https://github.com/acme/x/issues/{number}", "state": state,
        "created_at": f"2026-09-{number:02d}T00:00:00Z", "assignees": [], "assignee": None,
        "milestone": None, "closed_at": None, "state_reason": None,
    }
    base.update(kw)
    return base


def test_classify_issues_states_and_order():
    issues = [
        _issue(1),                                                   # planned
        _issue(2, assignees=[{"login": "joseph"}]),                 # in_progress (assignee)
        _issue(3),                                                   # in_progress (linked PR)
        _issue(4, state="closed", state_reason="not_planned",
               closed_at="2026-10-01T00:00:00Z"),                    # wont_do
        _issue(5, state="closed", state_reason="completed",
               closed_at="2026-10-01T00:00:00Z"),                    # dropped
        _issue(6, pull_request={"url": "x"}),                        # PR masquerading, dropped
        _issue(7, milestone={"title": "19.0.2"}),                    # planned with eta
    ]
    prs = [{"number": 50, "title": "Fix #3 timers", "body": None},
           {"number": 51, "title": "Unrelated", "body": "see acme/other#1"}]
    out = classify_issues(issues, prs, NOW)
    assert [(i["number"], i["state"]) for i in out] == [
        (3, "in_progress"), (2, "in_progress"), (7, "planned"), (1, "planned"), (4, "wont_do"),
    ]
    by = {i["number"]: i for i in out}
    assert by[2]["assignee"] == "joseph" and by[2]["pr_number"] is None
    assert by[3]["pr_number"] == 50
    assert by[7]["milestone"] == "19.0.2"
    assert by[4]["closed_at"] == "2026-10-01T00:00:00Z"
    assert by[1]["url"].endswith("/issues/1")


def test_classify_issues_wont_do_window():
    old = (NOW - timedelta(days=91)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = (NOW - timedelta(days=89)).strftime("%Y-%m-%dT%H:%M:%SZ")
    issues = [
        _issue(1, state="closed", state_reason="not_planned", closed_at=old),
        _issue(2, state="closed", state_reason="not_planned", closed_at=recent),
        _issue(3, state="closed", state_reason="not_planned", closed_at=None),
    ]
    assert [i["number"] for i in classify_issues(issues, [], NOW)] == [2]


def test_classify_issues_capped_at_50():
    issues = [_issue(n) for n in range(1, 61)]
    assert len(classify_issues(issues, [], NOW)) == 50
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd worker && .venv/bin/python -m pytest tests/test_product_catalog.py -q`
Expected: FAIL at import, `ModuleNotFoundError: No module named 'reva.product_catalog'`.

- [ ] **Step 3: Write `reva/product_catalog.py`**

```python
"""Pure rules behind the docs site's "Internal modules" page.

No GitHub access here: `api/app/routes/docs.py` fetches branches, trees,
manifests, `product.yml` and issues, and hands the text and payloads to these
functions. Keeping every rule about what a product module is in one
transport-free module is what makes the page unit-testable.

Spec: docs/superpowers/specs/2026-10-07-product-modules-overview-design.md
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
    price: str | int | float | None = None   # "free" | number (EUR) | None
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
    except yaml.YAMLError as exc:
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
        features = raw.get("features")
        if isinstance(features, list):
            meta.features = [str(f) for f in features if f is not None]
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
             manifest_error: str | None, readme_path: str | None) -> dict:
    return {
        "status": status,
        "version": version,
        "eta": meta.eta if meta else None,
        "note": meta.note if meta else None,
        "manifest_error": manifest_error,
        "readme_path": readme_path,
    }


def build_branch(
    branch: str,
    manifests: dict[str, str | None],
    readmes: set[str],
    yml: dict[str, ModuleMeta],
) -> tuple[dict[str, BranchModule], list[str]]:
    """Rows for one version branch. `manifests` maps module dir -> manifest
    text (None when the fetch failed); `readmes` holds the blob paths of the
    README.md files present on the branch."""
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
    TL;DR falls back to the manifest summary. Owner/price drift on a lower
    branch is reported, not silently overridden."""
    modules: dict[str, dict] = {}
    source: dict[str, str] = {}   # module -> branch its yml values came from
    warnings: list[str] = []
    for branch, rows in branches:
        for module, bm in rows.items():
            row = modules.setdefault(module, {
                "module": module, "name": None, "tldr": None, "owner": None, "price": None,
                "features": [], "has_yml_entry": False, "versions": {},
            })
            row["versions"][branch] = bm.version
            if row["name"] is None and bm.name:
                row["name"] = bm.name
            if bm.meta is not None:
                if not row["has_yml_entry"]:
                    row["has_yml_entry"] = True
                    row["owner"], row["price"] = bm.meta.owner, bm.meta.price
                    row["features"] = list(bm.meta.features)
                    source[module] = branch
                    if row["tldr"] is None:
                        row["tldr"] = bm.meta.tldr
                else:
                    for key in ("owner", "price"):
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
```

- [ ] **Step 4: Run the tests**

Run: `cd worker && .venv/bin/python -m pytest tests/test_product_catalog.py -v && cd .. && ruff check reva`
Expected: PASS (13 tests), ruff clean.

- [ ] **Step 5: Stage**

```bash
git add reva/product_catalog.py worker/tests/test_product_catalog.py
```

---

### Task 5: GitHub client lists issues and open PRs

**Files:**
- Modify: `reva/github_client.py` (add two methods after `get_issue`, ~line 275)
- Test: `worker/tests/test_github_client_lists.py` (new)

**Interfaces:**
- Produces: `GitHubClient.list_issues(token, owner, repo, *, state: str, since: str | None = None) -> list[dict]` (one page of 100, raw GitHub payloads, PRs included) and `GitHubClient.list_open_pull_requests(token, owner, repo) -> list[dict]` (one page of 100, raw payloads).

- [ ] **Step 1: Write the failing tests**

```python
"""Issue + PR listing for the docs site's product page: one page each, raw payloads."""

from __future__ import annotations

import httpx

from reva.github_client import GitHubClient


def _client(handler) -> GitHubClient:
    gh = GitHubClient.__new__(GitHubClient)
    gh.base_url = "https://api.github.com"
    gh._client = httpx.Client(transport=httpx.MockTransport(handler))
    return gh


def test_list_issues_open_page():
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/acme/widgets/issues"
        assert request.url.params["state"] == "open"
        assert request.url.params["per_page"] == "100"
        assert "since" not in request.url.params
        assert request.headers["Authorization"] == "Bearer tok"
        return httpx.Response(200, json=[{"number": 1, "state": "open"}])

    assert _client(handle).list_issues("tok", "acme", "widgets", state="open") == [
        {"number": 1, "state": "open"},
    ]


def test_list_issues_closed_since():
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.params["state"] == "closed"
        assert request.url.params["since"] == "2026-07-09T12:00:00Z"
        return httpx.Response(200, json=[])

    assert _client(handle).list_issues(
        "tok", "acme", "widgets", state="closed", since="2026-07-09T12:00:00Z"
    ) == []


def test_list_open_pull_requests():
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/acme/widgets/pulls"
        assert request.url.params["state"] == "open"
        assert request.url.params["per_page"] == "100"
        return httpx.Response(200, json=[{"number": 7, "title": "Fix #3"}])

    assert _client(handle).list_open_pull_requests("tok", "acme", "widgets") == [
        {"number": 7, "title": "Fix #3"},
    ]
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd worker && .venv/bin/python -m pytest tests/test_github_client_lists.py -q`
Expected: FAIL with `AttributeError: 'GitHubClient' object has no attribute 'list_issues'`.

- [ ] **Step 3: Add the methods**

After `get_issue` in `reva/github_client.py`:

```python
    def list_issues(
        self, token: str, owner: str, repo: str, *, state: str, since: str | None = None
    ) -> list[dict]:
        """One page (100) of a repo's issues as GitHub returns them — PRs
        included, the caller drops entries carrying `pull_request`. Backs the
        docs site's product page. `since` (ISO 8601) filters on updated_at,
        which the page uses as "closed within the window" (a close is an
        update). Newest first."""
        params: dict = {
            "state": state, "per_page": PAGE_SIZE, "sort": "created", "direction": "desc",
        }
        if since:
            params["since"] = since
        response = self._get(token, f"/repos/{owner}/{repo}/issues", params=params)
        return response.json()

    def list_open_pull_requests(self, token: str, owner: str, repo: str) -> list[dict]:
        """One page (100) of open PRs, raw payloads (title/body are what the
        product page scans for `#<issue>` references)."""
        response = self._get(
            token, f"/repos/{owner}/{repo}/pulls", params={"state": "open", "per_page": PAGE_SIZE}
        )
        return response.json()
```

- [ ] **Step 4: Run the tests**

Run: `cd worker && .venv/bin/python -m pytest tests/test_github_client_lists.py -v && cd .. && ruff check reva`
Expected: PASS (3 tests), ruff clean.

- [ ] **Step 5: Stage**

```bash
git add reva/github_client.py worker/tests/test_github_client_lists.py
```

---

### Task 6: Product flag, product list endpoint, and docs-tree scope for product repos

**Files:**
- Modify: `reva/repo_docs.py` (`browser_in_scope` gains `addon_roots`)
- Modify: `api/app/doc_cache.py` (two caches + `clear_all`)
- Modify: `api/app/schemas/docs.py` (`ProductRepoRef`, `ProductList`)
- Modify: `api/app/routes/docs.py` (`_cached_branches`, `_product_flag`, `_degrade`, `_cached_tree(product=)`, `doc_branches`, `doc_tree`, `doc_titles`, `doc_search`, `doc_file`, new `list_products`)
- Test: `worker/tests/test_repo_docs.py` (append after the existing `test_browser_in_scope`)
- Test: `api/tests/test_docs_products.py` (new; list + tree tests)

**Interfaces:**
- Consumes: `reva.repo_config.load_repo_config` (Task 2), `reva.product_catalog.select_version_branches`, `module_dirs` (Task 4).
- Produces:
  - `reva.repo_docs.browser_in_scope(path, addon_roots=()) -> bool`.
  - `api.app.routes.docs._cached_branches(github, repository_id, owner, name, token) -> list[dict]` (raw `[{"name","sha"}]`, cached).
  - `api.app.routes.docs._product_flag(github, repository_id, meta, token) -> bool` (cached).
  - `api.app.routes.docs._cached_tree(github, repository_id, owner, name, ref, token, product=False) -> {"entries","truncated","addon_roots"}`.
  - `GET /repo-docs/products` → `ProductList`.
  - Caches `product_flag_cache`, `products_cache` in `api/app/doc_cache.py`.

- [ ] **Step 1: Write the failing scope test**

Append to `worker/tests/test_repo_docs.py` (`browser_in_scope` is already imported there):

```python
def test_browser_in_scope_addon_roots_widen_product_repos():
    roots = ("cu_a", "cu_b")
    # root-level addon docs become visible
    assert browser_in_scope("cu_a/README.md", roots)
    assert browser_in_scope("cu_a/docs/consultant.md", roots)
    assert browser_in_scope("cu_a/docs/guide.html", roots)
    # html outside a docs/ folder stays out, as does the manifest stub
    assert not browser_in_scope("cu_a/static/description/index.html", roots)
    # directories without a manifest are not addons
    assert not browser_in_scope("scripts/README.md", roots)
    # repo-root files and agent files stay out
    assert not browser_in_scope("README.md", roots)
    assert not browser_in_scope("cu_a/CLAUDE.md", roots)
    assert not browser_in_scope("cu_a/docs/superpowers/x.md", roots)
    # the default (no roots) is unchanged
    assert not browser_in_scope("cu_a/README.md")
    assert browser_in_scope("custom_addons/cu_a/README.md")
```

Run: `cd worker && .venv/bin/python -m pytest tests/test_repo_docs.py -k addon_roots -q`
Expected: FAIL with `TypeError: browser_in_scope() takes 1 positional argument but 2 were given`.

- [ ] **Step 2: Widen `browser_in_scope`**

Replace the function in `reva/repo_docs.py`:

```python
def browser_in_scope(path: str, addon_roots: tuple[str, ...] | frozenset[str] = ()) -> bool:
    """True for anything the consultant docs browser serves as text.

    Every markdown doc `in_scope` covers, plus HTML that sits inside a `docs/`
    folder — the repo root's or an addon's own. `in_scope` stays the narrower
    grounding scope; only `api/app/routes/docs.py` calls this one.

    `addon_roots` names the top-level addon directories of a *product* repo
    (addons at the repo root, OCA layout): markdown anywhere under such a
    root and HTML under its `docs/` are in scope too. Empty for every other
    repo, so the default behaviour is unchanged.
    """
    if in_scope(path):
        return True
    segments = [seg.lower() for seg in path.split("/")[:-1]]
    if any(seg in EXCLUDED_SEGMENTS for seg in segments):
        return False
    lower = path.lower()
    if lower.endswith(BROWSER_DOC_EXTENSIONS) and path.startswith(SCOPE_PREFIXES):
        return "docs" in segments
    if addon_roots and segments and path.split("/")[0] in addon_roots:
        if path.endswith(tuple("/" + b for b in EXCLUDED_BASENAMES)):
            return False
        if lower.endswith(DOC_EXTENSIONS):
            return True
        return lower.endswith(BROWSER_DOC_EXTENSIONS) and "docs" in segments
    return False
```

Run the scope tests: `cd worker && .venv/bin/python -m pytest tests/test_repo_docs.py -q` → PASS, including the pre-existing parametrised cases.

- [ ] **Step 3: Write the failing api tests**

`api/tests/test_docs_products.py` (the fixtures come from `test_docs.py`; pytest's default prepend import mode puts `api/tests` on `sys.path`):

```python
"""Tests for the docs site's product page endpoints (/repo-docs/products)."""

from __future__ import annotations

from test_docs import _FakeGitHub, _seed_repo, _use_github, env  # noqa: F401

from reva.db.models import OpsEvent
from reva.errors import TransientError

MANIFEST = "{'name': 'Helpdesk SLA', 'summary': 'SLA timers', 'version': '%s.1.0.0'}"


class _ProductGitHub(_FakeGitHub):
    """Per-ref trees and files, plus issues/PRs. `files` keys are `(ref, path)`
    with a plain `path` fallback so a file can be the same on every branch."""

    def __init__(self, *, trees=None, files=None, branches=None, issues=None, prs=None,
                 tree_errors=None, file_errors=()):
        super().__init__(branches=branches)
        self.trees = trees or {}
        self.files = files or {}
        self.issues = issues or []
        self.prs = prs or []
        self.tree_errors = tree_errors or {}
        self.file_errors = set(file_errors)

    def get_tree(self, token, owner, repo, ref, recursive=True):
        if ref in self.tree_errors:
            raise self.tree_errors[ref]
        return self.trees.get(ref, {"tree": [], "truncated": False})

    def get_file_content(self, token, owner, repo, path, ref):
        if path in self.file_errors:
            raise TransientError("boom")
        if (ref, path) in self.files:
            return self.files[(ref, path)]
        return self.files.get(path)

    def list_issues(self, token, owner, repo, *, state, since=None):
        return [i for i in self.issues if i["state"] == state]

    def list_open_pull_requests(self, token, owner, repo):
        return self.prs


def _blobs(*paths):
    return {"tree": [{"path": p, "type": "blob", "size": 1} for p in paths], "truncated": False}


def _product_repo(db, name="cu-helpdesk", branches=("19.0", "18.0")):
    rid = _seed_repo(db, owner="Cloudunify", name=name, branch="main")
    gh = _ProductGitHub(
        branches=[{"name": "main", "sha": "m"}] + [{"name": b, "sha": b} for b in branches],
        trees={b: _blobs("cu_helpdesk_sla/__manifest__.py", "cu_helpdesk_sla/README.md",
                         "cu_helpdesk_sla/docs/guide.html", "product.yml", "README.md")
               for b in branches},
        files={".claude-review.yml": "product: true\n",
               **{(b, "cu_helpdesk_sla/__manifest__.py"): MANIFEST % b for b in branches},
               "product.yml": "modules:\n  cu_helpdesk_sla:\n    owner: J\n    price: 1200\n"},
    )
    return rid, gh


# --- GET /repo-docs/products ---------------------------------------------------

def test_products_list_only_product_repos(env):
    client, db, _ = env
    rid, gh = _product_repo(db)
    _seed_repo(db, owner="Cloudunify", name="customer-x")   # no product flag
    _use_github(gh)
    body = client.get("/repo-docs/products").json()
    assert [r["repository_id"] for r in body["items"]] == [rid]
    assert body["items"][0]["full_name"] == "Cloudunify/cu-helpdesk"
    assert body["items"][0]["html_url"] == "https://github.com/Cloudunify/cu-helpdesk"


def test_products_list_falls_back_to_version_branch_config(env):
    client, db, _ = env
    rid, gh = _product_repo(db)
    del gh.files[".claude-review.yml"]
    gh.files[("19.0", ".claude-review.yml")] = "product: true\n"
    _use_github(gh)
    assert [r["repository_id"] for r in client.get("/repo-docs/products").json()["items"]] == [rid]


def test_products_list_skips_repo_on_github_failure_with_ops_event(env):
    client, db, _ = env
    rid, gh = _product_repo(db)
    gh.file_errors.add(".claude-review.yml")
    _use_github(gh)
    assert client.get("/repo-docs/products").json()["items"] == []
    with db.session() as s:
        events = [(e.event, e.detail["step"]) for e in s.query(OpsEvent).all()]
    assert events == [("product_catalog_degraded", "config")]


# --- product repos in the docs tree -------------------------------------------

def test_tree_lists_root_addon_docs_for_product_repo(env):
    client, db, _ = env
    rid, gh = _product_repo(db)
    _use_github(gh)
    body = client.get(f"/repo-docs/repos/{rid}/tree?ref=19.0").json()
    assert [e["path"] for e in body["entries"]] == [
        "cu_helpdesk_sla/README.md", "cu_helpdesk_sla/docs/guide.html",
    ]


def test_tree_keeps_old_scope_for_non_product_repo(env):
    client, db, _ = env
    rid, gh = _product_repo(db)
    gh.files[".claude-review.yml"] = "max_diff_lines: 5\n"
    gh.branches = [{"name": "main", "sha": "m"}]   # no version-branch fallback either
    _use_github(gh)
    assert client.get(f"/repo-docs/repos/{rid}/tree?ref=19.0").json()["entries"] == []


def test_file_serves_addon_html_doc_for_product_repo(env):
    client, db, _ = env
    rid, gh = _product_repo(db)
    gh.files["cu_helpdesk_sla/docs/guide.html"] = "<h1>Guide</h1>"
    _use_github(gh)
    r = client.get(f"/repo-docs/repos/{rid}/file?path=cu_helpdesk_sla/docs/guide.html&ref=19.0")
    assert r.status_code == 200 and r.json()["content"] == "<h1>Guide</h1>"
```

Run: `cd api && .venv/bin/python -m pytest tests/test_docs_products.py -q`
Expected: FAIL (404 for `/repo-docs/products`, empty tree entries, 415 on the html file).

- [ ] **Step 4: Caches and schemas**

`api/app/doc_cache.py`, after `titles_cache`:

```python
# Product page (Internal modules). The flag gates both the product list and
# the widened docs-tree scope; a detail is one repo's whole section.
product_flag_cache = TTLCache(ttl=300)
products_cache = TTLCache(ttl=300)
```

and add both to `clear_all()`.

`api/app/schemas/docs.py`, append:

```python
# --- product page (Internal modules) -------------------------------------------


class ProductRepoRef(BaseModel):
    repository_id: int
    full_name: str
    owner: str
    name: str
    html_url: str


class ProductList(BaseModel):
    items: list[ProductRepoRef]
```

- [ ] **Step 5: Route helpers and the list endpoint**

In `api/app/routes/docs.py`:

Imports: add `ProductList` to the schemas import; `from app.doc_cache import ... product_flag_cache, products_cache`; `from reva.product_catalog import module_dirs, select_version_branches`; `from reva.repo_config import load_repo_config`; `import structlog` plus `logger = structlog.get_logger()` under `router = APIRouter()`.

Replace the body of `doc_branches` so it reads from a raw cache, and add the helpers right after `_meta_and_token`:

```python
def _cached_branches(github, repository_id, owner, name, token) -> list[dict]:
    """Raw `[{"name","sha"}]` for a repo, cached. Raises Permanent/Transient."""
    key = ("raw", repository_id)
    hit = branches_cache.get(key)
    if hit is not None:
        return hit
    branches = github.get_branches(token, owner, name)
    branches_cache.set(key, branches)
    return branches


def _product_flag(github, repository_id, meta, token) -> bool:
    """`product: true` in .claude-review.yml on the default branch, or on the
    highest version branch when the default branch does not say so (a product
    repo may keep `main` empty). Cached; raises Permanent/Transient."""
    hit = product_flag_cache.get(repository_id)
    if hit is not None:
        return hit
    owner, name = meta["owner"], meta["name"]
    flag = load_repo_config(github, token, owner, name, meta["default_branch"]).product
    if not flag:
        names = [b["name"] for b in _cached_branches(github, repository_id, owner, name, token)]
        read, _ = select_version_branches(names)
        if read:
            flag = load_repo_config(github, token, owner, name, read[0]).product
    product_flag_cache.set(repository_id, flag)
    return flag


def _degrade(db, repository_id: int, step: str, exc: Exception) -> str:
    """Log + ops event for a GitHub failure the product page works around;
    returns the warning line for the response."""
    logger.warning("product_catalog_degraded", repository_id=repository_id, step=step,
                   error=str(exc))
    writers.record_ops_event(
        db, "docs", "warning", "product_catalog_degraded",
        {"repository_id": repository_id, "step": step, "error": str(exc)[:200]},
    )
    return f"{step}: GitHub error, data may be incomplete"
```

`doc_branches` then becomes:

```python
    meta, token = _meta_and_token(db, github, repository_id)
    try:
        branches = _cached_branches(github, repository_id, meta["owner"], meta["name"], token)
    except PermanentError:
        raise HTTPException(status_code=404, detail="Repository branches not found")
    except TransientError:
        raise HTTPException(status_code=502, detail="Upstream GitHub error")
    default = meta["default_branch"]
    allowed = {"main", "dev", "test", default}
    items = [...]            # unchanged
    items.sort(...)          # unchanged
    return {"repository_id": repository_id, "default_branch": default, "items": items}
```

(drop its own `branches_cache.get/set` of the filtered result; the raw list is what is cached now).

`_cached_tree` gains `product: bool = False`, keys the cache on `(repository_id, ref, product)`, and computes addon roots before filtering:

```python
def _cached_tree(github, repository_id, owner, name, ref, token, product=False) -> dict:
    """{entries, truncated, addon_roots} for a repo+ref, cached. For a product
    repo the addon roots (top-level dirs with a manifest) widen the doc scope
    and feed the product page. Raises Permanent/Transient (caller maps)."""
    key = (repository_id, ref, product)
    hit = tree_cache.get(key)
    if hit is not None:
        return hit
    tree = github.get_tree(token, owner, name, ref)
    blobs = [e["path"] for e in tree.get("tree", []) if e.get("type") == "blob"]
    roots = tuple(module_dirs(blobs)) if product else ()
    sizes = {e["path"]: e.get("size") for e in tree.get("tree", []) if e.get("type") == "blob"}
    entries = sorted(
        ({"path": p, "size": sizes[p]} for p in blobs if browser_in_scope(p, roots)),
        key=lambda e: e["path"],
    )
    result = {"entries": entries, "truncated": bool(tree.get("truncated")),
              "addon_roots": list(roots)}
    tree_cache.set(key, result)
    return result
```

`doc_tree`, `doc_titles`, `doc_search`: before calling `_cached_tree`, resolve the flag inside the same `try` that maps GitHub errors:

```python
    try:
        product = _product_flag(github, repository_id, meta, token)
        result = _cached_tree(github, repository_id, meta["owner"], meta["name"], ref, token,
                              product=product)
```

`doc_file`: an HTML doc of a product repo lives under an addon root, which the plain `browser_in_scope(safe)` cannot know. Replace the 415 check with:

```python
    is_html = safe.lower().endswith(BROWSER_DOC_EXTENSIONS)
    meta, token = _meta_and_token(db, github, repository_id)
    ref = ref or meta["default_branch"]
    allowed = safe.lower().endswith(DOC_EXTENSIONS) or (is_html and browser_in_scope(safe))
    if not allowed and is_html:
        try:
            if _product_flag(github, repository_id, meta, token):
                tree = _cached_tree(github, repository_id, meta["owner"], meta["name"], ref,
                                    token, product=True)
                allowed = browser_in_scope(safe, tuple(tree["addon_roots"]))
        except (PermanentError, TransientError):
            allowed = False
    if not allowed:
        raise HTTPException(status_code=415, detail="Only doc files are served as text")
```

(the `_meta_and_token` call moves above the check; the rest of the function is unchanged).

The list endpoint, after `list_doc_repos`:

```python
@router.get("/products", response_model=ProductList)
def list_products(db: Database = Depends(get_db), github=Depends(get_github_client)) -> dict:
    """Enabled repos whose .claude-review.yml says `product: true` — the
    sections of the docs site's Internal modules page. One (cached) config
    read per repo; a repo GitHub cannot answer for is skipped with an ops
    event rather than failing the list."""
    items, _ = repo_q.list_repos(db)
    repos = [it for it in items if it["enabled"]]

    def flagged(it):
        # GitHub only in the pool; the ops-event write happens on the request
        # thread below (the test DB is a single shared SQLite connection).
        meta = {"owner": it["owner"], "name": it["name"],
                "default_branch": it["default_branch"] or "main"}
        try:
            token = github.get_installation_token(it["installation_id"])
            return _product_flag(github, it["id"], meta, token), None
        except (PermanentError, TransientError) as exc:
            return False, exc

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(flagged, repos))
    out = []
    for it, (flag, exc) in zip(repos, results):
        if exc is not None:
            _degrade(db, it["id"], "config", exc)
        elif flag:
            out.append({
                "repository_id": it["id"], "full_name": it["full_name"], "owner": it["owner"],
                "name": it["name"], "html_url": f"https://github.com/{it['owner']}/{it['name']}",
            })
    out.sort(key=lambda r: r["full_name"].lower())
    return {"items": out}
```

(`repo_q.list_repos` rows already carry `installation_id`, `owner`, `name`, `default_branch`, `enabled`.)

- [ ] **Step 6: Run the api suite**

Run: `cd api && .venv/bin/python -m pytest tests/test_docs_products.py tests/test_docs.py -v && cd .. && ruff check api/app reva`
Expected: PASS (6 new + all existing docs tests, including the unchanged `test_tree_returns_docs_in_scope_only` and branch tests), ruff clean.

- [ ] **Step 7: Stage**

```bash
git add reva/repo_docs.py worker/tests/test_repo_docs.py api/app/doc_cache.py api/app/schemas/docs.py api/app/routes/docs.py api/tests/test_docs_products.py
```

---

### Task 7: Product detail endpoint

**Files:**
- Modify: `api/app/schemas/docs.py` (detail models)
- Modify: `api/app/routes/docs.py` (`product_detail`)
- Test: `api/tests/test_docs_products.py` (append)

**Interfaces:**
- Consumes: Task 4 helpers, Task 5 client methods, Task 6 helpers.
- Produces: `GET /repo-docs/products/{repository_id}` → `ProductRepo` (spec shape).

- [ ] **Step 1: Write the failing tests**

Append to `api/tests/test_docs_products.py`:

```python
# --- GET /repo-docs/products/{id} ---------------------------------------------

def test_product_detail_modules_versions_and_issues(env):
    client, db, _ = env
    rid, gh = _product_repo(db, branches=("19.0", "18.0", "17.0", "16.0"))
    gh.files[("18.0", "product.yml")] = (
        "modules:\n  cu_helpdesk_sla:\n    owner: Markus\n    price: 1200\n"
        "  cu_helpdesk_kb:\n    status: planned\n    eta: Q1\n"
    )
    gh.issues = [
        {"number": 1, "title": "[feature] 6811 - holidays", "html_url": "u1", "state": "open",
         "created_at": "2026-10-01T00:00:00Z", "assignees": [{"login": "joseph"}],
         "milestone": {"title": "19.0.2"}, "closed_at": None, "state_reason": None},
        {"number": 2, "title": "[fix] 6754 - pivot", "html_url": "u2", "state": "open",
         "created_at": "2026-10-02T00:00:00Z", "assignees": [], "milestone": None,
         "closed_at": None, "state_reason": None},
        {"number": 3, "title": "done thing", "html_url": "u3", "state": "closed",
         "created_at": "2026-09-01T00:00:00Z", "assignees": [], "milestone": None,
         "closed_at": "2026-10-01T00:00:00Z", "state_reason": "completed"},
    ]
    gh.prs = [{"number": 9, "title": "Fix pivot, closes #2", "body": ""}]
    _use_github(gh)
    body = client.get(f"/repo-docs/products/{rid}").json()
    assert body["branches"] == ["19.0", "18.0", "17.0"]
    assert body["ignored_branches"] == ["16.0"]
    assert body["html_url"] == "https://github.com/Cloudunify/cu-helpdesk"
    assert body["loaded_at"]
    mods = {m["module"]: m for m in body["modules"]}
    sla = mods["cu_helpdesk_sla"]
    assert (sla["name"], sla["owner"], sla["price"], sla["tldr"]) == (
        "Helpdesk SLA", "J", 1200, "SLA timers",
    )
    assert sla["has_yml_entry"] is True
    assert sla["versions"]["19.0"]["version"] == "19.0.1.0.0"
    assert sla["versions"]["19.0"]["readme_path"] == "cu_helpdesk_sla/README.md"
    assert sla["versions"]["17.0"]["status"] == "available"
    kb = mods["cu_helpdesk_kb"]
    assert list(kb["versions"]) == ["18.0"]
    assert kb["versions"]["18.0"] == {"status": "planned", "version": None, "eta": "Q1",
                                      "note": None, "manifest_error": None, "readme_path": None}
    assert [(i["number"], i["state"]) for i in body["issues"]] == [
        (2, "in_progress"), (1, "in_progress"),
    ]
    assert body["issues"][0]["pr_number"] == 9
    assert body["issues"][1]["assignee"] == "joseph"
    assert body["issues"][1]["milestone"] == "19.0.2"
    assert body["warnings"] == ["`cu_helpdesk_sla`: owner differs between 19.0 and 18.0"]


def test_product_detail_404_for_non_product_repo(env):
    client, db, _ = env
    rid, gh = _product_repo(db)
    gh.files[".claude-review.yml"] = "odoo: true\n"
    gh.branches = [{"name": "main", "sha": "m"}]
    _use_github(gh)
    assert client.get(f"/repo-docs/products/{rid}").status_code == 404
    assert client.get("/repo-docs/products/9999").status_code == 404


def test_product_detail_degrades_per_step_with_ops_events(env):
    client, db, _ = env
    rid, gh = _product_repo(db, branches=("19.0", "18.0"))
    gh.tree_errors["18.0"] = TransientError("tree down")
    gh.file_errors.add("cu_helpdesk_sla/__manifest__.py")

    class _NoIssues(_ProductGitHub):
        def list_issues(self, *a, **kw):
            raise TransientError("issues down")

    gh.__class__ = _NoIssues
    _use_github(gh)
    body = client.get(f"/repo-docs/products/{rid}").json()
    assert body["branches"] == ["19.0", "18.0"]
    sla = body["modules"][0]
    assert sla["versions"]["19.0"]["manifest_error"]       # fetch failed -> unparsed
    assert "18.0" not in sla["versions"]                    # whole branch degraded
    assert body["issues"] == []
    assert len(body["warnings"]) == 3
    with db.session() as s:
        steps = sorted(e.detail["step"] for e in s.query(OpsEvent).all()
                       if e.event == "product_catalog_degraded")
    assert steps == ["issues", "manifests:19.0", "tree:18.0"]


def test_product_detail_warns_on_truncated_tree(env):
    client, db, _ = env
    rid, gh = _product_repo(db, branches=("19.0",))
    gh.trees["19.0"]["truncated"] = True
    _use_github(gh)
    body = client.get(f"/repo-docs/products/{rid}").json()
    assert any("truncated" in w for w in body["warnings"])


def test_product_detail_is_cached(env):
    client, db, _ = env
    rid, gh = _product_repo(db, branches=("19.0",))
    _use_github(gh)
    first = client.get(f"/repo-docs/products/{rid}").json()
    gh.issues = [{"number": 1, "title": "new", "html_url": "u", "state": "open",
                  "created_at": "2026-10-01T00:00:00Z", "assignees": [], "milestone": None,
                  "closed_at": None, "state_reason": None}]
    second = client.get(f"/repo-docs/products/{rid}").json()
    assert second == first
```

Run: `cd api && .venv/bin/python -m pytest tests/test_docs_products.py -k detail -q`
Expected: FAIL (404 on every detail call).

- [ ] **Step 2: Detail schemas**

Append to `api/app/schemas/docs.py`:

```python
class ModuleVersion(BaseModel):
    status: str                      # available | planned | discontinued
    version: str | None = None
    eta: str | None = None
    note: str | None = None
    manifest_error: str | None = None
    readme_path: str | None = None


class ProductModule(BaseModel):
    module: str
    name: str | None = None
    tldr: str | None = None
    owner: str | None = None
    price: str | int | float | None = None   # "free" | EUR amount
    features: list[str] = []
    has_yml_entry: bool
    versions: dict[str, ModuleVersion]


class RepoIssue(BaseModel):
    number: int
    title: str
    url: str
    state: str                        # in_progress | planned | wont_do
    assignee: str | None = None
    pr_number: int | None = None
    milestone: str | None = None
    created_at: str
    closed_at: str | None = None


class ProductRepo(ProductRepoRef):
    loaded_at: str
    branches: list[str]
    ignored_branches: list[str]
    modules: list[ProductModule]
    issues: list[RepoIssue]
    warnings: list[str]
```

- [ ] **Step 3: The detail route**

Add to the imports in `api/app/routes/docs.py`: `ProductRepo` (schemas), `from datetime import datetime, timezone`, and from `reva.product_catalog`: `WONT_DO_WINDOW, build_branch, classify_issues, merge_repo, parse_product_yml`.

After `list_products`:

```python
@router.get("/products/{repository_id}", response_model=ProductRepo)
def product_detail(
    repository_id: int,
    db: Database = Depends(get_db),
    github=Depends(get_github_client),
) -> dict:
    """One product repo's section: modules across its three highest version
    branches (manifests + product.yml + README presence) and its issues.
    Every GitHub failure degrades to a warning + ops event so one broken
    branch or a rate-limited issues call never blanks the section."""
    hit = products_cache.get(repository_id)
    if hit is not None:
        return hit
    meta, token = _meta_and_token(db, github, repository_id)
    owner, name = meta["owner"], meta["name"]
    try:
        if not _product_flag(github, repository_id, meta, token):
            raise HTTPException(status_code=404, detail="Not a product repo")
    except TransientError:
        raise HTTPException(status_code=502, detail="Upstream GitHub error")
    except PermanentError:
        raise HTTPException(status_code=404, detail="Repository not readable")

    now = datetime.now(timezone.utc)
    warnings: list[str] = []
    read: list[str] = []
    ignored: list[str] = []
    try:
        names = [b["name"] for b in _cached_branches(github, repository_id, owner, name, token)]
        read, ignored = select_version_branches(names)
    except (PermanentError, TransientError) as exc:
        warnings.append(_degrade(db, repository_id, "branches", exc))

    per_branch: list[tuple[str, dict]] = []
    for branch in read:
        try:
            tree = _cached_tree(github, repository_id, owner, name, branch, token, product=True)
        except (PermanentError, TransientError) as exc:
            warnings.append(_degrade(db, repository_id, f"tree:{branch}", exc))
            continue
        if tree["truncated"]:
            warnings.append(f"{branch}: GitHub truncated the file tree; modules may be missing")
        roots = tree["addon_roots"]
        readmes = {e["path"] for e in tree["entries"]
                   if e["path"].count("/") == 1 and e["path"].endswith("/README.md")}
        failed: list[str] = []

        def fetch(path, _branch=branch, _failed=failed):
            try:
                return _cached_file(github, repository_id, owner, name, path, _branch, token)
            except (PermanentError, TransientError):
                _failed.append(path)
                return None

        with ThreadPoolExecutor(max_workers=8) as pool:
            texts = list(pool.map(fetch, [f"{r}/__manifest__.py" for r in roots]))
        manifests = dict(zip(roots, texts))
        yml, yml_warnings = parse_product_yml(fetch("product.yml"))
        warnings.extend(f"{branch}: {w}" for w in yml_warnings)
        if failed:
            warnings.append(_degrade(db, repository_id, f"manifests:{branch}",
                                     TransientError(f"{len(failed)} file fetches failed")))
        rows, row_warnings = build_branch(branch, manifests, readmes, yml)
        warnings.extend(row_warnings)
        per_branch.append((branch, rows))

    modules, merge_warnings = merge_repo(per_branch)
    warnings.extend(merge_warnings)

    issues: list[dict] = []
    try:
        since = (now - WONT_DO_WINDOW).strftime("%Y-%m-%dT%H:%M:%SZ")
        raw = github.list_issues(token, owner, name, state="open")
        raw += github.list_issues(token, owner, name, state="closed", since=since)
        prs = github.list_open_pull_requests(token, owner, name)
        issues = classify_issues(raw, prs, now)
    except (PermanentError, TransientError) as exc:
        warnings.append(_degrade(db, repository_id, "issues", exc))

    result = {
        "repository_id": repository_id,
        "full_name": f"{owner}/{name}",
        "owner": owner,
        "name": name,
        "html_url": f"https://github.com/{owner}/{name}",
        "loaded_at": now.isoformat(),
        "branches": read,
        "ignored_branches": ignored,
        "modules": modules,
        "issues": issues,
        "warnings": warnings,
    }
    products_cache.set(repository_id, result)
    return result
```

Note on the `manifests:{branch}` degrade: `_cached_file` failures (manifests and `product.yml`) are summarised once per branch (one ops event), not once per file.

- [ ] **Step 4: Run the suite**

Run: `cd api && .venv/bin/python -m pytest tests/ -q && cd .. && ruff check api/app reva`
Expected: all PASS, ruff clean. If `test_product_detail_degrades_per_step_with_ops_events` reports a different warning count, read the three sources (tree, manifests, issues) before touching the assertion: the count is the contract.

- [ ] **Step 5: Stage**

```bash
git add api/app/schemas/docs.py api/app/routes/docs.py api/tests/test_docs_products.py
```

---

### Task 8: The Internal modules page in the SPA

**Files:**
- Modify: `docs-ui/src/api.js` (two calls)
- Modify: `docs-ui/src/components/InternalModules.vue` (replace the placeholder)
- Modify: `docs-ui/src/style.css` (append a `/* --- Internal modules --- */` block)
- Verify: `cd docs-ui && npm run build`

**Interfaces:**
- Consumes: `GET /repo-docs/products`, `GET /repo-docs/products/{id}`, existing `getFile`, `renderMarkdown`, `navigate`, `ui`/`toggleOpen`.

- [ ] **Step 1: Client calls**

Append to `docs-ui/src/api.js`:

```js
// Internal modules page: product repos, then one detail per repo.
export const listProducts = () => getJSON(`${BASE}/products`)
export const getProduct = (repoId) => getJSON(`${BASE}/products/${repoId}`)
```

- [ ] **Step 2: Replace `InternalModules.vue`**

```vue
<script setup>
// Internal modules: every sellable Cloudunify addon, read live from the product
// repos (spec: docs/superpowers/specs/2026-10-07-product-modules-overview-design.md).
// The list is one cheap call; each repo's detail loads in parallel and renders
// as it arrives, so one slow repo never blocks the page.
import { ref, reactive, onMounted } from 'vue'
import * as api from '../api.js'
import { renderMarkdown } from '../markdown.js'
import { navigate } from '../location.js'
import { ui, toggleOpen } from '../persist.js'

const repos = ref([])        // ProductRepoRef[]
const loading = ref(true)
const error = ref('')
const details = reactive({}) // repoId -> { data, loading, error }
const readmes = reactive({}) // 'm:<repoId>:<module>' -> { branch, path, html, loading, error, none }

onMounted(async () => {
  try {
    const list = await api.listProducts()
    repos.value = list.items
    for (const r of list.items) loadDetail(r.repository_id)
  } catch (e) {
    error.value = String(e.message || e)
  } finally {
    loading.value = false
  }
})

async function loadDetail(repoId) {
  details[repoId] = { data: null, loading: true, error: '' }
  try {
    details[repoId] = { data: await api.getProduct(repoId), loading: false, error: '' }
  } catch (e) {
    details[repoId] = { data: null, loading: false, error: String(e.message || e) }
  }
}

const key = (repo, m) => `m:${repo.repository_id}:${m.module}`
const isOpen = (repo, m) => !!ui.open[key(repo, m)]
function toggle(repo, m) {
  toggleOpen(key(repo, m))
  if (isOpen(repo, m)) loadReadme(repo, m)
}

// The README shown inline comes from the highest branch that has one.
function readmeSource(repo, m) {
  for (const b of repo.branches) {
    const v = m.versions[b]
    if (v?.readme_path) return { branch: b, path: v.readme_path }
  }
  return null
}

async function loadReadme(repo, m) {
  const k = key(repo, m)
  if (readmes[k]) return
  const src = readmeSource(repo, m)
  if (!src) {
    readmes[k] = { none: true, html: '', loading: false, error: '' }
    return
  }
  readmes[k] = { ...src, html: '', loading: true, error: '' }
  try {
    const data = await api.getFile(repo.repository_id, src.path, src.branch)
    const result = renderMarkdown(data.content, {
      repoId: repo.repository_id,
      path: src.path,
      owner: repo.owner,
      name: repo.name,
      branch: src.branch,
    })
    readmes[k] = { ...src, html: result.html, loading: false, error: '' }
  } catch (e) {
    readmes[k] = { ...src, html: '', loading: false, error: String(e.message || e) }
  }
}

// In-repo doc links inside a rendered README open in the doc view.
function onReadmeClick(ev, repo, m) {
  const a = ev.target.closest('a')
  const docPath = a?.getAttribute('data-doc-path')
  if (!docPath) return
  ev.preventDefault()
  navigate(repo.repository_id, docPath, readmes[key(repo, m)]?.branch)
}

const price = (p) =>
  p == null ? '—' : p === 'free' ? 'free' : `€ ${Number(p).toLocaleString('de-AT')}`
const allDiscontinued = (repo, m) =>
  Object.keys(m.versions).length > 0 &&
  Object.values(m.versions).every((v) => v.status === 'discontinued')
const discontinuedNote = (m) =>
  Object.values(m.versions).find((v) => v.status === 'discontinued')?.note
const stateLabel = { in_progress: 'in progress', planned: 'planned', wont_do: "won't do" }
const loadedAt = (iso) =>
  new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
const age = (iso) => {
  const days = Math.floor((Date.now() - new Date(iso)) / 86400000)
  return days < 1 ? 'today' : days === 1 ? '1 day ago' : days < 30 ? `${days} days ago`
    : days < 60 ? '1 month ago' : `${Math.floor(days / 30)} months ago`
}
</script>

<template>
  <article class="doc products">
    <div class="markdown-body">
      <h1>Internal modules</h1>
      <p class="lede">
        Every Cloudunify addon we sell, read live from the product repos: one row per module,
        one column per Odoo version branch (the three newest, no backports). Owner, price,
        summary, feature set and status exceptions come from each branch's
        <code>product.yml</code>; versions from the manifests; open issues from GitHub.
      </p>
    </div>

    <p v-if="loading" class="muted">Loading…</p>
    <p v-else-if="error" class="error">{{ error }}</p>
    <p v-else-if="!repos.length" class="muted">
      No product repos yet. A repo joins this page with <code>product: true</code> in its
      <code>.claude-review.yml</code>.
    </p>

    <section v-for="r in repos" :key="r.repository_id" class="product">
      <div class="product-head">
        <h2>{{ r.name }}</h2>
        <span class="meta">
          {{ r.full_name }}
          <template v-if="details[r.repository_id]?.data">
            · branches {{ details[r.repository_id].data.branches.join(', ') || 'none' }}
            <template v-if="details[r.repository_id].data.ignored_branches.length">
              ({{ details[r.repository_id].data.ignored_branches.join(', ') }} ignored)
            </template>
            · loaded {{ loadedAt(details[r.repository_id].data.loaded_at) }}
          </template>
          · <a :href="r.html_url" target="_blank" rel="noopener noreferrer">GitHub ↗</a>
        </span>
      </div>

      <p v-if="details[r.repository_id]?.loading" class="muted">Loading…</p>
      <p v-else-if="details[r.repository_id]?.error" class="error">
        {{ details[r.repository_id].error }}
      </p>
      <template v-else-if="details[r.repository_id]?.data">
        <p
          v-for="w in details[r.repository_id].data.warnings"
          :key="w"
          class="warning"
        >{{ w }}</p>

        <p v-if="!details[r.repository_id].data.modules.length" class="muted">
          No modules found on the version branches.
        </p>
        <div v-else class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Module</th>
                <th>Summary</th>
                <th>Owner</th>
                <th class="price">Price (one-time, net EUR)</th>
                <th v-for="b in details[r.repository_id].data.branches" :key="b" class="ver">
                  {{ b }}
                </th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              <template v-for="m in details[r.repository_id].data.modules" :key="m.module">
                <tr
                  class="mod"
                  :class="{ off: allDiscontinued(details[r.repository_id].data, m), noyml: !m.has_yml_entry }"
                >
                  <td class="name">
                    {{ m.name || m.module }}
                    <span class="tech">{{ m.module }}</span>
                    <span v-if="!m.has_yml_entry" class="tech">no product.yml entry</span>
                  </td>
                  <td class="tldr">
                    <template v-if="allDiscontinued(details[r.repository_id].data, m)">
                      Discontinued<span v-if="discontinuedNote(m)">: {{ discontinuedNote(m) }}</span>
                    </template>
                    <template v-else>{{ m.tldr || '—' }}</template>
                  </td>
                  <td class="owner">{{ m.owner || '—' }}</td>
                  <td class="price" :class="{ free: m.price === 'free' }">{{ price(m.price) }}</td>
                  <td v-for="b in details[r.repository_id].data.branches" :key="b" class="ver">
                    <template v-if="!m.versions[b]"><span class="absent">—</span></template>
                    <template v-else-if="m.versions[b].status === 'available'">
                      <a
                        v-if="m.versions[b].readme_path"
                        href="#"
                        :title="`README on ${b}`"
                        @click.prevent="navigate(r.repository_id, m.versions[b].readme_path, b)"
                      >{{ m.versions[b].version || 'no version' }}</a>
                      <span v-else>{{ m.versions[b].version || 'no version' }}</span>
                      <span v-if="m.versions[b].manifest_error" class="eta">{{ m.versions[b].manifest_error }}</span>
                    </template>
                    <template v-else>
                      <span class="pill" :class="m.versions[b].status">{{ m.versions[b].status }}</span>
                      <span v-if="m.versions[b].eta" class="eta">eta {{ m.versions[b].eta }}</span>
                      <span v-else-if="m.versions[b].note" class="eta">{{ m.versions[b].note }}</span>
                    </template>
                  </td>
                  <td class="toggle">
                    <button
                      class="feat"
                      type="button"
                      :aria-expanded="isOpen(r, m)"
                      :aria-label="isOpen(r, m) ? 'Hide details' : 'Show details'"
                      @click="toggle(r, m)"
                    >{{ isOpen(r, m) ? '−' : '+' }}</button>
                  </td>
                </tr>
                <tr v-if="isOpen(r, m)" class="features">
                  <td :colspan="5 + details[r.repository_id].data.branches.length">
                    <div class="label">Features</div>
                    <ul v-if="m.features.length"><li v-for="f in m.features" :key="f">{{ f }}</li></ul>
                    <p v-else class="muted">No features listed in product.yml.</p>
                    <div class="readme" v-if="readmes[key(r, m)]">
                      <div class="label">
                        README<template v-if="readmes[key(r, m)].branch"> · {{ readmes[key(r, m)].branch }}</template>
                        <a
                          v-if="readmes[key(r, m)].path"
                          href="#"
                          @click.prevent="navigate(r.repository_id, readmes[key(r, m)].path, readmes[key(r, m)].branch)"
                        >open in docs ↗</a>
                      </div>
                      <p v-if="readmes[key(r, m)].none" class="muted">No README on any version branch.</p>
                      <p v-else-if="readmes[key(r, m)].loading" class="muted">Loading README…</p>
                      <p v-else-if="readmes[key(r, m)].error" class="error">{{ readmes[key(r, m)].error }}</p>
                      <!-- html is DOMPurify-sanitized in renderMarkdown before it reaches v-html -->
                      <div v-else class="markdown-body" v-html="readmes[key(r, m)].html" @click="onReadmeClick($event, r, m)"></div>
                    </div>
                  </td>
                </tr>
              </template>
            </tbody>
          </table>
        </div>

        <div class="future">
          <h3>Open issues</h3>
          <p v-if="!details[r.repository_id].data.issues.length" class="empty">No open issues.</p>
          <ul v-else>
            <li
              v-for="i in details[r.repository_id].data.issues"
              :key="i.number"
              :class="i.state"
            >
              <span class="num">#{{ i.number }}</span>
              <span class="chip" :class="i.state">{{ stateLabel[i.state] }}</span>
              <a :href="i.url" target="_blank" rel="noopener noreferrer">{{ i.title }}</a>
              <span v-if="i.milestone" class="ms">{{ i.milestone }}</span>
              <span class="age">
                <template v-if="i.state === 'wont_do'">closed {{ age(i.closed_at) }}</template>
                <template v-else-if="i.pr_number">PR #{{ i.pr_number }} open</template>
                <template v-else-if="i.assignee">assigned to {{ i.assignee }}</template>
                <template v-else>{{ age(i.created_at) }}</template>
              </span>
            </li>
          </ul>
        </div>
      </template>
    </section>
  </article>
</template>
```

- [ ] **Step 3: Styles**

Append to `docs-ui/src/style.css` (tokens already exist at the top of the file):

```css
/* --- Internal modules (product page) --- */
.products { max-width: 1040px; }
.products .lede { color: var(--text-muted); font-size: 14px; }
.product { margin-bottom: 40px; }
.product-head { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; margin-bottom: 4px; }
.product-head h2 { font-size: 1.25em; color: var(--heading); margin: 0; font-weight: 600; }
.product-head .meta { color: var(--text-faint); font-size: 12.5px; }
.product-head .meta a { color: var(--text-muted); }
.product .warning { color: var(--warn); font-size: 12.5px; margin: 2px 0 6px; }
.product .warning::before { content: "⚠ "; }
.product .table-wrap { overflow-x: auto; border: 1px solid var(--border); border-radius: 8px; margin-top: 10px; }
.product table { border-collapse: collapse; width: 100%; font-size: 13.5px; }
.product th, .product td { text-align: left; padding: 9px 12px; border-bottom: 1px solid var(--border); vertical-align: top; }
.product th { font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-faint); font-weight: 600; background: var(--bg-sidebar); white-space: nowrap; }
.product tbody tr:last-child td { border-bottom: 0; }
.product tr.mod:hover td { background: rgba(255, 255, 255, 0.025); }
.product td.name { min-width: 180px; }
.product td.name .tech { display: block; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 11.5px; color: var(--text-faint); margin-top: 2px; }
.product td.tldr { min-width: 220px; max-width: 360px; line-height: 1.5; }
.product td.owner { white-space: nowrap; color: var(--text-muted); }
.product th.price, .product td.price { text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }
.product td.price.free { color: #6cc07a; }
.product th.ver, .product td.ver { text-align: center; white-space: nowrap; font-variant-numeric: tabular-nums; }
.product td.ver a { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12.5px; }
.product .pill { display: inline-block; font-size: 11px; padding: 1px 7px; border-radius: 999px; border: 1px solid var(--border-strong); color: var(--text-muted); }
.product .pill.planned { color: var(--accent); border-color: rgba(95, 168, 234, 0.45); }
.product .pill.discontinued { color: var(--text-faint); text-decoration: line-through; }
.product .eta { display: block; font-size: 11px; color: var(--text-faint); margin-top: 3px; }
.product .absent { color: var(--text-faint); }
.product tr.off td, .product tr.noyml td { color: var(--text-faint); }
.product td.toggle { width: 32px; text-align: center; padding-left: 6px; padding-right: 6px; }
.product button.feat { background: none; border: 1px solid var(--border-strong); color: var(--text-muted); border-radius: 5px; font-size: 11px; width: 22px; height: 22px; cursor: pointer; line-height: 1; }
.product button.feat:hover, .product button.feat:focus-visible { border-color: var(--accent); color: var(--text-normal); outline: none; }
.product tr.features td { background: var(--bg-code); padding: 10px 12px 12px 44px; }
.product tr.features ul { margin: 0; padding-left: 18px; line-height: 1.6; font-size: 13px; columns: 2; column-gap: 32px; }
.product tr.features li { break-inside: avoid; }
.product tr.features .label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-faint); margin-bottom: 4px; display: flex; gap: 10px; align-items: baseline; }
.product tr.features .label a { text-transform: none; letter-spacing: 0; font-size: 12px; margin-left: auto; }
.product .readme { margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--border); max-width: 72ch; }
.product .readme .markdown-body { font-size: 13.5px; }
.product .future { margin-top: 18px; }
.product .future h3 { font-size: 12px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-faint); margin: 0 0 6px; font-weight: 600; }
.product .future ul { list-style: none; margin: 0; padding: 0; }
.product .future li { padding: 5px 0; border-bottom: 1px dashed var(--border); display: flex; gap: 10px; align-items: baseline; font-size: 13.5px; flex-wrap: wrap; }
.product .future li:last-child { border-bottom: 0; }
.product .future li.wont_do a { color: var(--text-faint); text-decoration: line-through; }
.product .future .num { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; color: var(--text-faint); min-width: 36px; }
.product .future .ms { color: var(--text-faint); font-size: 12px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.product .future .age { margin-left: auto; color: var(--text-faint); font-size: 12px; white-space: nowrap; }
.product .future .empty { color: var(--text-faint); font-size: 13px; }
.product .chip { font-size: 10.5px; padding: 1px 6px; border-radius: 4px; border: 1px solid var(--border-strong); color: var(--text-muted); white-space: nowrap; text-transform: uppercase; letter-spacing: 0.04em; }
.product .chip.in_progress { color: #6cc07a; border-color: rgba(108, 192, 122, 0.45); }
.product .chip.wont_do { color: var(--text-faint); border-style: dashed; }
@media (max-width: 768px) {
  .product tr.features ul { columns: 1; }
}
```

- [ ] **Step 4: Build**

Run: `cd docs-ui && npm install && npm run build`
Expected: `vite build` completes with no errors or Vue compiler warnings. A warning about an unused variable or a template typo is a failure here.

- [ ] **Step 5: Browser check against the dev stack (owed if skipped)**

With `make dev` running and `cd docs-ui && npm run dev`, register one product repo (or point `.claude-review.yml` of a scratch repo with `product: true` and a `19.0` branch at the dev stack). Check: the section appears with its branches; a version cell links to the README on that branch (`?repo=&path=&ref=19.0`); the toggle opens features and the inline README with an image served through `/raw`; "open in docs" lands in the doc view; an issue line links to GitHub. Write down in the handoff which of these were checked.

- [ ] **Step 6: Stage**

```bash
git add docs-ui/src/api.js docs-ui/src/components/InternalModules.vue docs-ui/src/style.css
```

---

### Task 9: Docs, handoff, and archiving

**Files:**
- Modify: `docs-ui/README.md` (Features list)
- Modify: `CLAUDE.md` (the `.claude-review.yml` keys sentence under Scope filtering: add `product`)
- Modify: every doc that lists the `.claude-review.yml` keys (`grep -rln "review_all_paths" README.md docs/*.md` and add `product` where `review_all_paths` is listed)
- Modify: `HANDOFF.md` (new addendum at the top)
- Move: spec and this plan into `docs/superpowers/specs/archive/` and `docs/superpowers/plans/archive/`

- [ ] **Step 1: docs-ui README**

Add to the Features list in `docs-ui/README.md`:

```markdown
- **Internal modules** page (`?page=internal-modules`): every repo whose
  `.claude-review.yml` says `product: true`, one table per repo with a row
  per root-level addon and a column per Odoo version branch (the three
  highest `NN.0` branches). Owner, price, summary, features and the
  `planned` / `discontinued` exceptions come from a `product.yml` at the
  branch root; name and version from the manifest; the README renders
  inline on expand; the repo's issues list below (in progress / planned /
  won't do). Backed by `GET /repo-docs/products` and
  `/repo-docs/products/{id}` (5-minute caches). Product repos also get their
  root-level addon docs listed in the tree.
```

Add a `product.yml` example (the one from the spec's "Repo conventions" section) under a new `### product.yml` heading in that README.

- [ ] **Step 2: Config key lists**

In `CLAUDE.md`, the sentence "Per-repo overrides in `.claude-review.yml` (`max_diff_lines`, …, `odoo_instance`)": add `product` after `review_all_paths`. Do the same in every file `grep -rln "review_all_paths" README.md docs/*.md` returns, one line each: `product` (bool, default false) — marks a sellable-addons repo: listed on the docs site's Internal modules page and implies `review_all_paths`.

- [ ] **Step 3: HANDOFF addendum**

Insert at the top of `HANDOFF.md`, above the current first addendum:

```markdown
## Addendum 2026-10-07 — Internal modules page lists the product repos

**Status: implemented, not deployed** (spec
`docs/superpowers/specs/archive/2026-10-07-product-modules-overview-design.md`,
plan `docs/superpowers/plans/archive/2026-10-07-product-modules-overview.md`).
The docs site's "Internal modules" placeholder is now a live overview of the
repos whose `.claude-review.yml` says `product: true` (which also implies
`review_all_paths`, since product repos keep their addons at the root). Per
repo the api reads the three highest `NN.0` branches — manifests,
`product.yml` (owner, price, tldr, features, `planned`/`discontinued`
exceptions), README presence — plus the repo's issues (state from assignee /
linked PR / close reason, milestone as ETA). New `GET /repo-docs/products` and
`/repo-docs/products/{id}` (`api/app/routes/docs.py`), pure rules in
`reva/product_catalog.py`, the config loader moved to `reva/repo_config.py`,
two new GitHub client list calls. Product repos also get root-level addon
docs listed in the docs tree (`browser_in_scope(path, addon_roots)`); the
grounding scope is unchanged. No migration, no TUI change.

**Deploy:** api, worker and scheduler images (shared `reva/`), nginx (SPA).
No new Access prefix: `/repo-docs` is already gated.

**Verified / owed:** unit-tested (worker + api suites). Browser check: <fill
in from Task 8 Step 5: done, or which points are owed>. No product repo
exists yet, so the page shows the empty-state text until one sets the flag.
Deferred (spec "Explicitly deferred"): `custom_addons/` assumptions in
change-note module naming, the skill prompts and ticket grounding must be
revisited when the first product repo is onboarded.
```

- [ ] **Step 4: Archive spec and plan**

```bash
git mv docs/superpowers/specs/2026-10-07-product-modules-overview-design.md docs/superpowers/specs/archive/
git mv docs/superpowers/plans/2026-10-07-product-modules-overview.md docs/superpowers/plans/archive/
```

Update the spec's `Status:` line to `implemented 2026-10-07, not deployed` and the plan header's `**Spec:**` path to the archive location.

- [ ] **Step 5: Full definition of done**

Run:

```bash
make test
ruff check reva worker/worker api/app scheduler/scheduler
cd docs-ui && npm run build
```

Expected: all three services green, ruff clean, build clean. Report every outcome verbatim in the task summary, including anything only unit-tested.

- [ ] **Step 6: Stage**

```bash
git add docs-ui/README.md CLAUDE.md README.md docs/ HANDOFF.md
git status   # everything from Tasks 1-9 staged; Joseph commits
```
