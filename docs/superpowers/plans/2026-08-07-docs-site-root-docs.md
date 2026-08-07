# Docs site shows the repo-root `docs/` folder — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show every registered repo's top-level `docs/` folder in the consultant docs browser (and in ticket-analysis grounding), with any `superpowers/` folder excluded.

**Architecture:** `reva/repo_docs.py::in_scope` is the single definition of "the repo's docs" — the `/repo-docs` API imports it and the lazy `sync_repo_docs` indexer applies it. Widening that one predicate widens both surfaces at once. Because a scope change does not move the GitHub tree SHA that sync staleness keys on, a new `repo_docs_sync.scope_version` column plus a module constant forces each repo to re-index exactly once. The SPA needs only a sort tweak: root docs arrive as a top-level `docs` node that would otherwise sort below the `cu_*` addon folders.

**Tech Stack:** Python 3.14 (FastAPI, SQLAlchemy, pytest), plain-SQL migrations applied at startup, Vue 3 + Vite SPA (`docs-ui`, no JS test harness).

**Spec:** `docs/superpowers/specs/2026-08-07-docs-site-root-docs-design.md`

## Global Constraints

- Scope prefixes after this change: `custom_addons/`, `custom-addons/`, `docs/` — `startswith`, so `docs/` matches **only** the repo root.
- `superpowers` is excluded as a **directory segment anywhere** in the path, not only at `docs/superpowers/`.
- Root `README.md` and other loose root-level `.md` files stay **out** of scope.
- `EXCLUDED_BASENAMES` (`CLAUDE.md`) is unchanged. `doc_priority` is unchanged.
- One shared scope: the widening intentionally reaches ticket-analysis / support-answer grounding as well as the browser.
- Migrations are idempotent plain SQL (`ADD COLUMN IF NOT EXISTS`), numbered, and mirrored by an ORM field in `reva/db/models.py` — tests build tables from the models, so a missing field makes the column invisible to tests.
- Work on a branch (`feat/docs-site-root-docs`), not `main`.
- Definition of done: `make test` (worker + api + scheduler — this touches shared `reva/`) and `ruff check reva worker/worker api/app scheduler/scheduler` both green.
- No TUI change: this adds no new operational signal, so `tui/` is untouched.

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `reva/repo_docs.py` | modify | Scope constants + `in_scope`; `_SCOPE_VERSION` and the two staleness checks in `sync_repo_docs` |
| `reva/db/models.py` | modify | `RepoDocsSync.scope_version` field |
| `db/migrations/045_repo_docs_scope_version.sql` | create | `repo_docs_sync.scope_version` column |
| `api/app/routes/docs.py` | modify | Comment only — the imported-scope note now says "custom addons only" |
| `docs-ui/src/tree.js` | modify | Pin a top-level `docs` node first; fix the stale header comment |
| `worker/tests/test_repo_docs.py` | modify | Scope table, the two tests that encode the old scope, new scope-version test |
| `api/tests/test_docs.py` | modify | `/tree` includes root `docs/**`, hides `superpowers/` |
| `docs/ticket-analysis.md` | modify | Two prose claims that the indexed scope is custom-addons-only |
| `docs-ui/README.md` | modify | Features list — what the tree shows |
| `HANDOFF.md` | modify | New addendum recording the change and its deploy requirement |

---

### Task 1: Widen the doc scope to the repo-root `docs/` folder

**Files:**
- Modify: `reva/repo_docs.py:1-8` (module docstring), `reva/repo_docs.py:31-55` (constants + `in_scope`)
- Modify: `api/app/routes/docs.py:37-40` (scope comment)
- Test: `worker/tests/test_repo_docs.py:75-94` (scope table), `worker/tests/test_repo_docs.py:248-260` (`test_sync_only_fetches_in_scope_files`)
- Test: `api/tests/test_docs.py:134-155` (`test_tree_returns_only_markdown_under_custom_addons`)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `in_scope(path: str) -> bool` with the widened predicate; module constants `SCOPE_PREFIXES: tuple[str, ...]` and the new `EXCLUDED_SEGMENTS: tuple[str, ...]`. Task 2 relies on `in_scope` unchanged in signature.

- [ ] **Step 1: Update the scope table to the new expected behavior (failing test)**

In `worker/tests/test_repo_docs.py`, replace the `@pytest.mark.parametrize` block above `test_in_scope` with:

```python
@pytest.mark.parametrize(
    "path,expected",
    [
        ("custom_addons/cu_sale/README.md", True),
        ("custom-addons/cu_sale/README.md", True),          # hyphen variant
        ("custom_addons/cu_sale/docs/guide.markdown", True),
        ("custom_addons/cu_sale/README.MD", True),           # case-insensitive ext
        ("docs/setup-local.md", True),                       # repo-root docs folder
        ("docs/nested/deep/guide.md", True),
        ("docs/superpowers/specs/x-design.md", False),       # agent bookkeeping
        ("docs/superpowers/plans/y.md", False),
        ("custom_addons/cu_sale/docs/superpowers/z.md", False),  # segment anywhere
        ("docs/superpowers.md", True),                       # a FILE, not the folder
        ("custom_addons/cu_sale/CLAUDE.md", False),          # excluded basename
        ("custom_addons/CLAUDE.md", False),
        ("custom_addons/cu_sale/model.py", False),           # not markdown
        ("README.md", False),                                # loose root markdown
        ("CHANGELOG.md", False),
        ("docs/notes.txt", False),                           # not markdown
        ("documentation/guide.md", False),                   # prefix is anchored
        ("custom_addons/cu_sale/notes.txt", False),
    ],
)
def test_in_scope(path, expected):
    assert in_scope(path) is expected
```

- [ ] **Step 2: Update the sync test that encodes the old scope (failing test)**

Same file, replace `test_sync_only_fetches_in_scope_files` (it currently asserts `docs/guide.md` is skipped):

```python
def test_sync_only_fetches_in_scope_files(db):
    gh = _FakeGitHub(
        tree=_tree("sha1", [
            "custom_addons/a/README.md",
            "custom_addons/a/model.py",            # not markdown
            "docs/guide.md",                       # repo-root docs: in scope
            "docs/superpowers/specs/x-design.md",  # agent bookkeeping: never
            "README.md",                           # loose root markdown: out
            "custom_addons/a/CLAUDE.md",           # excluded basename
        ]),
        files={
            "custom_addons/a/README.md": "# H\nb\n",
            "docs/guide.md": "# G\nb\n",
        },
    )
    sync_repo_docs(db, gh, "acme", "widgets")
    assert sorted(p for p, _ in gh.file_fetches) == [
        "custom_addons/a/README.md",
        "docs/guide.md",
    ]
```

- [ ] **Step 3: Update the api tree test (failing test)**

In `api/tests/test_docs.py`, replace `test_tree_returns_only_markdown_under_custom_addons` with:

```python
def test_tree_returns_markdown_in_scope_only(env):
    client, db, _ = env
    rid = _seed_repo(db)
    _use_github(_FakeGitHub(tree={
        "tree": [
            {"path": "custom_addons/cu_x/docs/consultant.md", "type": "blob", "size": 10},
            {"path": "custom_addons/cu_x/README.md", "type": "blob", "size": 5},
            {"path": "custom_addons/cu_x/CLAUDE.md", "type": "blob", "size": 4},  # agent file, hidden
            {"path": "custom_addons/cu_x/app.py", "type": "blob", "size": 99},  # not markdown
            {"path": "custom_addons/cu_x/docs", "type": "tree"},                # directory
            {"path": "docs/architecture.md", "type": "blob", "size": 7},        # repo-root docs
            {"path": "docs/superpowers/specs/a-design.md", "type": "blob", "size": 7},  # hidden
            {"path": "README.md", "type": "blob", "size": 3},                   # out of scope
        ],
        "truncated": False,
    }))
    body = client.get(f"/repo-docs/repos/{rid}/tree").json()
    assert [e["path"] for e in body["entries"]] == [
        "custom_addons/cu_x/README.md",
        "custom_addons/cu_x/docs/consultant.md",
        "docs/architecture.md",
    ]
    assert body["ref"] == "main"
    assert body["truncated"] is False
```

(The endpoint sorts entries by path, so `custom_addons/...` precedes `docs/...`.)

- [ ] **Step 4: Run the tests to verify they fail**

```bash
cd worker && .venv/bin/python -m pytest tests/test_repo_docs.py -k "in_scope" -v
cd ../api && .venv/bin/python -m pytest tests/test_docs.py -k "tree_returns" -v
```

Expected: FAIL — `assert False is True` for the `docs/...` rows, and the api test's entries list missing `docs/architecture.md`.

- [ ] **Step 5: Widen `in_scope`**

