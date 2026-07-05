# Odoo↔REVA Contract Tests Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Machine-checkable Odoo↔REVA contracts: Pydantic payload models used at runtime, generated `contracts/` artifacts (schemas + samples + path manifest), drift/coverage tests, and a sync script for the ast-odoo repo — so path/shape drift fails tests instead of production.

**Architecture:** `reva/odoo_contracts.py` holds the four callback payload models + a declarative `CONTRACTS` table (name/direction/method/path/auth per contract, inbound entries referencing the existing FastAPI request models). `OdooCallbackClient` builds bodies through the models. A generator CLI writes `contracts/`; drift tests keep it committed-and-current; a coverage test forces every future callback method to publish its contract.

**Tech Stack:** Python 3.14, Pydantic v2 (`model_json_schema`), stdlib. **No new dependencies.**

**Spec:** `docs/superpowers/specs/2026-07-05-odoo-contract-tests-design.md`.

## Global Constraints

- Wire compatibility is sacred: after Task 2, every callback must POST **byte-identical JSON** to today's (asserted by tests). `IssueRefPayload` deliberately strips undocumented extra keys — Task 2 Step 1 verifies the runner already sends only the documented four.
- `contracts/` is generated-and-committed; regenerating must be deterministic (sorted keys, fixed separators, `\n` endings) or the drift test flaps.
- All tests offline/pure (worker suite). Final gate: `make test` + `ruff check reva worker/worker api/app scheduler/scheduler`.
- The ast-odoo tasks (Addendum A) are executed in that repo, after the REVA side lands and one sync has been committed there.

---

### Task 1: Payload models + CONTRACTS table + samples

**Files:**
- Create: `reva/odoo_contracts.py`
- Test: `worker/tests/test_odoo_contracts.py`

**Interfaces:**
- Produces:
  - Models: `WriteFieldPayload(ticket_id, model_name, field_name, html)`, `ResetStatusPayload(ticket_id, model_name)`, `IssueRefPayload(number, title, url, state)`, `IssuesCreatedPayload(ticket_id, model_name, request_id, status, issues, error)`, `IssueStatePayload(ticket_id, model_name, number, state, issues)`
  - `CONTRACTS: list[Contract]` (dataclass: `name, direction, method, path, auth, model | None, sample: dict`) — the single source for the manifest; inbound entries reference `TicketAnalysisRequest`, `CreateIssuesRequest`, and the health/error shapes.

- [ ] **Step 1: Write the failing tests**

Create `worker/tests/test_odoo_contracts.py`:

