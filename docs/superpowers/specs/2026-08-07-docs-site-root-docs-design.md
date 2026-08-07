# Docs site shows the repo-root `docs/` folder — Design

- **Date:** 2026-08-07
- **Status:** open — not implemented.
- **Context:** requested by Joseph 2026-08-07. The consultant docs browser
  (`docs-ui` SPA at `/docs`, backed by `/repo-docs`) shows a repo's Markdown
  only under `custom_addons/` / `custom-addons/` — `reva/repo_docs.py::in_scope`
  is the single definition of "the repo's docs" and `api/app/routes/docs.py`
  imports it. A repo's top-level `docs/` folder is therefore invisible on the
  site. Joseph wants it shown, with one hard exclusion: the `superpowers/`
  folder (specs/plans — agent workflow bookkeeping, not documentation) must
  never appear.

## Decisions taken during brainstorming

- **Every registered repo**, not just REVA's own — the scope widens for all
  repos the browser lists.
- **Both browser and grounding.** `in_scope` stays one shared definition, so
  root `docs/` also enters `repo_doc_sections` and can be retrieved/cited by
  ticket analysis and support answers. This is a deliberate behavior change to
  the answering paths, not just a UI change.
- **The root `docs/` tree only.** Root `README.md` and other loose root-level
  `.md` files stay out of scope.
- **`superpowers/` excluded as a path segment anywhere**, not only at
  `docs/superpowers/` — same cost, and a repo that nests its specs one level
  deeper can't defeat it.
- **Forced re-index via a `scope_version` column** (migration 045), because a
  scope change does not move the GitHub tree SHA that `sync_repo_docs` keys
  staleness on.

## Design

1. **Scope (`reva/repo_docs.py`).** `SCOPE_PREFIXES` becomes
   `("custom_addons/", "custom-addons/", "docs/")`; a new exclusion rejects any
   path carrying a `superpowers/` segment (test `"/superpowers/" in "/" + path`,
   so it also catches a repo whose root *is* `superpowers/`). `startswith` is
   anchored, so `"docs/"` matches only the repo root — `custom_addons/cu_x/docs/*.md`
   keeps matching through its own prefix exactly as today. `EXCLUDED_BASENAMES`
   (`CLAUDE.md`) is unchanged. Update the module docstring and the scope comment
   in `api/app/routes/docs.py`, which both currently say "custom addons only".

2. **Ranking is unchanged.** `doc_priority` keys on the parent directory name,
   so root `docs/consultant.md` already lands in tier 0 and the rest of
   `docs/*.md` in tier 2 without an edit. The `_MAX_FILES = 200` cap and its
   `files_capped` ops event stay as they are; corpora grow by each repo's root
   docs, and a repo that crosses the cap now loses its lowest-tier files, as
   designed.

3. **Forced re-index (migration `045_repo_docs_scope_version.sql`).**
   `sync_repo_docs` short-circuits on `row.tree_sha == tree_sha`, which a scope
   change does not move, so every already-synced repo would keep its old index
   until its default branch next moves. Add
   `repo_docs_sync.scope_version INTEGER NOT NULL DEFAULT 0` (idempotent
   `ADD COLUMN IF NOT EXISTS`) with the matching `RepoDocsSync` ORM field, and a
   module constant `_SCOPE_VERSION = 1`. Both staleness checks — the lock-free
   fast path and the re-check under the advisory lock — require
   `row.tree_sha == tree_sha and row.scope_version == _SCOPE_VERSION`; both
   write paths (insert and update) stamp `_SCOPE_VERSION`. Each repo re-indexes
   exactly once, lazily, on its next use. Future scope edits are a one-integer
   bump.

4. **Frontend (`docs-ui/src/tree.js`).** `buildDocTree` strips a leading
   `custom_addons` / `custom-addons` segment so addon folders sit at top level;
   root docs keep their `docs/` segment, so a `docs` folder appears in that same
   top-level list with no structural change. Because addons are named `cu_*`,
   which sorts before `docs`, the repo's own documentation would otherwise land
   at the bottom of every tree — so `toNodes` pins a top-level `docs` node
   first. Fix the file-header comment, which asserts the backend scopes to
   `custom_addons/`.

5. **No API-shape change.** `/repo-docs/repos/{id}/tree|file|search|raw` keep
   their contracts; the tree simply returns more paths. The api's `tree_cache` /
   `file_cache` are 120 s TTL, so no cache invalidation work is needed.

## Verification

- `worker/tests/test_repo_docs.py`: extend the `in_scope` param table —
  `docs/setup-local.md` in scope, `docs/superpowers/specs/x-design.md` and
  `docs/superpowers/plans/y.md` out, `custom_addons/**` cases unchanged,
  `custom_addons/cu_x/docs/consultant.md` still in. New test: a row whose
  `tree_sha` matches but whose `scope_version` is stale still re-indexes (and a
  fully current row still returns `fresh`).
- `api/tests/test_docs.py`: `/tree` includes root `docs/**` and hides
  `docs/superpowers/**`.
- `make test` — shared `reva/` change, so worker + api + scheduler — plus
  `ruff check`.
- Manual: `cd docs-ui && npm run dev` against the local stack; confirm a repo's
  tree shows `docs` first, that superpowers is absent, and that a root doc
  renders.
- Migration 045's raw SQL runs only on real Postgres (tests build from the ORM),
  so it is validated by `make test-integration` or the first staging boot.

## Deploy notes

- `docs-ui` is built into the nginx image (multi-stage `nginx/Dockerfile`), so
  this needs `docker compose -f docker-compose.prod.yml build nginx` in addition
  to the api/worker redeploy that carries migration 045.
- The first ticket analysis / support answer per repo after deploy pays one
  re-index (the `scope_version` bump makes every repo stale once).

## Out of scope

- Root `README.md` and other loose root-level Markdown.
- Any per-repo configurability of the doc scope (`.claude-review.yml`).
- Changes to retrieval ranking, section splitting, or the caps.
