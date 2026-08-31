# Docs site renders `.html` docs — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The consultant docs browser lists and renders `.html`/`.htm` docs that live in a `docs/` folder, alongside the Markdown it already serves.

**Architecture:** `reva/repo_docs.py::in_scope` stays the Markdown-only predicate that the ticket-analysis grounding index applies; a new sibling `browser_in_scope` widens the surface for the docs API only. The SPA's renderer already sanitizes HTML (markdown-it runs with `html: true`), so the frontend change is extracting the post-sanitize pipeline into a shared `postProcess` and adding a second entry point that skips markdown-it.

**Tech Stack:** Python 3.14 / FastAPI (`api/`), shared `reva/` library, pytest; Vue 3 + Vite + markdown-it + DOMPurify (`docs-ui/`).

**Spec:** `docs/superpowers/specs/2026-08-31-docs-site-html-rendering-design.md`

## Global Constraints

- **`in_scope`, `DOC_EXTENSIONS`, `doc_priority`, `_SCOPE_VERSION` and `sync_repo_docs` must not change behavior.** The grounding index must keep rejecting `.html`. No migration, no forced re-index.
- **`.html` is never served through `/raw`.** `ASSET_EXTENSIONS` is untouched; `/raw` keeps returning 415 for `.html`.
- **No commits.** Joseph commits himself. Every task ends with `git add` staging only — never run `git commit` or `git push`.
- **Definition of done for the Python tasks:** `make test` (a shared `reva/` change affects worker, api and scheduler) plus `ruff check reva worker/worker api/app scheduler/scheduler`.
- Match existing style: no new abstractions, no adjacent refactoring, comments in English.

---

### Task 1: Browser-only scope predicate

**Files:**
- Modify: `reva/repo_docs.py:1-8` (module docstring), `reva/repo_docs.py:35-38` (constants), after `reva/repo_docs.py:73` (new function below `in_scope`)
- Modify: `reva/README.md:28` (the `repo_docs.py` row)
- Test: `worker/tests/test_repo_docs.py:9-17` (imports), `worker/tests/test_repo_docs.py:76-104` (scope section)

**Interfaces:**
- Consumes: existing module constants `DOC_EXTENSIONS`, `SCOPE_PREFIXES`, `EXCLUDED_SEGMENTS`, `EXCLUDED_BASENAMES` and `in_scope(path: str) -> bool` — all unchanged.
- Produces: `BROWSER_DOC_EXTENSIONS: tuple[str, ...]` and `browser_in_scope(path: str) -> bool`. Task 2 imports both by these exact names.

- [ ] **Step 1: Write the failing tests**

In `worker/tests/test_repo_docs.py`, replace the import block at lines 9-17 with:

```python
from reva.repo_docs import (
    _MAX_FILES,
    _SCOPE_VERSION,
    BROWSER_DOC_EXTENSIONS,
    DOC_EXTENSIONS,
    browser_in_scope,
    doc_priority,
    in_scope,
    search_repo_docs,
    split_markdown_sections,
    sync_repo_docs,
)
```

Then append these two cases to the **existing** `test_in_scope` parametrize table (they lock the grounding boundary — HTML must stay out of the index):

```python
        ("docs/handbook.html", False),                       # HTML is browser-only
        ("custom_addons/cu_sale/docs/spec.html", False),
```

And add a new section after `test_in_scope`:

```python
# ---- browser_in_scope -------------------------------------------------------


@pytest.mark.parametrize(
    "path,expected",
    [
        # HTML is accepted only inside a docs/ folder.
        ("docs/handbook.html", True),                             # repo-root docs
        ("docs/nested/handbook.html", True),
        ("custom_addons/cu_sale/docs/spec.html", True),           # an addon's docs
        ("custom-addons/cu_sale/docs/spec.htm", True),            # hyphen + .htm
        ("docs/HANDBOOK.HTML", True),                             # case-insensitive ext
        # The Odoo manifest stub every addon ships — the reason for the docs/ rule.
        ("custom_addons/cu_sale/static/description/index.html", False),
        ("custom_addons/cu_sale/README.html", False),             # no docs/ segment
        ("custom_addons/cu_sale/index.html", False),
        ("index.html", False),                                    # loose root HTML
        ("docs/superpowers/plan.html", False),                    # agent bookkeeping
        ("docs/SUPERPOWERS/plan.html", False),
        ("custom_addons/cu_sale/docs/superpowers/z.html", False),
        ("documentation/guide.html", False),                      # prefix is anchored
        ("custom_addons/cu_sale/docs/app.py", False),             # neither md nor html
        # Every markdown path in_scope accepts is still accepted here.
        ("custom_addons/cu_sale/README.md", True),
        ("docs/setup-local.md", True),
        ("docs/superpowers/specs/x-design.md", False),
        ("custom_addons/cu_sale/CLAUDE.md", False),
    ],
)
def test_browser_in_scope(path, expected):
    assert browser_in_scope(path) is expected


def test_browser_doc_extensions_are_html_only():
    """The grounding scope stays markdown-only; HTML lives in the browser tuple."""
    assert BROWSER_DOC_EXTENSIONS == (".html", ".htm")
    assert DOC_EXTENSIONS == (".md", ".markdown")
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd worker && .venv/bin/python -m pytest tests/test_repo_docs.py -k "scope or extensions" -v
```