```python
"""Contract payload models: wire-identical to today's dicts; samples validate."""

from __future__ import annotations

import pytest

from reva.odoo_contracts import (
    CONTRACTS,
    IssueRefPayload,
    IssuesCreatedPayload,
    IssueStatePayload,
    ResetStatusPayload,
    WriteFieldPayload,
)


def test_write_field_wire_shape():
    p = WriteFieldPayload(ticket_id=123, model_name="helpdesk.ticket",
                          field_name="description", html="<h2>x</h2>")
    assert p.model_dump() == {
        "ticket_id": 123, "model_name": "helpdesk.ticket",
        "field_name": "description", "html": "<h2>x</h2>",
    }


def test_reset_status_wire_shape():
    assert ResetStatusPayload(ticket_id=1, model_name="project.task").model_dump() == {
        "ticket_id": 1, "model_name": "project.task",
    }


def test_issues_created_wire_shape_and_extra_keys_stripped():
    # The documented contract is {"number","title","url","state"} per issue;
    # richer internal dicts (id/attached/body…) must be stripped, not sent.
    p = IssuesCreatedPayload(
        ticket_id=1, model_name="helpdesk.ticket", request_id=7,
        status="created",
        issues=[{"number": 42, "title": "t",
                 "url": "https://github.com/o/r/issues/42", "state": "open",
                 "id": 900042, "attached": True}],
        error=None,
    )
    dumped = p.model_dump()
    assert dumped["issues"] == [{"number": 42, "title": "t",
                                 "url": "https://github.com/o/r/issues/42",
                                 "state": "open"}]
    assert dumped["error"] is None


def test_issue_state_wire_shape():
    p = IssueStatePayload(ticket_id=1, model_name="helpdesk.ticket",
                          number=42, state="closed",
                          issues=[{"number": 42, "title": "t", "url": None,
                                   "state": "closed"}])
    assert set(p.model_dump()) == {"ticket_id", "model_name", "number",
                                   "state", "issues"}


def test_contracts_table_complete_and_sane():
    names = {c.name for c in CONTRACTS}
    assert {"tickets.write-field", "tickets.reset-status",
            "tickets.issues-created", "tickets.issue-state",
            "ticket-analysis", "create-issues",
            "health.response", "error"} <= names
    for c in CONTRACTS:
        assert c.direction in ("reva->odoo", "odoo->reva")
        assert c.path.startswith("/")
        assert c.sample is not None


def test_callback_paths_are_ticket_namespaced():
    by_name = {c.name: c for c in CONTRACTS}
    assert by_name["tickets.write-field"].path == "/tickets/write-field"
    assert by_name["tickets.issue-state"].path == "/tickets/issue-state"


def test_every_sample_validates_against_its_model():
    for c in CONTRACTS:
        if c.model is not None:
            c.model.model_validate(c.sample)  # raises on drift


def test_failed_issues_created_sample_exists():
    # Both status shapes must be covered by samples (spec §3.3).
    ic = [c for c in CONTRACTS if c.name == "tickets.issues-created"]
    assert any(s.get("status") == "failed" for c in ic for s in c.extra_samples + [c.sample])
```

- [ ] **Step 2: Run to verify failure**

Run: `cd worker && .venv/bin/python -m pytest tests/test_odoo_contracts.py -q`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement `reva/odoo_contracts.py`**

