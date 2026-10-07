# Unified Site Design Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One visual design for every consultant-facing REVA page: the status page and how-it-works page lead, the docs browser (Vue SPA, incl. Internal modules) follows, and a new static "Backups" page joins the shared nav.

**Architecture:** A shared stylesheet `docs-ui/src/reva.css` holds the tokens (light first, dark via `prefers-color-scheme`) and the base components (header + nav, panel, table wrap, pill, inline code, button, field, note). `npm run build:theme` copies it to `api/app/static/reva.css` (committed, like `reva/static/release-log.css` already is); the api serves it at `/reviews/reva.css` for the three static pages, and the SPA imports it directly. The SPA keeps its layout and scroll model (sidebar + content column, DocView scrollspy untouched) and swaps its dark-only Cloudunify palette for the shared tokens. Mockup approved by Joseph 2026-10-07 (artifact "REVA Unified Design").

**Tech Stack:** plain CSS, Vue 3 + Vite (`docs-ui/`), FastAPI `FileResponse` routes (`api/app/routes/budget_status.py`), highlight.js theme switch via media-scoped `@import`, mermaid theme via `matchMedia`.

**Spec:** no separate spec (bounded change, design approved in chat). Decisions: technical backup content stays repo-only (`docs/odoo-sh-backup-technical.md` kept); the consultant markdown becomes the HTML page and is deleted; the Cloudunify mark stays only as favicon; no theme toggle, the system preference decides, like the static pages.

## Global Constraints

- Tokens are those of `api/app/static/reviews.html` + `how-it-works.html` today with ONE change, the accent, which Joseph picked from seven candidates on 2026-10-07: **navy** `--accent #1f3f7a`, `--accent-soft #e2e8f4` (light) and `--accent #8fa9dd`, `--accent-soft #1f2a44` (dark). Neutrals and status colours unchanged (`--bg #f5f6f8`, `--surface #fff`, `--line #dde1e7`, `--ink #1d2430`, `--muted #64707f`, `--ok #2e7d4f`, `--ok-soft #e3f3e8`, `--wait #b7791f`, `--wait-soft #fbf1dc`, `--over #b23a3a`, `--over-soft #f9e3e3`, `--track #e8ebf0`; dark: `#14181e #1c2229 #2c343e #e6eaef #98a3b1 … #6cc08b #1e3326 #e0a94a #3a2f16 #e57373 #3f2222 #2a323c`). The static pages pick the new accent up through the shared file; no page keeps a local `--accent`. Fonts `system-ui,-apple-system,"Segoe UI",sans-serif` and `ui-monospace,Menlo,monospace`. No Inter, no orange, no `#2878c0`.
- The nav on every page, in this order: **Docs** (`/docs/`), **Status** (`/reviews/`), **How it works** (`/reviews/how-it-works`), **Internal modules** (`/docs/?page=internal-modules`), **Backups** (`/reviews/backups`). Header title pattern `REVA <span>/ …</span>`.
- `docs-ui/src/reva.css` is the only source; `api/app/static/reva.css` is a generated copy and must be byte-identical after `npm run build:theme`.
- The SPA's print stylesheet (`@media print`) keeps its current behaviour (light paper, sidebar hidden).
- The DocView scrollspy keeps scrolling inside `.content` (do not move scrolling to the window).
- `v-html` only on DOMPurify output, as before. No new auth surface: all new routes are under `/reviews/`, already gated by Cloudflare Access.
- Git: **stage only, never commit.** Joseph makes one commit. Every "Stage" step is `git add` of the task's files.
- Definition of done: `cd api && .venv/bin/python -m pytest tests/ -q` green, `worker/.venv/bin/ruff check api/app` clean, `cd docs-ui && npm run build:theme && npm run build` clean, and `git diff --quiet -- api/app/static/reva.css` after `build:theme` (the copy is up to date).

## Review Focus

1. **A long inline `code` span in markdown prose** must wrap, not overflow the doc column: the shared chip style sets `white-space: nowrap` (right for command tables), the SPA must override it inside `.markdown-body`. Pinned in Task 3 (`.markdown-body :not(pre) > code { white-space: normal }`, verified by build + reviewer reading).
2. **Code blocks in dark mode** must use the dark highlight theme and in light mode the light one; both `@import`s must be media-scoped, or the second wins everywhere. Pinned in Task 3 Step 3.
3. **The status page's legend table and the how-it-works `dl`** must keep their look after the duplicated rules move out: those element-level rules stay page-local. Pinned in Task 1 Step 4 (only the listed rules are removed).
4. **A phone-width docs page**: the sidebar is still a drawer, the header wraps, nothing scrolls horizontally. Pinned in Task 3 (media queries retained).
5. **`/reviews/reva.css` must be served with a CSS content type** or browsers ignore it under strict MIME checks. Pinned in Task 1 (`test_reva_css_served`).

---

### Task 1: Shared stylesheet, served to the static pages

**Files:**
- Create: `docs-ui/src/reva.css`
- Create (generated): `api/app/static/reva.css`
- Modify: `docs-ui/package.json` (`build:theme`)
- Modify: `api/app/routes/budget_status.py` (route for the css)
- Modify: `api/app/static/reviews.html`, `api/app/static/how-it-works.html` (link the css, drop duplicated rules, add classes, add the Backups nav link)
- Test: `api/tests/test_budget_status.py` (append)

**Interfaces:**
- Produces: `GET /reviews/reva.css` → `text/css`. Classes available to every page: `.wrap`, `.site-header`, `.site-nav`, `.label`, `.panel`, `.tablewrap`, `.pill(.ok|.wait|.over|.info)`, `.note`, `.btn`, `.copy`, `.field`, `.cmd`, and `:not(pre) > code`.

- [ ] **Step 1: Write the failing api tests**