In `reva/repo_docs.py`, replace the scope-constant block and `in_scope` (currently lines 31-55) with:

```python
# Markdown scope — the consultant docs browser's definition. Custom addons plus
# the repo's own top-level docs/ folder; CLAUDE.md is agent instructions and
# superpowers/ is agent workflow bookkeeping (specs/plans), neither is docs.
# docs.py imports these back.
DOC_EXTENSIONS = (".md", ".markdown")
SCOPE_PREFIXES = ("custom_addons/", "custom-addons/", "docs/")
EXCLUDED_BASENAMES = ("CLAUDE.md",)
EXCLUDED_SEGMENTS = ("superpowers",)
```

and

```python
def in_scope(path: str) -> bool:
    """True for a markdown doc under a scope prefix that isn't excluded.

    `startswith` is anchored, so "docs/" matches only the repo-root folder —
    `custom_addons/cu_x/docs/*.md` keeps matching through its own prefix.
    `EXCLUDED_SEGMENTS` is checked against directory segments only (the last
    segment is the filename), so `docs/superpowers.md` is a doc while
    `docs/superpowers/spec.md` is not.
    """
    return (
        path.lower().endswith(DOC_EXTENSIONS)
        and path.startswith(SCOPE_PREFIXES)
        and not any(seg in EXCLUDED_SEGMENTS for seg in path.split("/")[:-1])
        and not path.endswith(tuple("/" + b for b in EXCLUDED_BASENAMES))
    )
```

- [ ] **Step 6: Update the two stale scope comments**

In `reva/repo_docs.py`, line 1-8 docstring — replace the first sentence:

```python
"""Customer-repo docs retrieval: index each repo's markdown docs section-level
into Postgres and search them for ticket-analysis grounding.
```

(leave the rest of the docstring as-is).

In `api/app/routes/docs.py`, replace the comment above the `reva.repo_docs` import (lines 37-39):

```python
# Markdown served as text through /file; the doc scope (DOC_EXTENSIONS +
# in_scope — custom addons plus the repo-root docs/ folder) is shared with
# ticket-analysis retrieval — one definition of "the repo's docs"
# (reva/repo_docs.py).
```

- [ ] **Step 7: Run the tests to verify they pass**

```bash
cd worker && .venv/bin/python -m pytest tests/test_repo_docs.py -v
cd ../api && .venv/bin/python -m pytest tests/test_docs.py -v
```

Expected: PASS, all of both files (not just the edited tests — `test_sync_indexes_then_fresh_fast_path` and the cap tests must stay green).

- [ ] **Step 8: Commit**

```bash
git add reva/repo_docs.py api/app/routes/docs.py worker/tests/test_repo_docs.py api/tests/test_docs.py
git commit -m "feat(docs): include the repo-root docs folder in the doc scope

The consultant docs browser and ticket-analysis grounding share in_scope, so
both now see a repo's top-level docs/ — minus any superpowers/ folder, which is
agent workflow bookkeeping rather than documentation."
```

---

### Task 2: Force one re-index with `scope_version`

**Files:**
- Create: `db/migrations/045_repo_docs_scope_version.sql`
- Modify: `reva/db/models.py:1009-1023` (`RepoDocsSync`)
- Modify: `reva/repo_docs.py` (new `_SCOPE_VERSION` constant; `sync_repo_docs` staleness checks at the fast path and under the lock, plus both write paths)
- Test: `worker/tests/test_repo_docs.py`

**Interfaces:**
- Consumes: `in_scope` from Task 1 (the widened scope is what makes the re-index necessary); `sync_repo_docs(db, github, owner, repo) -> dict` unchanged in signature and return keys (`status`, `sections`, `error`).
- Produces: `reva.repo_docs._SCOPE_VERSION: int` (= 1) and `RepoDocsSync.scope_version: int`.

- [ ] **Step 1: Write the failing test**

In `worker/tests/test_repo_docs.py`, extend the import from `reva.repo_docs` with `_SCOPE_VERSION`:

```python
from reva.repo_docs import (
    _MAX_FILES,
    _SCOPE_VERSION,
    doc_priority,
    in_scope,
    search_repo_docs,
    split_markdown_sections,
    sync_repo_docs,
)
```

and add this test immediately after `test_sync_reindexes_on_tree_sha_change`:

```python
def test_sync_reindexes_when_scope_version_is_stale(db):
    """A scope widening does not move the tree SHA, so the version stamp is the
    only thing that can force the re-index."""
    gh = _FakeGitHub(
        tree=_tree("sha1", ["custom_addons/a/README.md", "docs/guide.md"]),
        files={
            "custom_addons/a/README.md": "# H\nb\n",
            "docs/guide.md": "# G\nb\n",
        },
    )
    sync_repo_docs(db, gh, "acme", "widgets")
    with db.session() as s:
        assert s.get(RepoDocsSync, "acme/widgets").scope_version == _SCOPE_VERSION

    # Simulate a row indexed under the previous scope: same tree, old version.
    with db.session() as s:
        s.get(RepoDocsSync, "acme/widgets").scope_version = _SCOPE_VERSION - 1
    gh.file_fetches.clear()

    r = sync_repo_docs(db, gh, "acme", "widgets")
    assert r["status"] == "synced"
    assert sorted(p for p, _ in gh.file_fetches) == [
        "custom_addons/a/README.md",
        "docs/guide.md",
    ]
    with db.session() as s:
        assert s.get(RepoDocsSync, "acme/widgets").scope_version == _SCOPE_VERSION
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd worker && .venv/bin/python -m pytest tests/test_repo_docs.py -k scope_version -v
```

Expected: FAIL at collection — `ImportError: cannot import name '_SCOPE_VERSION' from 'reva.repo_docs'`.

- [ ] **Step 3: Add the migration**

Create `db/migrations/045_repo_docs_scope_version.sql`:

```sql
-- Scope-version stamp for the repo-docs index (spec 2026-08-07). The doc scope
-- widened to include each repo's root docs/ folder, which does not move the
-- GitHub tree SHA that sync staleness keys on — so an already-indexed repo
-- would never pick the new files up. reva/repo_docs.py::_SCOPE_VERSION is
-- compared against this column, making every repo stale exactly once per scope
-- change. Default 0 = indexed before the stamp existed.
-- Mirrors reva/db/models.py (RepoDocsSync).
ALTER TABLE repo_docs_sync ADD COLUMN IF NOT EXISTS scope_version INTEGER NOT NULL DEFAULT 0;
```

- [ ] **Step 4: Add the ORM field**

In `reva/db/models.py`, inside `class RepoDocsSync`, add after the `truncated` column:

```python
    scope_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
```

and extend the class docstring's parenthetical to `(migration 039; scope_version migration 045)`.

- [ ] **Step 5: Stamp and check the version in `sync_repo_docs`**

In `reva/repo_docs.py`, add the constant next to `_MAX_FILES`:

```python
# Bump whenever `in_scope` changes: the tree SHA is unmoved by a scope change,
# so this stamp is what makes every already-indexed repo re-sync exactly once.
_SCOPE_VERSION = 1
```

Replace the fast-path staleness check (currently `if row is not None and tree_sha is not None and row.tree_sha == tree_sha:`) with:

```python
        if _is_current(row, tree_sha):
            return {"status": "fresh", "sections": row.sections, "error": None}
```

Apply the identical replacement to the re-check under the advisory lock, and add the helper above `sync_repo_docs`:

```python
def _is_current(row: RepoDocsSync | None, tree_sha: str | None) -> bool:
    """True when the stored index matches both the repo's tree AND the scope
    that produced it."""
    return (
        row is not None
        and tree_sha is not None
        and row.tree_sha == tree_sha
        and row.scope_version == _SCOPE_VERSION
    )
```

Then stamp both write paths — in the insert branch:

```python
            s.add(RepoDocsSync(
                repo_full_name=repo_key, tree_sha=tree_sha or "",
                files=len(paths), sections=len(sections), truncated=truncated,
                scope_version=_SCOPE_VERSION,
            ))
```

and in the update branch, after `row.truncated = truncated`:

```python
            row.scope_version = _SCOPE_VERSION
```

- [ ] **Step 6: Run the tests to verify they pass**

```bash
cd worker && .venv/bin/python -m pytest tests/test_repo_docs.py -v
```

Expected: PASS — including `test_sync_indexes_then_fresh_fast_path`, which proves a freshly written row is stamped current and still takes the zero-fetch path.

- [ ] **Step 7: Verify the migration on real Postgres**

```bash
make test-integration
```

Expected: PASS. Tests build tables from the ORM, so this is the only check that migration 045's raw SQL parses and applies. If Docker is unavailable, say so plainly rather than claiming the SQL is verified — the first staging boot becomes the check.

- [ ] **Step 8: Commit**

```bash
git add db/migrations/045_repo_docs_scope_version.sql reva/db/models.py reva/repo_docs.py worker/tests/test_repo_docs.py
git commit -m "feat(docs): stamp the doc-scope version so a widening re-indexes once

sync_repo_docs keys staleness on the GitHub tree SHA, which a scope change does
not move. repo_docs_sync.scope_version (migration 045) makes every repo indexed
under the old scope re-sync lazily on its next use."
```

---

### Task 3: Pin the root `docs` folder first in the SPA tree

**Files:**
- Modify: `docs-ui/src/tree.js:1-24`
- Modify: `docs-ui/README.md` (Features list)

**Interfaces:**
- Consumes: `/repo-docs/repos/{id}/tree` entries widened in Task 1 (`{path, size}` objects, sorted by path).
- Produces: `buildDocTree(entries)` — same signature and node shape as today (`{type:'dir', name, children}` / `{type:'file', name, path}`), only the root ordering changes.

There is no JS test harness in `docs-ui` (no `test` script in `package.json`), so verification here is a Node one-liner against the module plus a manual browser check — not a unit test. Do not add a test framework for this.

- [ ] **Step 1: Write the verification command and watch it fail**

```bash
cd docs-ui && node --input-type=module -e "
import { buildDocTree } from './src/tree.js'
const nodes = buildDocTree([
  { path: 'custom_addons/cu_sale/README.md' },
  { path: 'custom_addons/cu_approval/README.md' },
  { path: 'docs/architecture.md' },
])
const names = nodes.map((n) => n.name)
console.log(names.join(','))
if (names[0] !== 'docs') { console.error('FAIL: docs is not first'); process.exit(1) }
console.log('PASS')
"
```

Expected: `cu_approval,cu_sale,docs` then `FAIL: docs is not first` (exit 1) — `cu_*` sorts before `docs`, so the repo's own documentation lands at the bottom.

- [ ] **Step 2: Fix the header comment and pin the root `docs` node**

In `docs-ui/src/tree.js`, replace the three header comment lines with:

```js
// Turn the flat list of doc paths from /tree into a nested folder hierarchy.
// The backend scopes to custom_addons/ plus the repo-root docs/ folder; we strip
// the custom_addons segment so addon folders surface at the top level, and keep
// the repo's own docs/ folder there under its own name.
```

Then change the `return` of `buildDocTree` from `return toNodes(root)` to:

```js
  return docsFirst(toNodes(root))
```

and add below `buildDocTree`:

```js
// The repo's own docs/ folder is the natural entry point, but addons are named
// cu_* and sort ahead of it — hoist it to the top of the root listing.
function docsFirst(nodes) {
  const i = nodes.findIndex((n) => n.type === 'dir' && n.name === 'docs')
  return i <= 0 ? nodes : [nodes[i], ...nodes.slice(0, i), ...nodes.slice(i + 1)]
}
```

- [ ] **Step 3: Re-run the verification command**

Run the Step 1 command again. Expected: `docs,cu_approval,cu_sale` then `PASS`.

- [ ] **Step 4: Confirm the bundle still builds**

```bash
cd docs-ui && npm run build
```

Expected: a clean Vite build into `dist/`.

- [ ] **Step 5: Manual check in the browser**

With the stack running (`make dev`, api on :8080):

```bash
cd docs-ui && npm run dev     # http://localhost:5173
```

Expand a repo that has a root `docs/` folder and confirm: `docs` appears first in the tree, its files open and render, and no `superpowers` folder is listed anywhere. Report what you actually saw; if you cannot run the stack, say the manual check was not performed.

- [ ] **Step 6: Update the docs-ui README**

In `docs-ui/README.md`, replace the first bullet of the **Features** list with:

```markdown
- Scrollable sidebar of all enabled repos; expand a repo to lazy-load its `.md`
  tree — the repo-root `docs/` folder first, then each `custom_addons/` addon.
  `CLAUDE.md` and any `superpowers/` folder are never listed.
```

- [ ] **Step 7: Commit**

```bash
git add docs-ui/src/tree.js docs-ui/README.md
git commit -m "feat(docs-ui): show the repo-root docs folder first in the tree"
```

---

### Task 4: Correct the prose, run the full gate, archive the spec and plan

**Files:**
- Modify: `docs/ticket-analysis.md:66` and the grounding bullet at `docs/ticket-analysis.md:218-219`
- Modify: `HANDOFF.md` (new addendum at the top, under the `# REVA — Work Handoff` title)
- Move: `docs/superpowers/specs/2026-08-07-docs-site-root-docs-design.md` → `docs/superpowers/specs/archive/`
- Move: `docs/superpowers/plans/2026-08-07-docs-site-root-docs.md` → `docs/superpowers/plans/archive/`

**Interfaces:**
- Consumes: the finished behavior from Tasks 1-3. Produces nothing consumed by code.

- [ ] **Step 1: Fix the two scope claims in `docs/ticket-analysis.md`**

At line 66, replace `grounds the analysis in the repo's own custom-addon docs — the worker lazily indexes `custom_addons/**/*.md` from the repo's default branch` with:

```markdown
grounds the analysis in the repo's own docs — the worker lazily indexes `custom_addons/**/*.md` plus the repo-root `docs/**/*.md` from the repo's default branch
```

In the grounding bullet (~line 218), replace:

```markdown
- The repo's default-branch markdown docs under `custom_addons/`/`custom-addons/`
  (excluding `CLAUDE.md`) are indexed **section-level** into Postgres
```

with:

```markdown
- The repo's default-branch markdown docs under `custom_addons/`/`custom-addons/`
  and the repo-root `docs/` folder (excluding `CLAUDE.md` and any `superpowers/`
  folder) are indexed **section-level** into Postgres
```

- [ ] **Step 2: Run the full gate**

```bash
make test
ruff check reva worker/worker api/app scheduler/scheduler
```

Expected: all three suites PASS and ruff clean. `scheduler` is included because `reva/` is shared. Fix anything that fails before continuing — do not proceed on a red suite.

- [ ] **Step 3: Add the HANDOFF addendum**

In `HANDOFF.md`, insert immediately after the `# REVA — Work Handoff` title line:

```markdown
## Addendum 2026-08-07 — docs site shows the repo-root `docs/` folder

**Status: implemented** (spec
`docs/superpowers/specs/archive/2026-08-07-docs-site-root-docs-design.md`, plan
`docs/superpowers/plans/archive/2026-08-07-docs-site-root-docs.md`, both
archived on completion). `reva/repo_docs.py::in_scope` now covers each repo's
top-level `docs/` folder in addition to `custom_addons/`; any `superpowers/`
folder is excluded as a directory segment anywhere in the path.

**Two things to know before deploying.** The scope is shared with
ticket-analysis / support-answer grounding, so root docs now enter
`repo_doc_sections` and can be cited in customer-facing answers — the first
analysis per repo after deploy pays one re-index, forced by
`repo_docs_sync.scope_version` (migration 045) because a scope change does not
move the tree SHA that sync staleness keys on. And `docs-ui` is built into the
nginx image, so the SPA change needs
`docker compose -f docker-compose.prod.yml build nginx` on top of the api/worker
redeploy that carries the migration.

**Not live-validated.** Unit-tested (worker + api) and, if Docker was
available, migration-checked on real Postgres via `make test-integration`; no
prod or staging run yet.
```