```python
"""Odoo↔REVA contract source of truth (contract-tests spec).

The payload models here are USED AT RUNTIME by OdooCallbackClient — code and
contract cannot diverge. The CONTRACTS table (name/direction/method/path/
auth/sample) is the single source for the generated contracts/ artifacts;
paths live here and nowhere else. Adding a new callback method without a
CONTRACTS entry fails the coverage drift test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict


class WriteFieldPayload(BaseModel):
    ticket_id: int
    model_name: str
    field_name: str
    html: str


class ResetStatusPayload(BaseModel):
    ticket_id: int
    model_name: str


class IssueRefPayload(BaseModel):
    """The documented per-issue shape. Extra internal keys (id/attached/…)
    are ignored on input and therefore never serialized — enforcing the
    docstring contract mechanically."""

    model_config = ConfigDict(extra="ignore")

    number: int | None = None
    title: str = ""
    url: str | None = None
    state: str | None = None


class IssuesCreatedPayload(BaseModel):
    ticket_id: int
    model_name: str
    request_id: int
    status: str            # exactly "created" | "failed"
    issues: list[IssueRefPayload]
    error: str | None = None


class IssueStatePayload(BaseModel):
    ticket_id: int
    model_name: str
    number: int
    state: str
    issues: list[IssueRefPayload]


@dataclass(frozen=True)
class Contract:
    name: str
    direction: str          # "reva->odoo" | "odoo->reva"
    method: str
    path: str
    auth: str
    sample: dict
    model: type[BaseModel] | None = None      # None: shape-only (error bodies)
    extra_samples: list[dict] = field(default_factory=list)


def _inbound_models() -> dict[str, type[BaseModel]]:
    """Resolve the FastAPI request models the inbound contracts mirror.

    `app.*` is only importable inside the api service dir, so the generator
    (a repo-root dev tool) adds it to sys.path itself. Runtime callback code
    never calls this — only generation and the api-side contract tests do.
    """
    import sys
    from pathlib import Path

    api_dir = str(Path(__file__).resolve().parents[1] / "api")
    if api_dir not in sys.path:
        sys.path.insert(0, api_dir)
    from app.schemas.ticket_analyses import TicketAnalysisRequest
    from app.schemas.ticket_issues import CreateIssuesRequest

    return {"ticket-analysis": TicketAnalysisRequest,
            "create-issues": CreateIssuesRequest}


_ISSUE_SAMPLE = {"number": 42, "title": "Implement login form",
                 "url": "https://github.com/acme/widgets/issues/42",
                 "state": "open"}

CONTRACTS: list[Contract] = [
    Contract(
        name="tickets.write-field", direction="reva->odoo", method="POST",
        path="/tickets/write-field", auth="bearer:instance-outbound-key",
        model=WriteFieldPayload,
        sample={"ticket_id": 123, "model_name": "helpdesk.ticket",
                "field_name": "x_reva_analysis", "html": "<h2>Summary</h2>"},
    ),
    Contract(
        name="tickets.reset-status", direction="reva->odoo", method="POST",
        path="/tickets/reset-status", auth="bearer:instance-outbound-key",
        model=ResetStatusPayload,
        sample={"ticket_id": 123, "model_name": "helpdesk.ticket"},
    ),
    Contract(
        name="tickets.issues-created", direction="reva->odoo", method="POST",
        path="/tickets/issues-created", auth="bearer:instance-outbound-key",
        model=IssuesCreatedPayload,
        sample={"ticket_id": 123, "model_name": "helpdesk.ticket",
                "request_id": 7, "status": "created",
                "issues": [_ISSUE_SAMPLE], "error": None},
        extra_samples=[{"ticket_id": 123, "model_name": "helpdesk.ticket",
                        "request_id": 7, "status": "failed", "issues": [],
                        "error": "GitHub authentication failed"}],
    ),
    Contract(
        name="tickets.issue-state", direction="reva->odoo", method="POST",
        path="/tickets/issue-state", auth="bearer:instance-outbound-key",
        model=IssueStatePayload,
        sample={"ticket_id": 123, "model_name": "helpdesk.ticket",
                "number": 42, "state": "closed",
                "issues": [dict(_ISSUE_SAMPLE, state="closed")]},
    ),
    Contract(
        name="ticket-analysis", direction="odoo->reva", method="POST",
        path="/api/v1/ticket-analysis", auth="bearer:instance-inbound-key",
        model=None,  # resolved via _inbound_models() at generation/test time
        sample={"ticket_id": 42, "model_name": "helpdesk.ticket",
                "field_name": "x_reva_analysis",
                "text": "The login page is broken.", "attachment": None},
    ),
    Contract(
        name="create-issues", direction="odoo->reva", method="POST",
        path="/api/v1/create-issues", auth="bearer:instance-inbound-key",
        model=None,
        sample={"ticket_id": 42, "model_name": "project.task",
                "github_url": "https://github.com/acme/widgets",
                "name": "Login rework", "description": "Please add a login page.",
                "analysis_html": "", "description_docx": None,
                "priority": "1",
                "ticket_url": "https://odoo.example.com/web#id=42",
                "issue_type": None},
    ),
    Contract(
        name="health.response", direction="odoo->reva", method="GET",
        path="/api/v1/health", auth="bearer:any",
        model=None,
        sample={"status": "ok", "authenticated_as": "instance",
                "instance": "acme"},
    ),
    Contract(
        name="error", direction="odoo->reva", method="*",
        path="*", auth="*",
        model=None,
        sample={"detail": "Odoo instance daily budget reached"},
    ),
]
```

**Verification step baked in:** before finishing this task, confirm the
runner really sends only documented issue keys:
`grep -n "union" worker/worker/ticket_issue_runner.py | head` and read the
union construction — the docstring contract says `{"number","title","url",
"state"}`. If richer dicts reach `issues_created` today, the stripping in
`IssueRefPayload` is a *deliberate contract enforcement*; note it in the
commit message.

- [ ] **Step 4: Add the api-side inbound validation test**