Append to `api/tests/test_budget_status.py` (mirror the file's existing how-it-works test for the client fixture it uses):

```python
def test_reva_css_served(client):
    r = client.get("/reviews/reva.css")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/css")
    assert "--accent:" in r.text


def test_static_pages_link_shared_css_and_backups(client):
    for path in ("/reviews/", "/reviews/how-it-works"):
        body = client.get(path).text
        assert '<link rel="stylesheet" href="reva.css">' in body
        assert 'href="backups"' in body
```

If the file's existing tests build the `TestClient` differently (no `client` fixture), copy that pattern instead of inventing a fixture.

Run: `cd api && .venv/bin/python -m pytest tests/test_budget_status.py -k "reva_css or shared_css" -v`
Expected: FAIL (404 on the css, missing link in the pages).

- [ ] **Step 2: Create `docs-ui/src/reva.css`**

```css
/* REVA consultant site — the shared look.
   Source of truth: docs-ui/src/reva.css. `npm run build:theme` copies it to
   api/app/static/reva.css, which the static pages (/reviews/, /reviews/how-it-works,
   /reviews/backups) link as reva.css; the docs SPA imports it directly.
   Light first; dark follows the system setting. Element-level rules for h2, bare
   tables and dl stay page-local on purpose — the SPA renders markdown, where those
   elements must keep their document meaning. */

:root {
  --bg: #f5f6f8; --surface: #fff; --line: #dde1e7; --ink: #1d2430; --muted: #64707f;
  --accent: #1f3f7a; --accent-soft: #e2e8f4;
  --ok: #2e7d4f; --ok-soft: #e3f3e8; --wait: #b7791f; --wait-soft: #fbf1dc;
  --over: #b23a3a; --over-soft: #f9e3e3; --track: #e8ebf0;
  --sans: system-ui, -apple-system, "Segoe UI", sans-serif;
  --mono: ui-monospace, Menlo, monospace;
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --bg: #14181e; --surface: #1c2229; --line: #2c343e; --ink: #e6eaef; --muted: #98a3b1;
    --accent: #8fa9dd; --accent-soft: #1f2a44;
    --ok: #6cc08b; --ok-soft: #1e3326; --wait: #e0a94a; --wait-soft: #3a2f16;
    --over: #e57373; --over-soft: #3f2222; --track: #2a323c;
  }
}

body { margin: 0; background: var(--bg); color: var(--ink); font-family: var(--sans); font-size: 14px; line-height: 1.55; }
.wrap { max-width: 1080px; margin: 0 auto; display: grid; gap: 26px; padding: 0 16px 56px; }

/* Header + nav, identical on every page. */
.site-header { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 8px 24px; padding-block: 22px 14px; border-bottom: 1px solid var(--line); }
.site-header h1 { font-size: 20px; font-weight: 600; margin: 0; }
.site-header h1 span { color: var(--muted); font-weight: 400; }
.site-header .meta { color: var(--muted); font-size: 13px; }
.site-nav a { color: var(--accent); text-decoration: none; margin-left: 16px; font-size: 13px; }
.site-nav a.active { font-weight: 600; border-bottom: 2px solid var(--accent); }

/* Components. */
.label { font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); margin: 0 0 10px; }
.panel { background: var(--surface); border: 1px solid var(--line); border-radius: 6px; padding: 14px 16px; }
.panel h3 { font-size: 14px; margin: 0 0 8px; }
.panel p { margin: 0 0 8px; max-width: 62ch; }
.panel p:last-child { margin: 0; }
:not(pre) > code, .cmd { font-family: var(--mono); font-size: 12.5px; background: var(--accent-soft); color: var(--accent); padding: 1px 6px; border-radius: 3px; white-space: nowrap; }
.tablewrap { overflow-x: auto; background: var(--surface); border: 1px solid var(--line); border-radius: 6px; }
.tablewrap table { border-collapse: collapse; width: 100%; }
.tablewrap th, .tablewrap td { text-align: left; padding: 9px 14px; border-top: 1px solid var(--line); vertical-align: top; }
.tablewrap th { border-top: 0; font-size: 11.5px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); font-weight: 600; white-space: nowrap; }
.pill { display: inline-block; font-size: 11px; font-weight: 600; letter-spacing: .04em; text-transform: uppercase; padding: 1px 7px; border-radius: 3px; }
.pill.ok { background: var(--ok-soft); color: var(--ok); }
.pill.wait { background: var(--wait-soft); color: var(--wait); }
.pill.over { background: var(--over-soft); color: var(--over); }
.pill.info { background: var(--accent-soft); color: var(--accent); }
.note { color: var(--muted); font-size: 12.5px; }
.btn { font: inherit; font-size: 12px; padding: 3px 9px; border: 1px solid var(--line); border-radius: 4px; background: var(--surface); color: var(--accent); cursor: pointer; white-space: nowrap; }
.btn:hover { border-color: var(--accent); }
.btn:focus-visible { outline: 2px solid var(--accent); }
.copy { position: absolute; top: 10px; right: 10px; font: inherit; font-size: 12px; padding: 3px 9px; border: 1px solid var(--line); border-radius: 4px; background: var(--surface); color: var(--accent); cursor: pointer; }
.copy:focus-visible { outline: 2px solid var(--accent); }
.field { font: inherit; padding: 6px 10px; border: 1px solid var(--line); border-radius: 4px; background: var(--surface); color: var(--ink); }
.field:focus { outline: none; border-color: var(--accent); }
.field::placeholder { color: var(--muted); }
```

- [ ] **Step 3: Copy script and generated file**

In `docs-ui/package.json` change `build:theme` to:

```json
"build:theme": "sass src/release-log.scss ../reva/static/release-log.css --no-source-map && cp src/reva.css ../api/app/static/reva.css"
```

Run `cd docs-ui && npm run build:theme`. `api/app/static/reva.css` now exists and is identical to the source.

- [ ] **Step 4: Static pages link the css and lose the duplicated rules**

In **both** `api/app/static/reviews.html` and `api/app/static/how-it-works.html`:

1. Directly before `<style>`, add `<link rel="stylesheet" href="reva.css">` (relative: both pages live under `/reviews/`).
2. Delete these rules from the inline `<style>` (they are now shared): the `:root { … }` block and its dark `@media` block; `body { … }`; `.wrap { … }`; `header { … }`, `header h1 …` / `h1 { … }` and `h1 span`; `nav a …` and `nav a.active`; `.panel …` and `.panel h3`, `.panel p` (how-it-works only; reviews keeps its own `.panel h3 { … display:flex … }` and `.panel h3 small`); `code, .cmd { … }` (how-it-works); `.tablewrap { … }`; `.pill { … }` and the four `.pill.*` colour rules; `.note { … }` (how-it-works; reviews keeps its `.note { …; margin:8px 0 0 }` since the margin differs); `.copy …` (how-it-works).
3. Keep everything else page-local, explicitly including: `h2 { … }`, `table { … min-width:560px }`, `th, td { … }`, `th { … }`, `td.k`, `dl/dt/dd`, `pre.yaml`, `.tldr`, `ol.flow`, `.meta`, `.budgets`, `.meter`, `.num`, `.track`, `.fill`, `.repo …`, `.chev`, `.title`, `.right`, `td.mono`, `tr.stripe`, `.empty`, `.count`, `.err`, `.banner`, `.status-*`, `#author`, `details.legend`, both pages' `@media` rules. In `reviews.html`, change `#author { … }` to keep its width/margin but add `class="field"` on the input and drop the duplicated font/padding/border/background/color declarations.
4. Add the classes: `<header class="site-header">` and `<nav class="site-nav">` in both files.
5. Add the Backups link as the last nav entry in both files: `<a href="backups">Backups</a>`.

Open each file once more and confirm no `--accent` definition remains inline and the `<link>` precedes `<style>` (page-local rules must win on equal specificity).

- [ ] **Step 5: Serve the css**

In `api/app/routes/budget_status.py`:

```python
_CSS = Path(__file__).resolve().parent.parent / "static" / "reva.css"


@router.get("/reva.css", include_in_schema=False)
def shared_css() -> FileResponse:
    """The shared look of every consultant page (generated from docs-ui/src/reva.css)."""
    return FileResponse(_CSS, media_type="text/css; charset=utf-8",
                        headers={"Cache-Control": "public, max-age=300"})
```

- [ ] **Step 6: Run the tests**

Run: `cd api && .venv/bin/python -m pytest tests/test_budget_status.py -v && cd .. && worker/.venv/bin/ruff check api/app`
Expected: PASS, ruff clean.

- [ ] **Step 7: Stage**

```bash
git add docs-ui/src/reva.css api/app/static/reva.css docs-ui/package.json api/app/routes/budget_status.py api/app/static/reviews.html api/app/static/how-it-works.html api/tests/test_budget_status.py
```

---

### Task 2: The Backups page

**Files:**
- Create: `api/app/static/backups.html`
- Modify: `api/app/routes/budget_status.py` (route)
- Delete: `docs/odoo-sh-backup-consultant.md`
- Modify: `docs/odoo-sh-backup-technical.md` (first paragraph's link)
- Test: `api/tests/test_budget_status.py` (append)

**Interfaces:**
- Produces: `GET /reviews/backups` → the page, `Cache-Control: no-store` like the other two.

- [ ] **Step 1: Write the failing test**

```python
def test_backups_page_served(client):
    r = client.get("/reviews/backups")
    assert r.status_code == 200
    assert "CU-odoo-sh-backup" in r.text
    assert '<link rel="stylesheet" href="reva.css">' in r.text
```

Run: `cd api && .venv/bin/python -m pytest tests/test_budget_status.py -k backups -v` → FAIL (404).

- [ ] **Step 2: Write `api/app/static/backups.html`**

Content comes from `docs/odoo-sh-backup-consultant.md` verbatim in substance; the page uses the how-it-works idiom (TL;DR panel, section labels, panels in a grid, a table, a dl):

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>REVA / Odoo.sh backups</title>
<link rel="stylesheet" href="reva.css">
<style>
  h2 { font-size:12px; font-weight:600; text-transform:uppercase; letter-spacing:.08em; color:var(--muted); margin:0 0 10px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:16px; }
  .tldr { font-size:15px; max-width:70ch; } .tldr b { font-weight:600; }
  ol.flow { margin:0; padding-left:20px; display:grid; gap:6px; max-width:66ch; }
  table { border-collapse:collapse; width:100%; min-width:560px; }
  th, td { text-align:left; padding:9px 14px; border-top:1px solid var(--line); vertical-align:top; }
  th { border-top:0; font-size:11.5px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); font-weight:600; }
  td.k { white-space:nowrap; }
  dl { display:grid; grid-template-columns:max-content 1fr; gap:6px 14px; margin:0; } dt { font-weight:600; white-space:nowrap; } dd { margin:0; max-width:60ch; }
  @media (max-width:520px) { dl { grid-template-columns:1fr; } dt { margin-top:6px; } }
</style>
</head>
<body>
<div class="wrap">
  <header class="site-header">
    <h1>REVA <span>/ Odoo.sh backups</span></h1>
    <nav class="site-nav"><a href="/docs/">Docs</a><a href="./">Status</a><a href="how-it-works">How it works</a><a href="/docs/?page=internal-modules">Internal modules</a><a href="backups" class="active">Backups</a></nav>
  </header>

  <section>
    <h2>TL;DR</h2>
    <div class="panel tldr">
      <p><b>Every night we pull a copy of each customer's Odoo.sh production</b> (database and filestore) to our own backup server and store it encrypted on a Hetzner storage box. Retention: 7 daily, 4 weekly, 6 monthly snapshots. It is independent of Odoo.sh's own backups, so a customer can get data back from us even when the Odoo.sh project is gone.</p>
      <p>For that to work, the backup server needs SSH access to the customer's Odoo.sh project. Setting that up is the consultant's part. Everything else is done by tech.</p>
    </div>
  </section>

  <section>
    <h2>Your three steps · in the customer's Odoo.sh project</h2>
    <p class="note" style="margin:0 0 10px">A project admin is needed, which is usually us. If the customer administers the project, send them the steps.</p>
    <div class="grid">
      <div class="panel">
        <h3>1. Give us the domain</h3>
        <p>Send tech the project's Odoo.sh domain and the SSH command of the production branch.</p>
        <dl>
          <dt>Domain</dt><dd><code>&lt;project&gt;.odoo.com</code></dd>
          <dt>SSH command</dt><dd>Open the production branch in Odoo.sh and copy the command shown under "Shell" or "SSH". It looks like <code>ssh 12345678@&lt;project&gt;.odoo.com</code>. The number is the production build id; tech needs both the number and the host.</dd>
        </dl>
      </div>
      <div class="panel">
        <h3>2. Allow the IP</h3>
        <p>Odoo.sh project, Settings, Firewall: add <b>178.105.100.225</b> for SSH. Takes up to 5 minutes to apply.</p>
        <p class="note">Odoo.sh only accepts SSH from IPs that are allowed in the project's firewall.</p>
      </div>
      <div class="panel">
        <h3>3. Put in the user</h3>
        <p>Odoo.sh project, Settings, Collaborators: add the Odoo.sh user <b>CU-odoo-sh-backup</b> with access to the production branch.</p>
        <p class="note">This user belongs to us. Its profile holds the backup server's SSH key, so no key has to be exchanged per customer.</p>
      </div>
    </div>
  </section>

  <section>
    <h2>Hand over to tech</h2>
    <div class="panel">
      <p>Open a ticket for tech with:</p>
      <ol class="flow">
        <li>client short name (lowercase, used as the backup name, e.g. <code>pomberger</code>),</li>
        <li>the domain and the SSH command from step 1,</li>
        <li>confirmation that steps 2 and 3 are done.</li>
      </ol>
      <p style="margin-top:8px">Tech runs the onboarding on the backup server. It verifies the access, sets up the encrypted repository and does a first full backup right away. The client is onboarded when tech reports the first snapshot.</p>
    </div>
  </section>

  <section>
    <h2>How you know it keeps working</h2>
    <div class="tablewrap"><table>
      <thead><tr><th>Message at 00:45 UTC in the backup Google Chat space</th><th>Meaning</th><th>What to do</th></tr></thead>
      <tbody>
        <tr><td class="k"><span class="pill ok">green</span></td><td>Snapshot of tonight exists.</td><td>Nothing.</td></tr>
        <tr><td class="k"><span class="pill over">red</span></td><td>Something failed, with the reason.</td><td>To tech. The most common reason is that the IP was removed from the firewall or the collaborator was removed from the project. Both are fixed by repeating steps 2 and 3.</td></tr>
        <tr><td class="k"><span class="pill wait">no message</span></td><td>The backup server or its timer is down.</td><td>To tech.</td></tr>
      </tbody>
    </table></div>
    <p class="note" style="margin:8px 0 0">One message per night, one line per client.</p>
  </section>

  <section>
    <h2>When a client leaves · Restoring data</h2>
    <div class="grid">
      <div class="panel">
        <h3>When a client leaves</h3>
        <p>Tell tech. They stop the nightly backup and ask you one question: keep the existing snapshots for a retention period, or delete them now. Then remove CU-odoo-sh-backup from the collaborators and the IP from the firewall in the Odoo.sh project.</p>
      </div>
      <div class="panel">
        <h3>Restoring data</h3>
        <p>Ask tech. They can restore any snapshot of the last 6 months as a database dump plus filestore.</p>
        <p class="note">What that involves on the server is documented for tech in the cu_reva repo (<code>docs/odoo-sh-backup-technical.md</code>).</p>
      </div>
    </div>
  </section>
</div>
</body>
</html>
```

- [ ] **Step 3: Route, markdown cleanup**

In `api/app/routes/budget_status.py`:

```python
_BACKUPS = Path(__file__).resolve().parent.parent / "static" / "backups.html"


@router.get("/backups", include_in_schema=False)
def backups() -> FileResponse:
    """Consultant page: the Odoo.sh backup onboarding steps and what the nightly message means."""
    return FileResponse(_BACKUPS, media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-store"})
```

`git rm docs/odoo-sh-backup-consultant.md`. In `docs/odoo-sh-backup-technical.md`, replace the first sentence `The consultant's part is on the [consultant page](odoo-sh-backup-consultant.md).` with `The consultant's part is the Backups page on the REVA site (/reviews/backups, source api/app/static/backups.html).`

- [ ] **Step 4: Run the tests**

Run: `cd api && .venv/bin/python -m pytest tests/test_budget_status.py -v && cd .. && worker/.venv/bin/ruff check api/app` → PASS, clean.

- [ ] **Step 5: Stage**

```bash
git add api/app/static/backups.html api/app/routes/budget_status.py docs/odoo-sh-backup-technical.md api/tests/test_budget_status.py
# docs/odoo-sh-backup-consultant.md is already staged as deleted by git rm
```

---

### Task 3: The docs SPA follows the shared look

**Files:**
- Modify: `docs-ui/src/main.js`
- Modify: `docs-ui/src/style.css` (full rewrite, below)
- Rename: `docs-ui/src/components/PageSwitch.vue` → `docs-ui/src/components/SiteHeader.vue` (new content below)
- Modify: `docs-ui/src/App.vue` (template + import)
- Modify: `docs-ui/src/components/DocView.vue` (mermaid theme, one line)
- Modify: `docs-ui/src/components/InternalModules.vue` (one class name, `table-wrap` → `tablewrap`)
- Verify: `cd docs-ui && npm run build`

**Interfaces:**
- Consumes: `docs-ui/src/reva.css` (Task 1).

- [ ] **Step 1: `main.js`**

```js
import { createApp } from 'vue'
import App from './App.vue'
import './reva.css'
import './style.css'
import './release-log.scss'

createApp(App).mount('#app')
```

(The highlight.js theme import moves into `style.css`, media-scoped.)

- [ ] **Step 2: `SiteHeader.vue`** (replaces `PageSwitch.vue`; `git mv` then rewrite)

```vue
<script setup>
import { route, goPage } from '../location.js'

// The header every consultant page shares (see docs-ui/src/reva.css). Status,
// How it works and Backups are static pages the api serves under /reviews/;
// Docs and Internal modules are this SPA's two pages.
const subtitle = () => (route.value.page === 'internal-modules' ? '/ internal modules' : '/ docs')
</script>

<template>
  <header class="site-header">
    <h1>REVA <span>{{ subtitle() }}</span></h1>
    <nav class="site-nav">
      <a href="./" :class="{ active: !route.page }" @click.prevent="goPage()">Docs</a>
      <a href="/reviews/">Status</a>
      <a href="/reviews/how-it-works">How it works</a>
      <a
        href="?page=internal-modules"
        :class="{ active: route.page === 'internal-modules' }"
        @click.prevent="goPage('internal-modules')"
        >Internal modules</a
      >
      <a href="/reviews/backups">Backups</a>
    </nav>
  </header>
</template>
```

- [ ] **Step 3: `style.css`** — replace the whole file with:

```css
/* Docs browser look. Tokens and shared components come from reva.css (imported
   first in main.js); this file holds only what the SPA has and the static
   pages do not: the sidebar + content layout, the markdown body, TOC, command
   palette, the Internal modules table, and print. */

@import 'highlight.js/styles/github.css' (prefers-color-scheme: light);
@import 'highlight.js/styles/github-dark.css' (prefers-color-scheme: dark);

:root { --sidebar-w: 280px; }
* { box-sizing: border-box; }
html, body, #app { height: 100%; margin: 0; }
a { color: var(--accent); text-decoration: none; }

/* App frame: header on top, then a sidebar column and a scrolling content column.
   The content column is the scroll container (DocView's scrollspy relies on it). */
.site { height: 100%; display: flex; flex-direction: column; padding-inline: 16px; }
.site > .site-header { flex: 0 0 auto; max-width: 1320px; width: 100%; margin: 0 auto; }
.layout { flex: 1; min-height: 0; display: flex; gap: 20px; max-width: 1320px; width: 100%; margin: 0 auto; padding-block: 16px 0; }

/* --- Sidebar --- */
.sidebar {
  width: var(--sidebar-w); flex: 0 0 var(--sidebar-w);
  background: var(--surface); border: 1px solid var(--line); border-radius: 6px;
  display: flex; flex-direction: column; overflow: hidden; align-self: stretch; margin-bottom: 16px;
}
.sidebar-top { display: flex; gap: 6px; align-items: center; padding: 10px 10px 0; }
.sidebar-top .search { flex: 1; min-width: 0; }
.search { font-size: 13px; width: 100%; }
.kbd-hint { font-family: var(--mono); font-size: 11px; padding: 2px 7px; }
.repos { overflow-y: auto; padding: 8px 6px 16px; flex: 1; }

.repo-name {
  width: 100%; text-align: left; background: none; border: none; padding: 5px 6px; font: inherit;
  font-size: 13px; font-weight: 600; cursor: pointer; color: var(--ink); display: flex; gap: 6px;
  align-items: baseline; border-radius: 4px;
}
.repo-name:hover { background: var(--track); }
.chev { color: var(--muted); width: 10px; flex: 0 0 10px; font-size: 10px; }
.repo-label { overflow-wrap: anywhere; }
.repo-count { margin-left: auto; font-size: 11px; font-weight: 500; color: var(--muted); background: var(--track); border-radius: 10px; padding: 0 7px; }
.repo-name.empty { color: var(--muted); font-weight: 500; }
.repo-name.empty .repo-count { background: none; }
.files { padding: 0 0 6px 0; }

.folder {
  width: 100%; text-align: left; background: none; border: none; font: inherit; font-size: 12.5px;
  color: var(--muted); cursor: pointer; display: flex; align-items: center; gap: 4px; padding: 4px 8px; border-radius: 4px;
}
.folder:hover { background: var(--track); color: var(--ink); }
.folder-name { overflow-wrap: anywhere; }
.file { display: block; padding: 4px 8px; font-size: 12.5px; color: var(--muted); border-radius: 4px; overflow-wrap: anywhere; }
.file:hover { background: var(--track); color: var(--ink); }
.file.active { background: var(--accent-soft); color: var(--accent); font-weight: 600; }
.hit-snippet { display: block; margin-top: 1px; font-size: 11.5px; line-height: 1.35; color: var(--muted); font-weight: 400; }
.hit-snippet mark { background: var(--wait-soft); color: var(--ink); border-radius: 2px; }

.repo-group {
  width: 100%; text-align: left; background: none; border: none; margin-top: 10px; padding: 5px 6px;
  font: inherit; font-size: 11px; letter-spacing: 0.05em; text-transform: uppercase; color: var(--muted);
  cursor: pointer; display: flex; gap: 6px; align-items: baseline; border-radius: 4px;
}
.repo-group:hover { color: var(--ink); }
.branch-row { display: flex; align-items: center; gap: 6px; padding: 2px 8px 8px 22px; }
.branch-ico { color: var(--muted); font-size: 12px; }
.branch-select { flex: 1; min-width: 0; font-size: 12px; padding: 3px 6px; cursor: pointer; }

.muted { color: var(--muted); font-size: 12.5px; padding: 4px 8px; }
.muted.warn { color: var(--wait); }
.error { color: var(--over); font-size: 12.5px; padding: 4px 8px; }

/* --- Content column --- */
.content { flex: 1; min-width: 0; overflow-y: auto; position: relative; padding-bottom: 32px; }
.backdrop { display: none; }
.sidebar-show { display: none; position: sticky; top: 0; margin: 0 0 -30px 0; z-index: 5; font-size: 14px; line-height: 1; padding: 5px 8px; }
.sidebar-hidden .sidebar { display: none; }
.sidebar-hidden .sidebar-show { display: inline-block; }
.sidebar-hidden .doc { margin-top: 0; }
.placeholder { height: 100%; display: flex; align-items: center; justify-content: center; color: var(--muted); }
.placeholder kbd { font-family: var(--mono); font-size: 12px; background: var(--surface); border: 1px solid var(--line); border-radius: 4px; padding: 1px 6px; }

.doc { background: var(--surface); border: 1px solid var(--line); border-radius: 6px; max-width: 860px; margin: 0 auto 16px; padding: 22px 28px 40px; }
.doc.has-toc { max-width: 1100px; }
.doc.has-toc .doc-grid { display: grid; grid-template-columns: minmax(0, 1fr) 220px; gap: 36px; align-items: start; }
.doc.has-toc .markdown-body { grid-column: 1; grid-row: 1; }
.doc.has-toc .markdown-body > :first-child { margin-top: 0; }

.crumbs { display: flex; align-items: center; gap: 8px; font-size: 12.5px; color: var(--muted); padding-bottom: 12px; margin-bottom: 20px; border-bottom: 1px solid var(--line); flex-wrap: wrap; }
.crumb-branch { background: var(--accent-soft); color: var(--accent); font-size: 11px; font-weight: 600; padding: 1px 7px; border-radius: 3px; }
.crumb-path { color: var(--ink); font-weight: 600; overflow-wrap: anywhere; }
.gh { margin-left: auto; white-space: nowrap; }
.pdf-btn { font: inherit; font-size: 12px; padding: 3px 9px; border: 1px solid var(--line); border-radius: 4px; background: var(--surface); color: var(--accent); cursor: pointer; white-space: nowrap; }
.pdf-btn:hover { border-color: var(--accent); }
.print-header { display: none; }

/* --- Markdown body --- */
.markdown-body { font-size: 14.5px; line-height: 1.65; color: var(--ink); }
.markdown-body h1, .markdown-body h2, .markdown-body h3, .markdown-body h4 { margin-top: 1.5em; margin-bottom: 0.5em; line-height: 1.3; font-weight: 600; color: var(--ink); }
.markdown-body h1 { font-size: 1.6em; }
.markdown-body h2 { font-size: 1.25em; }
.markdown-body h1, .markdown-body h2 { border-bottom: 1px solid var(--line); padding-bottom: 0.3em; }
.markdown-body a:hover { text-decoration: underline; }
.markdown-body img { max-width: 100%; border-radius: 6px; }
.markdown-body pre { position: relative; background: var(--bg); padding: 12px 14px; border-radius: 6px; overflow-x: auto; border: 1px solid var(--line); font-family: var(--mono); font-size: 12.5px; line-height: 1.5; }
.markdown-body pre code { background: none; color: inherit; padding: 0; font-size: inherit; white-space: pre; }
.markdown-body :not(pre) > code { white-space: normal; }   /* prose code wraps; the shared chip is for command tables */
.markdown-body blockquote { margin: 1em 0; padding: 4px 12px; color: var(--ink); border-left: 3px solid var(--accent); background: var(--accent-soft); border-radius: 0 4px 4px 0; }
.markdown-body table { border-collapse: collapse; margin: 1em 0; }
.markdown-body th, .markdown-body td { border: 1px solid var(--line); padding: 7px 13px; }
.markdown-body th { background: var(--bg); font-size: 11.5px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); }
.markdown-body hr { border: none; border-top: 1px solid var(--line); margin: 2em 0; }
.markdown-body .mermaid { margin: 1em 0; text-align: center; }

/* Heading anchors. */
.markdown-body :is(h1, h2, h3, h4) { position: relative; }
.heading-anchor { position: absolute; left: -0.9em; opacity: 0; color: var(--muted); font-weight: 400; }
.markdown-body :is(h1, h2, h3, h4):hover .heading-anchor { opacity: 1; }
.heading-anchor:hover { color: var(--accent); }
.heading-anchor.copied { opacity: 1; color: var(--accent); }
.heading-anchor.copied::after { content: "link copied"; position: absolute; left: 0; top: -1.5em; font-size: 11px; font-weight: 500; white-space: nowrap; color: var(--accent); }

/* Copy button on code blocks. */
.code-copy { position: absolute; top: 6px; right: 6px; opacity: 0; font: inherit; font-size: 11px; padding: 2px 8px; border: 1px solid var(--line); border-radius: 4px; background: var(--surface); color: var(--accent); cursor: pointer; }
.markdown-body pre:hover .code-copy, .code-copy:focus-visible { opacity: 1; }
.code-copy:hover { border-color: var(--accent); }
.code-copy.copied { opacity: 1; border-color: var(--accent); font-size: 0; }
.code-copy.copied::after { content: "Copied"; font-size: 11px; }

/* On-this-page TOC. */
.toc { grid-column: 2; grid-row: 1; position: sticky; top: 8px; max-height: calc(100vh - 160px); overflow-y: auto; padding: 2px 0 2px 14px; border-left: 1px solid var(--line); font-size: 12.5px; }
.toc-title { color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; font-size: 11px; font-weight: 600; margin-bottom: 6px; }
.toc-link { display: block; color: var(--muted); padding: 2px 0; }
.toc-link:hover { color: var(--accent); }
.toc-sub { padding-left: 12px; }
.toc-link.active { color: var(--accent); font-weight: 600; margin-left: -15px; padding-left: 13px; border-left: 2px solid var(--accent); }
.toc-sub.active { padding-left: 25px; }
@media (max-width: 1100px) {
  .doc.has-toc { max-width: 860px; }
  .doc.has-toc .doc-grid { display: block; }
  .toc { position: static; max-height: none; margin-bottom: 20px; padding: 10px 14px; background: var(--bg); border: 1px solid var(--line); border-radius: 6px; }
  .toc-link.active { margin-left: 0; padding-left: 0; border-left: none; }
  .toc-sub.active { padding-left: 12px; }
}

/* Ctrl+K command palette. */
.palette-overlay { position: fixed; inset: 0; background: rgba(0, 0, 0, 0.45); display: flex; justify-content: center; align-items: flex-start; padding-top: 12vh; z-index: 50; }
.palette { width: min(640px, 92vw); background: var(--surface); border: 1px solid var(--line); border-radius: 8px; overflow: hidden; box-shadow: 0 16px 48px rgba(0, 0, 0, 0.25); }
.palette-input { width: 100%; padding: 14px 16px; background: var(--surface); color: var(--ink); border: none; border-bottom: 1px solid var(--line); font: inherit; font-size: 15px; outline: none; }
.palette-results { max-height: 50vh; overflow-y: auto; padding: 6px; }
.palette-item { width: 100%; display: flex; align-items: baseline; gap: 10px; text-align: left; background: none; border: none; color: var(--ink); padding: 7px 10px; border-radius: 4px; cursor: pointer; font: inherit; font-size: 13px; }
.palette-item.active { background: var(--accent-soft); color: var(--accent); }
.palette-path { overflow-wrap: anywhere; }
.palette-repo { margin-left: auto; color: var(--muted); font-size: 11.5px; white-space: nowrap; }

/* --- Phone: the sidebar is a drawer over the content. --- */
@media (max-width: 768px) {
  .site { padding-inline: 12px; }
  .layout { gap: 0; }
  .sidebar { position: fixed; inset: 0 auto 0 0; width: min(var(--sidebar-w), 86vw); z-index: 30; border-radius: 0; margin: 0; box-shadow: 8px 0 32px rgba(0, 0, 0, 0.3); }
  .layout:not(.sidebar-hidden) .backdrop { display: block; position: fixed; inset: 0; background: rgba(0, 0, 0, 0.45); z-index: 20; }
  .doc, .doc.has-toc { padding: 44px 14px 40px; }
  .sidebar-show { margin-bottom: -36px; }
  .crumbs { flex-wrap: wrap; }
  .gh { margin-left: 0; }
  .markdown-body { font-size: 14px; }
  .markdown-body table { display: block; overflow-x: auto; }
}

/* --- Internal modules --- */
.products { max-width: 1100px; }
.products .lede { color: var(--muted); font-size: 14px; max-width: 72ch; }
.product { margin-bottom: 32px; }
.product-head { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; margin-bottom: 6px; }
.product-head h2 { font-size: 15px; font-weight: 600; margin: 0; }
.product-head .meta { color: var(--muted); font-size: 12.5px; }
.product .warning { color: var(--wait); font-size: 12.5px; margin: 2px 0 6px; }
.product .warning::before { content: "⚠ "; }
.product .tablewrap { margin-top: 8px; font-size: 13.5px; }
.product tbody tr:first-child td { border-top: 0; }
.product tr.mod:hover td { background: var(--bg); }
.product td.name { min-width: 180px; }
.product td.name .tech { display: block; font-family: var(--mono); font-size: 11.5px; color: var(--muted); margin-top: 2px; }
.product td.tldr { min-width: 220px; max-width: 360px; line-height: 1.5; }
.product td.owner { white-space: nowrap; color: var(--muted); }
.product th.price, .product td.price { text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }
.product td.price.free { color: var(--ok); font-weight: 600; }
.product th.ver, .product td.ver { text-align: center; white-space: nowrap; font-variant-numeric: tabular-nums; }
.product td.ver a { font-family: var(--mono); font-size: 12.5px; }
.product .pill.planned { background: var(--accent-soft); color: var(--accent); }
.product .pill.discontinued { background: var(--over-soft); color: var(--over); }
.product .eta { display: block; font-size: 11px; color: var(--muted); margin-top: 3px; }
.product .absent { color: var(--muted); }
.product tr.off td, .product tr.noyml td { color: var(--muted); }
.product td.toggle { width: 32px; text-align: center; padding-left: 6px; padding-right: 6px; }
.product button.feat { font: inherit; font-size: 12px; width: 22px; height: 22px; padding: 0; line-height: 1; border: 1px solid var(--line); border-radius: 4px; background: var(--surface); color: var(--accent); cursor: pointer; }
.product button.feat:hover, .product button.feat:focus-visible { border-color: var(--accent); outline: none; }
.product tr.features td { background: var(--bg); padding: 10px 14px 12px 44px; }
.product tr.features ul { margin: 0; padding-left: 18px; line-height: 1.6; font-size: 13px; columns: 2; column-gap: 32px; }
.product tr.features li { break-inside: avoid; }
.product tr.features .label { display: flex; gap: 10px; align-items: baseline; margin-bottom: 4px; }
.product tr.features .label a { text-transform: none; letter-spacing: 0; font-size: 12px; margin-left: auto; }
.product .readme { margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--line); max-width: 72ch; }
.product .readme .markdown-body { font-size: 13.5px; }
.product .future { margin-top: 14px; }
.product .future h3 { font-size: 12px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); margin: 0 0 6px; font-weight: 600; }
.product .future ul { list-style: none; margin: 0; padding: 0; }
.product .future li { padding: 6px 0; border-top: 1px solid var(--line); display: flex; gap: 10px; align-items: baseline; font-size: 13.5px; flex-wrap: wrap; }
.product .future li:first-child { border-top: 0; }
.product .future li.wont_do a { color: var(--muted); text-decoration: line-through; }
.product .future .num { font-family: var(--mono); font-size: 12px; color: var(--muted); min-width: 36px; }
.product .future .ms { color: var(--muted); font-size: 12px; font-family: var(--mono); }
.product .future .age { margin-left: auto; color: var(--muted); font-size: 12px; white-space: nowrap; }
.product .future .empty { color: var(--muted); font-size: 13px; }
.product .chip { font-size: 11px; font-weight: 600; letter-spacing: .04em; text-transform: uppercase; padding: 1px 7px; border-radius: 3px; white-space: nowrap; background: var(--accent-soft); color: var(--accent); }
.product .chip.in_progress { background: var(--ok-soft); color: var(--ok); }
.product .chip.wont_do { background: var(--over-soft); color: var(--over); }
@media (max-width: 768px) { .product tr.features ul { columns: 1; } }

