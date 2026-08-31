# Docs site renders `.html` docs alongside `.md` — Design

- **Date:** 2026-08-31
- **Status:** implemented 2026-08-31, staged but NOT committed. Unit-tested: scope predicate (worker) and `/repo-docs` endpoints (api). The SPA change is verified only by a clean `npm run build` — the 7-point browser check in the plan's Task 3 Step 4 was NOT performed and is owed. Not deployed, not live-validated.
- **Context:** requested by Joseph 2026-08-31. The consultant docs browser
  (`docs-ui` SPA at `/docs`, backed by `/repo-docs`) serves Markdown only:
  `/tree` filters on `reva/repo_docs.py::in_scope`, `/file` 415s anything
  outside `DOC_EXTENSIONS`, and `docs-ui/src/markdown.js` runs the content
  through markdown-it. A consultant doc written as HTML is invisible on the
  site. Joseph wants those rendered too.

## Why this is small

`markdown-it` is already configured with `html: true`, so the SPA renders and
sanitizes doc-authored HTML today — a `.md` file may contain arbitrary markup
and it reaches the page through DOMPurify. Everything in `renderMarkdown` after
the sanitize step (relative-image rewrite, link rewrite, heading anchors, TOC,
mermaid extraction) operates on a DOM template, not on Markdown. An `.html`
file is therefore the existing pipeline with the markdown-it step removed. The
trust boundary does not move; the renderer's input format does.

## Decisions taken during brainstorming

- **HTML only inside a `docs/` folder** — the repo root's `docs/`, or an
  addon's own `custom_addons/<addon>/docs/`. Deliberately narrower than the
  Markdown scope, which reaches all of `custom_addons/**`. Every Odoo addon
  ships a manifest stub at `custom_addons/<addon>/static/description/index.html`
  (a ~110-byte `<h1>addon name</h1>`; four exist across the local customer repos
  today). A blanket `.html` widening would put one of those in the tree for
  every addon in every repo. Requiring a `docs/` segment excludes them without a
  `static/` special case.
- **Browser-only; the grounding index never sees HTML.** `in_scope` stays the
  Markdown-only predicate that `sync_repo_docs` applies, and the docs API gets
  its own wider predicate. Rationale: `split_markdown_sections` splits on ATX
  `#` headings, which an HTML file has none of — the whole file would land as
  one section titled by the file stem, truncated to 2000 chars of tag soup, then
  compete in `search_repo_docs`'s `ts_rank` against real prose. Keeping the
  split also means **no `_SCOPE_VERSION` bump and no forced re-index** of every
  repo (contrast the 2026-08-07 root-`docs/` change, which needed migration 045
  for exactly that reason).
- **`<style>` stripped, `style="…"` kept.** A doc's `<style>` block emits global
  CSS rules into the SPA's own page and can restyle or overlay it; inline
  attributes cannot escape their element, and are what makes an exported HTML
  table still look like a table.
- **`.html` is never served through `/raw`.** `/raw` returns bytes with a
  guessed `Content-Type`; serving repo HTML from the site's own origin would be
  stored XSS. `ASSET_EXTENSIONS` is unchanged and `/raw` keeps 415ing `.html`.

## Design