The worker suite cannot import `app.*`, so the inbound samples validate in
the api suite. Create `api/tests/test_contracts_inbound.py`:

```python
"""Inbound contract samples must validate against the live FastAPI models."""

from __future__ import annotations

from app.schemas.ticket_analyses import TicketAnalysisRequest
from app.schemas.ticket_issues import CreateIssuesRequest
from reva.odoo_contracts import CONTRACTS

_MODELS = {"ticket-analysis": TicketAnalysisRequest,
           "create-issues": CreateIssuesRequest}


def test_inbound_samples_validate():
    for c in CONTRACTS:
        model = _MODELS.get(c.name)
        if model is None:
            continue
        model.model_validate(c.sample)
        for extra in c.extra_samples:
            model.model_validate(extra)


def test_all_inbound_request_contracts_covered():
    inbound = {c.name for c in CONTRACTS
               if c.direction == "odoo->reva" and c.method == "POST"}
    assert inbound == set(_MODELS), (
        "new inbound create route? add its model here AND a CONTRACTS entry"
    )
```

- [ ] **Step 5: Run to verify pass**

Run: `cd worker && .venv/bin/python -m pytest tests/test_odoo_contracts.py -q && cd ../api && .venv/bin/python -m pytest tests/test_contracts_inbound.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add reva/odoo_contracts.py worker/tests/test_odoo_contracts.py api/tests/test_contracts_inbound.py
git commit -m "feat(contracts): payload models + CONTRACTS table + samples"
```

---

### Task 2: `OdooCallbackClient` builds bodies through the models