Correct the last paragraph if `make test-integration` did not actually run in Task 2.

- [ ] **Step 4: Archive the spec and plan**

```bash
git mv docs/superpowers/specs/2026-08-07-docs-site-root-docs-design.md docs/superpowers/specs/archive/
git mv docs/superpowers/plans/2026-08-07-docs-site-root-docs.md docs/superpowers/plans/archive/
```

Then change the spec's `- **Status:** open — not implemented.` line to `- **Status:** implemented 2026-08-07 (migration 045). Unit-tested; not live-validated.`

- [ ] **Step 5: Commit**

```bash
git add docs/ticket-analysis.md HANDOFF.md docs/superpowers
git commit -m "docs: record the widened doc scope and archive the spec + plan"
```

- [ ] **Step 6: Report status**

Summarize honestly: which suites ran and passed, whether `make test-integration` ran, whether the browser check was performed, and that the change is **not deployed** — prod needs the nginx rebuild plus the api/worker redeploy noted in the HANDOFF addendum. Do not deploy; that is Joseph's manual trigger.

---

## Deploy (not part of the plan — Joseph triggers it)

```bash
docker compose -f docker-compose.prod.yml build nginx   # bundles the SPA change
docker compose -f docker-compose.prod.yml up -d nginx
# plus the standard api/worker redeploy (scripts/deploy.sh) for migration 045
```