1. **Browser scope (`reva/repo_docs.py`).** Add a browser-only extension tuple
   and predicate beside the existing ones:

   ```python
   BROWSER_DOC_EXTENSIONS = (".html", ".htm")

   def browser_in_scope(path: str) -> bool:
       """True for anything the consultant docs browser serves as text: every
       Markdown doc `in_scope` covers, plus HTML inside a docs/ folder."""
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

   `SCOPE_PREFIXES` already anchors both `custom_addons/` and the repo-root
   `docs/`, and `EXCLUDED_SEGMENTS` already carries `superpowers`, so
   `docs/superpowers/notes.html` is rejected on the same rule as its `.md`
   sibling. `in_scope`, `DOC_EXTENSIONS`, `doc_priority` and `_SCOPE_VERSION`
   are untouched — the grounding path keeps its exact current behavior. Update
   the module docstring, which currently says `in_scope` is *the* single
   definition of the repo's docs: it becomes the definition of the *grounded*
   docs, with `browser_in_scope` the wider browser surface.

2. **API (`api/app/routes/docs.py`).** Import `BROWSER_DOC_EXTENSIONS` and
   `browser_in_scope` in place of `DOC_EXTENSIONS`/`in_scope`. `_cached_tree`'s
   entry filter becomes `browser_in_scope(e["path"])`; the `/file` gate keeps
   markdown's pre-existing extension-only check (`DOC_EXTENSIONS`, unrestricted
   by folder — unchanged behavior for existing callers) and additionally
   requires HTML to satisfy `browser_in_scope`:

   ```python
   is_html = safe.lower().endswith(BROWSER_DOC_EXTENSIONS)
   if not (safe.lower().endswith(DOC_EXTENSIONS) or (is_html and browser_in_scope(safe))):
       raise HTTPException(status_code=415, detail="Only doc files are served as text")
   ```

   An extension-only gate
   (`safe.lower().endswith(DOC_EXTENSIONS + BROWSER_DOC_EXTENSIONS)`) was tried
   first and rejected: it let `custom_addons/<addon>/static/description/index.html`
   through to a 404 instead of the 415 the scope rule above requires. The 415
   detail is reworded from "Only Markdown files are served as text" to "Only
   doc files are served as text". `/raw`, `/branches`, `/repos` and the caches
   are unchanged. Update the file's scope comment.

3. **Renderer (`docs-ui/src/markdown.js`).** Extract everything after the
   sanitize call into `postProcess(cleanHtml, ctx) -> { html, toc, hasMermaid }`
   — the image rewrite, link rewrite, heading anchors/TOC and mermaid
   extraction, moved verbatim. Then:

   - `renderMarkdown(markdown, ctx)` = `md.render` → `sanitize` → `postProcess`.
   - `renderHtml(source, ctx)` = `sanitize` → `postProcess`.

   Both call one shared sanitize helper with
   `{ USE_PROFILES: { html: true }, FORBID_TAGS: ['style'] }`. DOMPurify's html
   profile already drops `<script>`, event handlers and `javascript:` URLs;
   `<iframe>`/`<object>`/`<embed>` are outside the profile and need no explicit
   forbid. A standalone document's `<html>`/`<head>`/`<body>` wrapper is
   flattened by the sanitizer, so the doc body renders inside `.markdown-body`
   and inherits site typography — which is the intent, not a limitation.

   Extend the in-app link test from `/\.(md|markdown)$/i` to
   `/\.(md|markdown|html?)$/i` so doc-to-doc links between HTML pages navigate
   in-app instead of bouncing to GitHub.

4. **View (`docs-ui/src/components/DocView.vue`).** Pick the renderer by
   extension in `load()`:

   ```js
   const render = /\.html?$/i.test(filePath) ? renderHtml : renderMarkdown
   const result = render(data.content, { repoId, path: filePath, owner, name, branch })
   ```

   Nothing else changes: the TOC, anchor scroll, `View on GitHub`, print-to-PDF
   and the mermaid pass all consume `postProcess`'s output and work unchanged.
   `tree.js`, `DocTreeNode.vue` and `Sidebar.vue` need no edit — they render
   `node.name` verbatim, so `handbook.html` lists and sorts like any other file.

## What is knowingly left as-is

- **Search snippets.** `doc_search` substring-matches raw file content, so an
  HTML doc can match on markup (`class`, `href`) and `_snippet` can return a
  line of tags. Acceptable: HTML docs are a small minority of the corpus and the
  path hit still navigates correctly. Stripping tags for the snippet is a
  follow-up if it proves annoying in practice.
- **No syntax highlighting inside HTML docs.** `hljs` runs from markdown-it's
  `highlight` hook, which the HTML path skips. A `<pre>` in an HTML doc renders
  unhighlighted.
- **`custom_addons/<addon>/static/docs/x.html` would pass scope.** It carries a
  `docs` segment. Implausible in an Odoo addon and not worth a second exclusion.

## Testing

- `worker/tests/test_repo_docs.py` — extend the scope table so `in_scope`
  **rejects** `.html` at every location (this assertion is the grounding
  boundary, not an incidental case), and add a `browser_in_scope` table:
  `docs/handbook.html` and `custom_addons/cu_x/docs/spec.html` accepted;
  `custom_addons/cu_x/static/description/index.html`,
  `custom_addons/cu_x/README.html` (no `docs/` segment) and
  `docs/superpowers/plan.html` rejected; `.htm` and uppercase `.HTML` accepted;
  every existing Markdown case still accepted.
- `api/tests/test_docs.py` — `/tree` includes an in-scope `.html` and still
  excludes the manifest stub; `/file?path=docs/handbook.html` returns 200 with
  the raw source; `/file` still 415s `.py`; `/raw?path=docs/handbook.html`
  still 415s.
- `docs-ui` has no JS test harness (no vitest/jest in `package.json`), so items
  3 and 4 are verified by hand: `npm run build` clean, then a local
  `npm run dev` against the dev api rendering a real HTML doc — checking the
  TOC, a doc-to-doc link, a relative image, and that a `<style>` block in the
  doc does not restyle the sidebar.
- Full gate: `make test` (shared `reva/` change touches all three services) plus
  `ruff check reva worker/worker api/app scheduler/scheduler`. No `tui/` change,
  no contract change, no migration.