**Files:**
- Modify: `reva/odoo_client.py` (four methods)
- Test: existing `worker/tests/test_odoo_client.py` must pass UNCHANGED (it asserts exact wire bodies — that's the regression gate)

- [ ] **Step 1: Rewrite the four bodies**

In `reva/odoo_client.py`, import the models:

```python
from reva.odoo_contracts import (
    IssuesCreatedPayload,
    IssueStatePayload,
    ResetStatusPayload,
    WriteFieldPayload,
)
```

and replace each inline dict:

```python
    def reset_status(self, ticket_id: int, model_name: str) -> None:
        """Set reva_status = pending in Odoo before re-running analysis."""
        self._post("/tickets/reset-status", ResetStatusPayload(
            ticket_id=ticket_id, model_name=model_name).model_dump())
```

```python
        self._post("/tickets/write-field", WriteFieldPayload(
            ticket_id=ticket_id, model_name=model_name,
            field_name=field_name, html=html).model_dump())
```

```python
        self._post("/tickets/issues-created", IssuesCreatedPayload(
            ticket_id=ticket_id, model_name=model_name,
            request_id=request_id, status=status,
            issues=issues, error=error).model_dump())
```

```python
        self._post("/tickets/issue-state", IssueStatePayload(
            ticket_id=ticket_id, model_name=model_name,
            number=number, state=state, issues=issues).model_dump())
```

(keep each method's docstring + logging lines untouched).

- [ ] **Step 2: Run the regression gate**

Run: `cd worker && .venv/bin/python -m pytest tests/test_odoo_client.py tests/test_ticket_runner.py tests/test_ticket_issue_runner.py -q`
Expected: PASS with **zero test edits**. If `test_issues_created_posts_contract_payload_to_sibling_path` fails on the body equality, the runner passed richer issue dicts than documented — inspect the diff: only undocumented keys (`id`, `attached`, `body`, `acceptance_criteria`, `type`) may disappear. Update that one test's expectation to the documented shape and say so in the commit message (contract enforcement, verified against the Odoo handler's documented reader fields).

- [ ] **Step 3: Commit**

```bash
git add reva/odoo_client.py worker/tests/test_odoo_client.py
git commit -m "refactor(odoo): callbacks build bodies via contract models"
```

---

### Task 3: Generator CLI + committed `contracts/`

**Files:**
- Modify: `reva/odoo_contracts.py` (append generator + `__main__`)
- Create: `contracts/` (generated output, committed), `contracts/README.md` (written by hand, not the generator)
- Test: `worker/tests/test_contracts_generator.py`

**Interfaces:**
- Produces: `generate(out_dir: Path) -> str` (returns `contracts_version` hash; deterministic output); `check(committed_dir: Path) -> list[str]` (differences, empty = current); CLI `python -m reva.odoo_contracts generate [--check]`.

- [ ] **Step 1: Write the failing tests**

Create `worker/tests/test_contracts_generator.py`:

```python
"""Deterministic contract generation + self-check."""

from __future__ import annotations

import json
from pathlib import Path

from reva.odoo_contracts import CONTRACTS, check, generate


def test_generate_writes_manifest_schema_sample(tmp_path):
    version = generate(tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["contracts_version"] == version
    names = {c["name"] for c in manifest["contracts"]}
    assert "tickets.write-field" in names and "ticket-analysis" in names
    entry = next(c for c in manifest["contracts"]
                 if c["name"] == "tickets.write-field")
    assert entry["path"] == "/tickets/write-field"
    schema = json.loads((tmp_path / entry["schema"]).read_text())
    assert schema["properties"].keys() >= {"ticket_id", "model_name",
                                           "field_name", "html"}
    sample = json.loads((tmp_path / entry["sample"]).read_text())
    assert sample["ticket_id"] == 123


def test_generate_is_deterministic(tmp_path):
    v1 = generate(tmp_path / "a")
    v2 = generate(tmp_path / "b")
    assert v1 == v2
    a = sorted(p.relative_to(tmp_path / "a") for p in (tmp_path / "a").rglob("*.json"))
    for rel in a:
        assert (tmp_path / "a" / rel).read_bytes() == (tmp_path / "b" / rel).read_bytes()


def test_check_flags_drift(tmp_path):
    generate(tmp_path)
    assert check(tmp_path) == []
    (tmp_path / "manifest.json").write_text("{}")
    assert check(tmp_path)  # non-empty differences


def test_every_contract_has_schema_or_shape(tmp_path):
    generate(tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert len(manifest["contracts"]) == len(CONTRACTS)
```

- [ ] **Step 2: Run to verify failure, then append the generator**

Append to `reva/odoo_contracts.py`:

```python
# --- generation -----------------------------------------------------------------
# (Move these imports to the module top if ruff flags E402 — keep the section
# comment either way.)

import hashlib
import json
from pathlib import Path


def _schema_for(contract: Contract) -> dict:
    if contract.model is not None:
        return contract.model.model_json_schema()
    inbound = _inbound_models()
    if contract.name in inbound:
        return inbound[contract.name].model_json_schema()
    # Shape-only contracts (health/error): derive a minimal schema from the sample.
    return {
        "type": "object",
        "properties": {k: {"type": _json_type(v)} for k, v in contract.sample.items()},
        "required": sorted(contract.sample.keys()),
    }


def _json_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if value is None:
        return "null"
    return "string"


def _dump(path: Path, data: dict) -> bytes:
    raw = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return raw


def generate(out_dir: Path) -> str:
    """Write contracts/ deterministically; return the contracts_version hash."""
    out_dir = Path(out_dir)
    hasher = hashlib.sha256()
    entries = []
    for c in CONTRACTS:
        sub = "callbacks" if c.direction == "reva->odoo" else "inbound"
        schema_rel = f"{sub}/{c.name}.schema.json"
        sample_rel = f"{sub}/{c.name}.sample.json"
        hasher.update(_dump(out_dir / schema_rel, _schema_for(c)))
        hasher.update(_dump(out_dir / sample_rel, c.sample))
        for i, extra in enumerate(c.extra_samples):
            extra_rel = f"{sub}/{c.name}.sample{i + 2}.json"
            hasher.update(_dump(out_dir / extra_rel, extra))
        entries.append({
            "name": c.name, "direction": c.direction, "method": c.method,
            "path": c.path, "auth": c.auth,
            "schema": schema_rel, "sample": sample_rel,
        })
    version = hasher.hexdigest()
    _dump(out_dir / "manifest.json", {
        "contracts_version": version,
        "generated_from": "reva/odoo_contracts.py",
        "contracts": entries,
    })
    return version


def check(committed_dir: Path) -> list[str]:
    """Regenerate to a temp dir and diff against the committed copy."""
    import tempfile

    committed_dir = Path(committed_dir)
    problems: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        generate(Path(tmp))
        fresh = {p.relative_to(tmp): p.read_bytes() for p in Path(tmp).rglob("*.json")}
        existing = {p.relative_to(committed_dir): p.read_bytes()
                    for p in committed_dir.rglob("*.json")} if committed_dir.is_dir() else {}
        for rel in sorted(set(fresh) | set(existing)):
            if fresh.get(rel) != existing.get(rel):
                problems.append(str(rel))
    return problems


def _main() -> None:
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="python -m reva.odoo_contracts")
    parser.add_argument("command", choices=["generate"])
    parser.add_argument("--out", default="contracts", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        problems = check(args.out)
        if problems:
            print("contracts/ is stale — regenerate and commit:\n  "
                  + "\n  ".join(problems))
            sys.exit(1)
        print("contracts/ is current.")
        return
    version = generate(args.out)
    print(f"contracts_version {version} written to {args.out}/")


if __name__ == "__main__":
    _main()
```

- [ ] **Step 3: Generate + hand-write the README**

```bash
cd /home/joseph/Projects/Cloudunify/cu_reva && worker/.venv/bin/python -m reva.odoo_contracts generate
```

Create `contracts/README.md`:

```markdown
# Odoo↔REVA contracts (generated — do not edit by hand)

Source of truth: `reva/odoo_contracts.py` (payload models used at runtime +
the CONTRACTS table). Regenerate after any contract change:

    python -m reva.odoo_contracts generate

The worker test suite fails when this directory is stale. Sync into the
ast-odoo repo with `scripts/sync_contracts.sh <ast-odoo-path>`; the manifest's
`contracts_version` is the freshness pin the addon tests assert against.
```

- [ ] **Step 4: Run to verify pass, commit**

```bash
cd worker && .venv/bin/python -m pytest tests/test_contracts_generator.py -q && cd ..
git add reva/odoo_contracts.py contracts/ worker/tests/test_contracts_generator.py
git commit -m "feat(contracts): deterministic generator + committed contracts/"
```

---

### Task 4: Drift + coverage tests

**Files:**
- Test: `worker/tests/test_contracts_drift.py`

- [ ] **Step 1: Write the tests (they should PASS immediately — they guard the future)**

Create `worker/tests/test_contracts_drift.py`:

```python
"""Drift guards: contracts/ current; every callback method published."""

from __future__ import annotations

import inspect
from pathlib import Path

from reva.odoo_client import OdooCallbackClient
from reva.odoo_contracts import CONTRACTS, check

_ROOT = Path(__file__).resolve().parents[2]


def test_committed_contracts_are_current():
    problems = check(_ROOT / "contracts")
    assert not problems, (
        "contracts/ is stale — run `python -m reva.odoo_contracts generate` "
        f"and commit. Differences: {problems}"
    )


def test_every_callback_method_has_a_contract():
    """Adding e.g. timesheet_results (/hr/) or website_analysis_result
    (/metasoul/) without publishing its contract fails here by design."""
    published_paths = {c.path for c in CONTRACTS if c.direction == "reva->odoo"}
    methods = [
        name for name, member in inspect.getmembers(
            OdooCallbackClient, predicate=inspect.isfunction)
        if not name.startswith("_")
    ]
    # Each public method must POST to a published path: assert by source scan.
    source = inspect.getsource(OdooCallbackClient)
    import re
    posted_paths = set(re.findall(r'self\._post\(\s*"([^"]+)"', source))
    unpublished = posted_paths - published_paths
    assert not unpublished, (
        f"OdooCallbackClient posts to unpublished paths {unpublished} — add "
        f"CONTRACTS entries in reva/odoo_contracts.py (see the contract-tests "
        f"spec) and regenerate contracts/."
    )
    assert methods, "sanity: introspection found no public methods"
```

- [ ] **Step 2: Run, commit**

```bash
cd worker && .venv/bin/python -m pytest tests/test_contracts_drift.py -q
git add worker/tests/test_contracts_drift.py
git commit -m "test(contracts): drift + coverage guards"
```

---

### Task 5: Sync script + pending-plan pointers

**Files:**
- Create: `scripts/sync_contracts.sh`
- Modify: `docs/superpowers/plans/2026-07-03-timesheet-wording-review.md`, `docs/superpowers/plans/2026-07-05-odoo-core-knowledge.md`? — no; timesheet + `docs/superpowers/plans/2026-07-04-metasoul-website-analysis.md` (one coordination line each), `CLAUDE.md`

- [ ] **Step 1: Create `scripts/sync_contracts.sh`**

```bash
#!/usr/bin/env bash
# Vendor the generated Odoo↔REVA contracts into the ast-odoo repo.
# Usage: scripts/sync_contracts.sh /path/to/ast-odoo
set -euo pipefail
[ $# -eq 1 ] || { echo "usage: $0 <ast-odoo-path>"; exit 2; }
SRC="$(cd "$(dirname "$0")/.." && pwd)/contracts"
DEST="$1/reva_contracts"
[ -d "$SRC" ] || { echo "contracts/ missing — run: python -m reva.odoo_contracts generate"; exit 1; }
rsync -a --delete "$SRC/" "$DEST/"
VERSION="$(python3 -c "import json;print(json.load(open('$SRC/manifest.json'))['contracts_version'][:12])")"
echo "Synced contracts_version ${VERSION}… to $DEST — review + commit there,"
echo "and bump the version pin in the addon's contract tests."
```

`chmod +x scripts/sync_contracts.sh`.

- [ ] **Step 2: Coordination pointers**

Append to the **Global Constraints** of the timesheet plan:

```markdown
- Contract publication (contract-tests spec, 2026-07-05): the `/hr/timesheet-results` callback MUST get a payload model + `CONTRACTS` entry in `reva/odoo_contracts.py` and a regenerated `contracts/` — the coverage drift test fails otherwise.
```

Same line (with `/metasoul/website-analysis-result`) in the metasoul plan's Global Constraints.

`CLAUDE.md` — in the definition-of-done paragraph, extend the sentence about suites with: `; a change to any Odoo↔REVA contract additionally requires regenerating contracts/ (python -m reva.odoo_contracts generate) and re-syncing ast-odoo`.

- [ ] **Step 3: Full gate + commit**

```bash
make test && worker/.venv/bin/ruff check reva worker/worker api/app scheduler/scheduler
git add scripts/sync_contracts.sh docs/superpowers/plans/ CLAUDE.md
git commit -m "feat(contracts): ast-odoo sync script + plan/DoD pointers"
```

---

## Addendum A — ast-odoo tasks (execute in that repo, after one sync)

Not part of this repo's plan execution; recorded here as the consumption
contract (the typed-issue-requests pattern):

1. Run `scripts/sync_contracts.sh <ast-odoo>` from cu_reva; commit
   `reva_contracts/` there.
2. Addon test per inbound controller (tickets write-field/reset-status/
   issues-created/issue-state under `/api/reva/tickets/…`): the vendored
   **sample** is accepted (2xx against a test record); a mutated sample
   (missing required key, wrong type) is rejected; the controller's route
   string equals the manifest `path` (prefixed by the app's `/api/reva`).
3. Addon test per outbound call (ticket-analysis / create-issues requests):
   the request body the addon builds validates against the vendored inbound
   schema (`jsonschema.validate`).
4. Freshness pin: one test asserts `manifest.json`'s `contracts_version`
   equals a constant in the addon test module — bumping it is the conscious
   review step after each sync.