/* --- Print / Save-as-PDF: clean light paper (driven by the Download PDF button) --- */
@media print {
  :root { color-scheme: light; }
  body, .content, .markdown-body, .doc { background: #fff !important; color: #111 !important; }
  .site-header, .sidebar, .sidebar-show, .backdrop, .crumbs, .toc, .code-copy, .heading-anchor { display: none !important; }
  .site, .layout { display: block; height: auto; padding: 0; }
  .content { overflow: visible; }
  .doc, .doc.has-toc { max-width: none; margin: 0; padding: 0; border: 0; }
  .doc.has-toc .doc-grid { display: block; }
  .print-header { display: block; font-size: 11px; color: #555; border-bottom: 1px solid #ccc; padding-bottom: 8px; margin-bottom: 18px; }
  .markdown-body :is(h1, h2, h3, h4) { color: #000; break-after: avoid; }
  .markdown-body h1, .markdown-body h2 { border-bottom-color: #ccc; }
  .markdown-body a { color: #000; text-decoration: underline; }
  .markdown-body pre { background: #f5f5f5 !important; border: 1px solid #ddd; break-inside: avoid; }
  .markdown-body pre, .markdown-body pre * { color: #1a1a1a !important; }
  .markdown-body :not(pre) > code { background: #f0f0f0 !important; color: #b1004b !important; }
  .markdown-body blockquote { background: #f7f7f7 !important; border-left-color: #999; color: #333; }
  .markdown-body th { background: #f0f0f0 !important; }
  .markdown-body th, .markdown-body td { border-color: #ccc; }
  .markdown-body .mermaid { background: #fff; border: 1px solid #ccc; border-radius: 6px; padding: 10px; break-inside: avoid; }
  .markdown-body img { break-inside: avoid; }
}
```

- [ ] **Step 4: `App.vue` template**

Replace the `<template>` and the two imports:

```vue
import SiteHeader from './components/SiteHeader.vue'   // replaces PageSwitch
import markUrl from './assets/cloudunify-mark.png'       // DELETE this line; the mark stays favicon-only
```

```vue
<template>
  <div class="site" :class="{ 'sidebar-hidden': !sidebarShown }">
    <SiteHeader />
    <div class="layout" :class="{ 'sidebar-hidden': !sidebarShown }">
      <div class="backdrop" @click="toggleSidebar"></div>
      <aside class="sidebar">
        <div class="sidebar-top">
          <input class="search field" v-model="store.filter" type="search" placeholder="Filter docs…" />
          <button class="btn kbd-hint" title="Quick open (Ctrl/⌘ K)" @click="paletteOpen = true">⌘K</button>
          <button class="btn kbd-hint" title="Hide sidebar" aria-label="Hide sidebar" @click="toggleSidebar">«</button>
        </div>
        <p v-if="store.reposError" class="error">{{ store.reposError }}</p>
        <Sidebar />
      </aside>
      <main class="content">
        <button class="btn sidebar-show" title="Show sidebar" aria-label="Show sidebar" @click="toggleSidebar">☰</button>
        <InternalModules v-if="route.page === 'internal-modules'" />
        <DocView v-else-if="hasSelection" />
        <div v-else class="placeholder">
          <p>Select a document, or press <kbd>Ctrl</kbd>+<kbd>K</kbd> to search.</p>
        </div>
      </main>
    </div>
    <CommandPalette v-if="paletteOpen" @close="paletteOpen = false" />
  </div>
</template>
```

The script's `drawer`/`narrow` logic is unchanged. In `RepoTree.vue` add the class `field` to the branch `<select class="branch-select field" …>`; nothing else in the tree components changes (class names are kept).

- [ ] **Step 5: `DocView.vue` mermaid theme and `InternalModules.vue` table class**

In `renderMermaid()` replace `theme: 'dark'` with
`theme: window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'default'`.

In `InternalModules.vue` replace `class="table-wrap"` with `class="tablewrap"`. Also change the three pill/chip states nothing else; the template keeps `pill planned|discontinued` and `chip in_progress|planned|wont_do` (styled above).

- [ ] **Step 6: Build and look**

Run: `cd docs-ui && npm run build:theme && npm run build && cd .. && git diff --quiet -- api/app/static/reva.css && echo "theme copy in sync"`
Expected: build clean (no Vue warnings; the pre-existing chunk-size notice is fine), "theme copy in sync" printed.

Then `cd docs-ui && npm run dev` against `make dev` if the stack is available and check with a browser: light and dark (toggle the OS setting), the header nav, a doc with a TOC, a code block in both themes, the Internal modules page, the phone width drawer. If the stack is not available, say so in the report; the reviewer reads the CSS instead.

- [ ] **Step 7: Stage**

```bash
git add docs-ui/src/main.js docs-ui/src/style.css docs-ui/src/components/SiteHeader.vue docs-ui/src/App.vue docs-ui/src/components/DocView.vue docs-ui/src/components/InternalModules.vue docs-ui/src/components/RepoTree.vue
git rm --cached docs-ui/src/components/PageSwitch.vue 2>/dev/null || true   # already handled by git mv
```

---

### Task 4: Docs, handoff, archive, definition of done

**Files:**
- Modify: `docs-ui/README.md` (How it fits, Build & deploy, Features)
- Modify: `docs/setup-production.md` (only if it lists the `/reviews` pages individually: add `/reviews/backups`; the Access prefix is unchanged)
- Modify: `HANDOFF.md` (addendum at the top)
- Move: this plan to `docs/superpowers/plans/archive/`

- [ ] **Step 1: docs-ui README**

In "How it fits", after the diagram, add one paragraph: "Every consultant page (this SPA, `/reviews/`, `/reviews/how-it-works`, `/reviews/backups`) shares one look: `src/reva.css` holds the tokens and base components; `npm run build:theme` copies it to `api/app/static/reva.css` (commit the copy), which the api serves at `/reviews/reva.css`. Light first, dark follows the system; the accent is navy." In "Release-log theme" rename the heading to "Themes" and mention that `build:theme` now also copies `reva.css`. In Features, replace any "dark theme in the Cloudunify colours" wording with "the shared REVA look (light or dark by system setting)".

- [ ] **Step 2: HANDOFF addendum** (insert at the top)

```markdown
## Addendum 2026-10-07 — one look for all consultant pages, Backups page

**Status: implemented, not deployed** (plan
`docs/superpowers/plans/archive/2026-10-07-unified-site-design.md`; mockup
approved in chat, no spec). The status and how-it-works pages lead; the docs
SPA now follows them: shared tokens and components in `docs-ui/src/reva.css`,
copied by `npm run build:theme` to `api/app/static/reva.css` and served at
`/reviews/reva.css`; the SPA imports it directly and dropped its dark-only
Cloudunify palette, Inter and the orange marker (the mark stays as favicon).
Light first, dark by system preference, no toggle; accent navy (`#1f3f7a` / `#8fa9dd`), chosen from seven candidates. highlight.js switches
theme by media query, mermaid by `matchMedia`. The SPA header is the same
header + nav as the static pages (`SiteHeader.vue`, replacing `PageSwitch`).
New static page `/reviews/backups` (`api/app/static/backups.html`) carries
the consultant half of the Odoo.sh backup docs; the technical half stays
repo-only in `docs/odoo-sh-backup-technical.md`, and the consultant markdown
was removed. Every page's nav now has five entries incl. Backups.

**Deploy:** api image (routes + static files) and nginx image (SPA). No
migration. `/reviews/backups` and `/reviews/reva.css` sit under the already
gated `/reviews` prefix.

**Verified / owed:** api suite green; `npm run build` clean; browser check:
<done, or which views are owed, from Task 3 Step 6>.
```

- [ ] **Step 3: Archive and definition of done**

```bash
git mv docs/superpowers/plans/2026-10-07-unified-site-design.md docs/superpowers/plans/archive/
cd api && .venv/bin/python -m pytest tests/ -q && cd ..
worker/.venv/bin/ruff check api/app
cd docs-ui && npm run build:theme && npm run build && cd .. && git diff --quiet -- api/app/static/reva.css
```

All green, build clean, copy in sync. Report every outcome verbatim.

- [ ] **Step 4: Stage**

```bash
git add docs-ui/README.md docs/setup-production.md HANDOFF.md docs/superpowers/plans/
git status --short   # everything staged; Joseph commits
```
