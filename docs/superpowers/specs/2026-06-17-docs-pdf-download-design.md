# Docs app — "Download PDF" button (print-to-PDF)

**Date:** 2026-06-17
**Component:** `docs-ui/` (Vue 3 SPA served at `/docs/`)
**Status:** Design approved, pending implementation plan

## Problem

Consultants browsing a repo's Markdown docs in the REVA docs app (`docs-ui/`)
have no way to take a doc offline or share it as a self-contained file. They
want a one-click **Download PDF** for the currently open document.

## Context

The docs app renders Markdown entirely **client-side**: `src/markdown.js` runs
`markdown-it` → DOMPurify → DOM rewrites; `src/components/DocView.vue` injects the
result via `v-html` into `.markdown-body` and lazily renders ` ```mermaid ` blocks
to SVG (mermaid initialized with `theme: 'dark'`, `DocView.vue:30`). Code is
highlighted with `highlight.js` using a dark on-screen theme. The backend
`/repo-docs` API only returns raw Markdown text — it never produces HTML. The SPA
is built static into the nginx image; auth is at the Cloudflare edge.

Because the fully rendered document (including mermaid SVG) already lives in the
browser DOM, the cheapest faithful path to a PDF is the browser's own
print-to-PDF, driven by a print stylesheet. This adds **no dependency**, **no
backend change**, and no new render pipeline to maintain.

Rejected alternatives:
- **Client-side PDF lib** (`html2pdf.js`/jsPDF+html2canvas): true one-click
  download, but rasterizes text (non-selectable, large files) and breaks awkwardly
  across pages. Adds bundle weight.
- **Server-side render** (WeasyPrint / headless Chromium): highest fidelity, but
  duplicates the client render pipeline in Python and needs a mermaid-capable
  renderer. Heavy infra for marginal benefit here.

## Design

### UX

- A **`Download PDF`** button is added to the doc crumbs bar in
  `DocView.vue`, alongside the existing `View on GitHub ↗` link. It is part of the
  `v-if="repo"` crumbs row, so it only shows when a doc is loaded.
- Clicking it sets `document.title` to `<repo.full_name> — <path>` (so the browser's
  "Save as PDF" suggests a meaningful filename), calls `window.print()`, and
  restores the previous `document.title` on the `afterprint` event.
- The handler is registered/cleaned up with the component lifecycle; the
  `afterprint` listener is removed on unmount to avoid leaks.

### Print stylesheet (`src/style.css`, new `@media print` block)

Reformat for clean light paper:

- White background, dark text on `body`/`.content`/`.markdown-body`.
- **Hide** everything that is not the document: `.sidebar`, `.search`, `.brand`/
  `.kbd-hint`, the crumbs chrome (`.crumb-branch`, the `.gh` GitHub link, the new
  Download button itself), the `.toc` nav, and the `.heading-anchor` `#` marks.
- Keep a **print-only header** identifying the doc — repo · path · branch — shown
  only in print (display:none on screen, visible in `@media print`). Implemented
  as a dedicated element in the crumbs area marked print-only.
- **Code blocks:** give `.markdown-body pre`/`code` a light background + dark text
  for print (on-screen colors are tuned for dark bg). Allow page breaks between
  blocks but avoid breaking inside a `pre` where practical
  (`break-inside: avoid` as a best-effort hint).
- **Mermaid (approach (a), framed as-is):** the dark-themed SVG is kept; in print
  `.markdown-body .mermaid` gets a white bordered card with padding so the diagram
  reads as a self-contained framed box on the page. No re-render, no async risk.
- Let the content use the full page width in print (drop the on-screen
  `max-width: 820px` centering / large side padding on `.doc`).

### Components / boundaries

- **`DocView.vue`** — owns the button, the click handler, and the title swap.
  Pure presentation; no API or store changes.
- **`style.css`** — owns all print formatting in one appended `@media print`
  block. No existing on-screen rules are modified (surgical: print rules are
  additive and scoped to the print media query).

No changes to `markdown.js`, `api.js`, `store.js`, the `/repo-docs` API, or any
Python service.

## Error handling

Minimal surface: `window.print()` and the `document.title` swap don't throw in
practice. The `afterprint` restore is best-effort; if `afterprint` never fires
(rare), the title is also restored defensively. A browser that blocks the print
dialog simply does nothing — no error state to surface.

## Testing / verification

`docs-ui` has **no JS test harness** today (no test runner in `package.json`), so
this is verified **manually**, and that limitation is stated honestly:

1. `cd docs-ui && npm run dev`, open a doc, click **Download PDF** → browser print
   dialog opens; "Save as PDF" produces a clean light-paper PDF.
2. Confirm in print preview: sidebar/filter/crumbs-chrome/TOC/heading-anchors are
   hidden; the print-only header (repo · path · branch) shows; code blocks are
   light and legible; a mermaid diagram appears framed on a white card.
3. Confirm the suggested PDF filename reflects repo + path, and that the on-screen
   `document.title` is restored after the dialog closes.
4. Regression: on-screen appearance is unchanged (print rules live only inside
   `@media print`).

No backend tests change. `docs/` reference: update `docs-ui/README.md` "Features"
list to mention Download PDF (and drop the stale "Not yet" entries already shipped
if encountered — full-text search and scroll-to-heading are now implemented).

## Out of scope

- Multi-doc / whole-repo PDF export.
- True one-click download without the browser print dialog.
- Light-theme re-render of mermaid diagrams (approach (b)).
- Server-side PDF generation.
