# reva-docs-ui

A read-only Vue 3 SPA that lets consultants browse the docs of every
repo REVA knows about. It is a pure client of the backend `/repo-docs` surface
(see `api/app/routes/docs.py`) — list repos, list a repo's doc tree, fetch a
file, proxy embedded images.

## How it fits

```
browser ─▶ Cloudflare Access ─▶ cloudflared tunnel ─▶ nginx ┬─ /docs/      → this SPA (static)
           (gates /docs +                                   ├─ /repo-docs/ → api  (docs data)
            /repo-docs)                                      ├─ /webhooks/  → api  (GitHub, NOT gated)
                                                             ├─ /api/       → api  (TUI, Bearer key)
                                                             └─ /health     → api
```

Auth is handled entirely at the Cloudflare edge; the SPA and the `/repo-docs`
endpoints carry no app-layer auth. Docs are pulled live from each repo's default
branch via the GitHub App token — there is no clone or rebuild step, so a doc
edit is visible on the next page load.

## Develop

Needs Node ≥ 22.12. Run the REVA stack (`make dev`, api on :8080), then:

```bash
cd docs-ui
npm install
npm run dev            # http://localhost:5173, proxies /repo-docs -> :8080
# REVA_API_URL=http://other:8080 npm run dev   # point at a different api
```

## Build & deploy

The SPA is **built into the nginx image** (multi-stage `nginx/Dockerfile`) and
served at `/docs/` — no separate container or static host. Vite `base` is
`/docs/`, so assets resolve under `/docs/assets/*`; routing is via query params
(`?repo=<id>&path=<file>&ref=<branch>`), so nginx serves it with no rewrite rules.

```bash
docker compose -f docker-compose.prod.yml build nginx   # builds + bundles the SPA
docker compose -f docker-compose.prod.yml up -d nginx
```

To build just the static assets locally: `npm run build` → `dist/`.

### Release-log theme

`src/release-log.scss` styles the `rl-*` release-log fragments customer repos
commit under `docs/releases/`. It is bundled into the SPA by `npm run build`
**and** compiled standalone by `npm run build:theme` into
`../reva/static/release-log.css`, which the worker sends to Odoo with every
release-log callback. Run `build:theme` after every SCSS change and commit the
CSS; the nginx image build does not run it.

### Cloudflare Access

Create an Access application protecting **both** path prefixes on the prod host,
with your consultant policy:

- `reva.dev.cloudunify.org/docs` (the SPA)
- `reva.dev.cloudunify.org/repo-docs` (the data API the SPA calls)

Leave `/webhooks`, `/api`, and `/health` **un-gated** — GitHub webhooks can't
complete an SSO login, and the TUI authenticates to `/api` with a Bearer key.

## Features

- Scrollable sidebar of all enabled repos; expand a repo to lazy-load its doc
  tree — the repo-root `docs/` folder first, then each `custom_addons/` addon.
  `CLAUDE.md` and any `superpowers/` folder are never listed.
- Filter box — typing loads every repo's tree and filters paths across all of them.
- Markdown rendered with `markdown-it`, **sanitized with DOMPurify**, code
  highlighted with `highlight.js`.
- `.html` / `.htm` docs inside a `docs/` folder are rendered through the same
  sanitized pipeline (no markdown-it step). A doc's `<style>` block is stripped
  so it cannot restyle the site; inline `style="…"` is kept.
- Repo-relative links/images rewritten: images through the `/raw` proxy,
  in-repo `.md` and `.html` links to in-app navigation, other repo files to
  GitHub.
- Deep-linkable: the selected doc lives in the URL.
- **Download PDF** — reformats the current doc for clean light-paper print
  (`@media print`) and opens the browser's print dialog ("Save as PDF").