Expected: `ImportError: cannot import name 'browser_in_scope' from 'reva.repo_docs'` — the whole file errors at collection, which is the correct failure here.

- [ ] **Step 3: Add the constant and the predicate**

In `reva/repo_docs.py`, directly below the `EXCLUDED_SEGMENTS` line, add:

```python
# HTML is browser-only. The docs SITE renders it; the grounding index must never
# ingest it — `split_markdown_sections` splits on ATX `#` headings, which an HTML
# file has none of, so the whole file would land as one stem-titled section of
# truncated tag soup and out-rank real prose in `search_repo_docs`. Narrower than
# the markdown scope on purpose: HTML only inside a docs/ folder, which is what
# keeps every addon's static/description/index.html manifest stub out of the tree.
BROWSER_DOC_EXTENSIONS = (".html", ".htm")
```

And directly below `in_scope` (after its closing `)` on line 73), add:

```python
def browser_in_scope(path: str) -> bool:
    """True for anything the consultant docs browser serves as text.

    Every markdown doc `in_scope` covers, plus HTML that sits inside a `docs/`
    folder — the repo root's or an addon's own. `in_scope` stays the narrower
    grounding scope; only `api/app/routes/docs.py` calls this one.
    """
    if in_scope(path):
        return True
    segments = [seg.lower() for seg in path.split("/")[:-1]]
    return (
        path.lower().endswith(BROWSER_DOC_EXTENSIONS)
        and path.startswith(SCOPE_PREFIXES)
        and "docs" in segments
        and not any(seg in EXCLUDED_SEGMENTS for seg in segments)
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd worker && .venv/bin/python -m pytest tests/test_repo_docs.py -v
```

Expected: PASS, including every pre-existing test in the file.

- [ ] **Step 5: Update the two prose claims this change falsifies**

`reva/repo_docs.py` module docstring — replace lines 4-5:

```
Scope (`in_scope`) is the single definition of "the repo's docs", shared with
the consultant docs browser (`api/app/routes/docs.py` imports it from here).
```

with:

```
Scope: `in_scope` defines the GROUNDED docs (markdown only) and is what this
module's sync applies. `browser_in_scope` is the wider surface the consultant
docs browser serves — the same markdown plus HTML inside a docs/ folder;
`api/app/routes/docs.py` imports it from here.
```

`reva/README.md:28` — replace `markdown scope shared with the docs browser (\`in_scope\`)` with `markdown grounding scope (\`in_scope\`) plus the wider docs-browser scope (\`browser_in_scope\`)`.

- [ ] **Step 6: Run the full gate and stage**

```bash
make test
ruff check reva worker/worker api/app scheduler/scheduler
git add reva/repo_docs.py reva/README.md worker/tests/test_repo_docs.py
```

Expected: all three suites green, ruff clean. **Do not commit.**

---

### Task 2: Serve HTML through `/tree` and `/file`

**Files:**
- Modify: `api/app/routes/docs.py:40-44` (scope comment + import), `api/app/routes/docs.py:90` (tree filter), `api/app/routes/docs.py:203-207` (`/file` gate + docstring)
- Test: `api/tests/test_docs.py` (`test_tree_returns_markdown_in_scope_only` at line 134, and new tests in the `/file` and `/raw` sections)

**Interfaces:**
- Consumes: `BROWSER_DOC_EXTENSIONS: tuple[str, ...]` and `browser_in_scope(path: str) -> bool` from Task 1.
- Produces: `/repo-docs/repos/{id}/tree` entries now include in-scope `.html`/`.htm`; `/repo-docs/repos/{id}/file` returns 200 with the raw source for those paths. `/raw` is unchanged. Task 3 relies on `/file` returning the HTML verbatim in the existing `content` field.

- [ ] **Step 1: Write the failing tests**

In `api/tests/test_docs.py`, extend the tree fixture inside `test_tree_returns_markdown_in_scope_only` — add these three entries to the `"tree"` list:

```python
            {"path": "docs/handbook.html", "type": "blob", "size": 12},          # HTML doc
            {"path": "custom_addons/cu_x/docs/spec.htm", "type": "blob", "size": 8},
            {"path": "custom_addons/cu_x/static/description/index.html", "type": "blob", "size": 110},  # manifest stub, hidden
```

and update its assertion to the new sorted list:

```python
    assert [e["path"] for e in body["entries"]] == [
        "custom_addons/cu_x/README.md",
        "custom_addons/cu_x/docs/consultant.md",
        "custom_addons/cu_x/docs/spec.htm",
        "docs/architecture.md",
        "docs/handbook.html",
    ]
```

Rename the test to `test_tree_returns_docs_in_scope_only` (it is no longer markdown-only).

Then add, after `test_file_non_markdown_is_415`:

```python
def test_file_returns_html(env):
    client, db, _ = env
    rid = _seed_repo(db)
    _use_github(_FakeGitHub(files={"docs/handbook.html": "<h1>Hello</h1>"}))
    body = client.get(f"/repo-docs/repos/{rid}/file?path=docs/handbook.html").json()
    assert body["content"] == "<h1>Hello</h1>"


def test_file_html_outside_docs_folder_is_415(env):
    """The addon manifest stub is not a doc, even though it is HTML."""
    client, db, _ = env
    rid = _seed_repo(db)
    _use_github(_FakeGitHub())
    path = "custom_addons/cu_x/static/description/index.html"
    assert client.get(f"/repo-docs/repos/{rid}/file?path={path}").status_code == 415
```

And in the `/raw` section, beside the existing `raw?path=docs/intro.md` 415 assertion:

```python
def test_raw_refuses_html(env):
    """SECU: HTML from a customer repo must never be served as bytes from our
    own origin — /file hands it to the SPA, which sanitizes it."""
    client, db, _ = env
    rid = _seed_repo(db)
    _use_github(_FakeGitHub())
    assert client.get(f"/repo-docs/repos/{rid}/raw?path=docs/handbook.html").status_code == 415
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd api && .venv/bin/python -m pytest tests/test_docs.py -v
```

Expected: `test_tree_returns_docs_in_scope_only` fails on the entries list (HTML absent), `test_file_returns_html` fails with 415, `test_file_html_outside_docs_folder_is_415` and `test_raw_refuses_html` already pass.

- [ ] **Step 3: Wire the browser scope in**

In `api/app/routes/docs.py`, replace the comment + import at lines 40-44:

```python
# Docs served as text through /file; the browser's doc scope
# (`browser_in_scope` — markdown under custom addons and the repo-root docs/
# folder, plus HTML inside any docs/ folder) is deliberately WIDER than the
# `in_scope` used for ticket-analysis grounding (reva/repo_docs.py).
from reva.repo_docs import BROWSER_DOC_EXTENSIONS, DOC_EXTENSIONS, browser_in_scope
```

Line 90, inside `_cached_tree`:

```python
            if e.get("type") == "blob" and browser_in_scope(e["path"])
```

Lines 203-207, in `doc_file` — docstring and gate:

```python
    """Raw doc source (Markdown or HTML) for one file. Returned as JSON data,
    never as an HTML response — the frontend renders + sanitizes it (DOMPurify),
    so no HTML is built or served as a document here."""
    safe = _safe_path(path)
    if not safe.lower().endswith(DOC_EXTENSIONS + BROWSER_DOC_EXTENSIONS):
        raise HTTPException(status_code=415, detail="Only doc files are served as text")
```

> **Superseded during implementation.** This gate is extension-only, which lets
> `custom_addons/<addon>/static/description/index.html` reach the fetch and 404
> instead of the 415 that Step 1's `test_file_html_outside_docs_folder_is_415`
> requires. What shipped additionally scope-checks HTML through
> `browser_in_scope`, leaving markdown's extension-only behavior unchanged. See
> the archived spec's Design item 2.

Leave `ASSET_EXTENSIONS`, `doc_raw`, `doc_search`, `doc_branches` and `list_doc_repos` untouched.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd api && .venv/bin/python -m pytest tests/test_docs.py -v
```

Expected: PASS, whole file.

- [ ] **Step 5: Run the full gate and stage**

```bash
make test
ruff check reva worker/worker api/app scheduler/scheduler
git add api/app/routes/docs.py api/tests/test_docs.py
```

Expected: green and clean. **Do not commit.**

---

### Task 3: Render HTML docs in the SPA

**Files:**
- Modify: `docs-ui/src/markdown.js` (whole render pipeline), `docs-ui/src/components/DocView.vue` (the `load()` function, ~line 55)
- Modify: `docs-ui/README.md` (intro paragraph + two Features bullets)
- Test: none — `docs-ui` has no JS test harness (no vitest/jest in `package.json`). Verified by build + browser, Step 4.

**Interfaces:**
- Consumes: `/repo-docs/repos/{id}/file` from Task 2 returning HTML source in `content`.
- Produces: `renderHtml(source, ctx) -> { html, toc, hasMermaid }` exported from `markdown.js` with the same shape `renderMarkdown` already returns; `ctx` is the existing `{ repoId, path, owner, name, branch }`.

- [ ] **Step 1: Split the renderer**

In `docs-ui/src/markdown.js`, add a shared sanitize config below the `EXTERNAL` constant:

```js
// A doc's <style> block emits GLOBAL css into the SPA's own page and can
// restyle or overlay it; inline style="…" cannot escape its element, so it
// stays — it is what keeps an exported table looking like a table.
const SANITIZE = { USE_PROFILES: { html: true }, FORBID_TAGS: ['style'] }
```

Rename the existing `renderMarkdown` body: everything from `const tpl = document.createElement('template')` down to the `return` becomes a new module-level function, moved **verbatim** apart from its signature and the `baseDir` line:

```js
// Shared post-sanitize pipeline: rewrite relative images/links, add heading
// anchors + TOC, extract mermaid. Operates on the sanitized DOM, so it is
// identical for markdown-sourced and html-sourced docs.
function postProcess(clean, { repoId, path, owner, name, branch }) {
  const baseDir = dirname(path)
  const tpl = document.createElement('template')
  tpl.innerHTML = clean
  // ... existing image rewrite, link rewrite, heading/TOC, mermaid blocks ...
  return { html: tpl.innerHTML, toc, hasMermaid }
}
```

Inside the moved link-rewrite loop, widen the in-app navigation test so HTML docs link to each other in-app instead of bouncing to GitHub:

```js
    if (/\.(md|markdown|html?)$/i.test(resolved)) {
```

Then the two entry points:

```js
export function renderMarkdown(markdown, ctx) {
  return postProcess(DOMPurify.sanitize(md.render(markdown || ''), SANITIZE), ctx)
}

// An HTML doc is the same pipeline minus markdown-it. DOMPurify's html profile
// drops <script>, event handlers and javascript: URLs, and flattens a
// standalone document's <html>/<head>/<body> wrapper — so the doc body renders
// inside .markdown-body and inherits site typography, which is the intent.
export function renderHtml(source, ctx) {
  return postProcess(DOMPurify.sanitize(source || '', SANITIZE), ctx)
}
```

Update the file's header comment: it currently opens `// Markdown -> { html (sanitized), toc, hasMermaid }.` — make it `// Markdown or HTML -> { html (sanitized), toc, hasMermaid }.` and add a line noting `<style>` is stripped from both.

- [ ] **Step 2: Switch renderer by extension in the view**

In `docs-ui/src/components/DocView.vue`, change the import:

```js
import { renderMarkdown, renderHtml } from '../markdown.js'
```

and inside `load()`, replace the `renderMarkdown(...)` call:

```js
    const data = await api.getFile(repoId, filePath, useRef)
    const render = /\.html?$/i.test(filePath) ? renderHtml : renderMarkdown
    const result = render(data.content, {
      repoId,
      path: filePath,
      owner: r?.owner,
      name: r?.name,
      branch: useRef,
    })
```

Nothing else in the component changes — `toc`, `scrollToId`, `onClick`, `downloadPdf` and `renderMermaid` all consume `postProcess`'s output unchanged. `tree.js`, `DocTreeNode.vue` and `Sidebar.vue` need no edit: they render `node.name` verbatim, so `handbook.html` lists and sorts like any other file.

- [ ] **Step 3: Verify the build is clean**

```bash
cd docs-ui && npm run build
```

Expected: build succeeds, no unresolved import warnings for `renderHtml`.

- [ ] **Step 4: Verify by hand in the browser**

There is no JS test harness, so this step IS the test for Steps 1-2. Put a real HTML doc in a repo the dev stack can reach (`docs/handbook.html` on a branch of any registered repo) containing: an `<h1>` plus two `<h2>`s, a relative `<img src="images/x.png">`, a link to a sibling `.md` doc, an inline `style="color:red"` span, and a `<style>body{background:red}</style>` block.

```bash
make dev                      # api on :8080
cd docs-ui && npm run dev     # http://localhost:5173
```

Check, and record the result in your report:
1. The file appears in the repo's sidebar tree.
2. It renders, and the "On this page" TOC lists both `<h2>`s.
3. The relative image loads (through `/repo-docs/.../raw`).
4. The link to the `.md` doc navigates in-app (no page reload, no GitHub tab).
5. The inline `style="color:red"` span is red.
6. **The sidebar is NOT red** — the `<style>` block was stripped.
7. "Download PDF" opens the print dialog with the doc formatted for paper.

- [ ] **Step 5: Update `docs-ui/README.md`**

Three edits, all in prose that the change falsifies:
- Intro: `browse the Markdown docs of every repo` → `browse the docs of every repo`; `list a repo's \`.md\` tree` → `list a repo's doc tree`.
- Features bullet 1: `expand a repo to lazy-load its \`.md\` tree` → `expand a repo to lazy-load its doc tree`.
- Features bullet 3: after the existing `Markdown rendered with markdown-it…` bullet, add:

```markdown
- `.html` / `.htm` docs inside a `docs/` folder are rendered through the same
  sanitized pipeline (no markdown-it step). A doc's `<style>` block is stripped
  so it cannot restyle the site; inline `style="…"` is kept.
```

- [ ] **Step 6: Stage**

```bash
git add docs-ui/src/markdown.js docs-ui/src/components/DocView.vue docs-ui/README.md
```

**Do not commit.**

---

### Task 4: Record the change and archive the spec + plan

**Files:**
- Modify: `HANDOFF.md` (new addendum at the end)
- Move: `docs/superpowers/specs/2026-08-31-docs-site-html-rendering-design.md` → `docs/superpowers/specs/archive/`
- Move: `docs/superpowers/plans/2026-08-31-docs-site-html-rendering.md` → `docs/superpowers/plans/archive/`

**Interfaces:**
- Consumes: the finished state of Tasks 1-3.
- Produces: nothing code-facing.

- [ ] **Step 1: Append a HANDOFF.md addendum**

Follow the shape of the existing `## Addendum 2026-08-07` section:

```markdown
## Addendum 2026-08-31 — docs site renders `.html` docs

**Status: implemented** (spec
`docs/superpowers/specs/archive/2026-08-31-docs-site-html-rendering-design.md`,
plan `docs/superpowers/plans/archive/2026-08-31-docs-site-html-rendering.md`,
both archived on completion). `reva/repo_docs.py::browser_in_scope` widens the
docs browser to `.html`/`.htm` inside a `docs/` folder — the repo root's or an
addon's own. `in_scope` is unchanged, so the ticket-analysis grounding index
still sees markdown only: no `_SCOPE_VERSION` bump, no re-index, no migration.

The SPA renders HTML through the same DOMPurify-sanitized pipeline as markdown
(`renderHtml` in `docs-ui/src/markdown.js`), with `<style>` stripped so a doc
cannot restyle the site. `.html` is deliberately still refused by `/raw` — it is
never served as a document from our origin.

**Deploy:** nginx must be rebuilt for the SPA change
(`docker compose -f docker-compose.prod.yml build nginx && … up -d nginx`),
plus the usual api redeploy for the `/repo-docs` change. Frontend behavior is
hand-verified only — `docs-ui` has no JS test harness.
```

- [ ] **Step 2: Archive the spec and plan**

```bash
git mv docs/superpowers/specs/2026-08-31-docs-site-html-rendering-design.md docs/superpowers/specs/archive/
git mv docs/superpowers/plans/2026-08-31-docs-site-html-rendering.md docs/superpowers/plans/archive/
```

Set the spec's `- **Status:**` line to `implemented 2026-08-31. Unit-tested (scope + API); frontend hand-verified only; not live-validated.`

- [ ] **Step 3: Final gate and stage**

```bash
make test
ruff check reva worker/worker api/app scheduler/scheduler
cd docs-ui && npm run build
git add -A HANDOFF.md docs/superpowers
git status
```

Expected: three Python suites green, ruff clean, SPA build clean. Report the staged file list to Joseph and stop — **he commits.**
