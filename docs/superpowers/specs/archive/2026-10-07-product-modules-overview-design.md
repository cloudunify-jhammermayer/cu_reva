# Product modules overview on the docs site — Design

- **Date:** 2026-10-07
- **Status:** implemented 2026-10-07, not deployed. Mockup reviewed with
  Joseph 2026-10-07 (HTML mockup, sample data; the layout below follows it).
- **Context:** the consultant docs browser (`docs-ui` SPA at `/docs`, backed
  by `/repo-docs`) got an "Internal modules" page on 2026-10-05 that is a
  placeholder (`docs-ui/src/components/InternalModules.vue`). Cloudunify will
  maintain several GitHub repos of Odoo addons that are sold to customers
  ("product repos"), one branch per Odoo version. Consultants need one page
  that says, per module: which Odoo versions it exists for, who owns it, what
  it does in one line, its feature set, and what is planned next.

## Decisions taken during brainstorming

- **Source of truth is git plus GitHub issues.** Developers maintain the
  module list in the repo; nothing is stored in REVA's database and nothing is
  edited from Odoo or the TUI. Rejected: an Odoo-pushed table (new contract,
  drifts from code) and a TUI-managed table (lives outside the repo).
- **Available** is derived from the addon manifests, not hand-written.
- **Open work** is the repo's GitHub issues, all of them, one flat list per
  repo. No label convention: a product repo's issues are the ones REVA
  creates from Odoo tickets (`[type] <ticket> - <name>`) or developers file by
  hand, and either way they describe the work. Their state is read from
  GitHub fields alone (assignee, linked PR, close reason, milestone).
  Rejected: an `enhancement` label filter, per-module issue labels.
- **The yml holds only what code cannot say:** owner, price, TL;DR, feature
  set, and a status exception (`planned`, `discontinued`, an ETA). There is
  no "in development" state: a module directory on a version branch is sold
  as is, and `installable: False` is not used in product repos.
- **One branch per Odoo version** (`18.0`, `19.0`, `20.0`). The page reads the
  **three highest** version branches a repo has and ignores older ones:
  Cloudunify does not backport, so older branches are frozen and not sold.
- **A product repo is the distribution unit.** Customers consume a product
  repo as a git submodule, and a submodule is always the whole repo, so a
  module sold on its own gets its own repo. Family repos and one-module repos
  both appear on the page the same way, one section per repo.
- **A product repo declares itself** with `product: true` in
  `.claude-review.yml`. Rejected: inferring from version branches (a customer
  repo with a `19.0` branch would appear), a DB flag (repo cannot say what it
  is).
- **`product: true` implies `review_all_paths`**: a product repo keeps its
  addons at the repo root, OCA-style, so the default `custom_addons/` review
  lock would review nothing.

## Repo conventions

### `.claude-review.yml` on the default branch

```yaml
product: true
```

Read from the default branch when deciding whether a repo is a product, with
the highest version branch as fallback when the default branch has no config
or no `product` key (a product repo may keep `main` empty). Per-branch review config is unaffected: the reviewer still loads the
config at the PR head SHA, and `product: true` there is what widens the review
scope for that PR.

### `product.yml` at the root of every version branch

```yaml
modules:
  cu_helpdesk_sla:
    owner: Joseph Hammermayer      # free text; a name consultants recognise
    price: 1200                    # EUR, or the word free
    tldr: SLA timers and escalation on helpdesk tickets
    features:
      - Per-team SLA policies with business-hours calendars
      - Escalation mail on breach
      - SLA column and filters on the ticket list
  cu_helpdesk_kb:
    owner: Markus B.
    price: free
    tldr: Suggest knowledge articles while typing a ticket reply
    status: planned                # no directory yet
    eta: 2027-Q1                   # free text, shown verbatim
  cu_helpdesk_chat:
    status: discontinued
    note: Replaced by Odoo 19's native live-chat handover
```

- Keyed by the module's technical name, which is the directory name.
- Every key is optional. A module present in the manifests but absent from
  the yml still renders, with the manifest `summary` as TL;DR and empty owner
  and features, visibly marked as lacking a yml entry.
- `price` is either the word `free` or a number in EUR. Shown as "free" or
  the formatted amount; absent means the column shows an em dash. Not per
  branch in spirit, so it is read from the highest branch like the owner.
- `status` is one of `available`, `planned`, `discontinued`; omitted means
  `available`. A `planned` entry may have no module directory; any other yml
  entry without a matching directory on that branch is ignored and reported
  in that repo's `warnings` (see below). `eta` and `note` are free text shown
  next to the status pill. Unknown status values degrade to `available` plus
  a warning.
- The file is per branch because the module set and the status differ per
  version. Owner, TL;DR and features are repeated across branches; the page
  shows them from the highest version branch that has the module, and a
  differing `owner` or `price` on a lower branch is reported as a warning so
  drift is visible.

### Manifests

A module is a top-level directory on the branch containing `__manifest__.py`.
The manifest provides `name`, `summary`, `version`. Parsing is
`ast.literal_eval`, as `reva/odoo_registry.py` already does for core; a
manifest that fails to parse yields a module with only its technical name and
a `manifest_error` string.

### Version branches

Branches whose name matches `^\d+\.0$`, sorted numerically descending, and
**only the three highest are read**; older ones are dropped and listed in the
repo's `ignored_branches` so the page can say "16.0 ignored".

### Open issues

Every issue on the repo: `state=open` (first page of 100) plus
`state=closed&since=<now - 90 days>` (first page of 100). Issues only: the REST issues endpoint also returns pull
requests, and those are dropped (`pull_request` key present). Each issue gets
one state, read from GitHub fields alone:

- **in_progress**: open with an assignee, or open with a linked open PR
  (GitHub's `pull_request` cross-reference is not in the REST issue payload,
  so "linked PR" means an open PR whose body or title references `#<n>` on
  this repo; the first page of open PRs is scanned once per repo).
- **planned**: open, unassigned, no linked PR.
- **wont_do**: closed with `state_reason == "not_planned"` within the last 90
  days. Older ones are dropped.
- Closed as completed: not shown; the release log covers shipped work.
- The title is shown as GitHub has it. REVA's own ticket issues carry
  `[type] <ticket> - <name>`, so the Odoo ticket number is visible without the
  page knowing about Odoo.
- **eta**: the milestone title when the issue has one, else none. Milestones
  are optional and need no naming convention.

Sorted in_progress, planned, wont_do, then newest first; capped at 50.

## Backend

### `reva/types.py`

`RepoConfig` gains `product: bool = False` and a `model_validator(mode="after")`
that sets `review_all_paths = True` when `product` is true. One unit test for
the implication, one that `product: false` leaves `review_all_paths` alone.

### Config loader moves to `reva/`

`worker/worker/repo_config.py` becomes `reva/repo_config.py` unchanged; the
five worker imports and the worker tests update their import path. The api
needs the same degrade-to-defaults loader to read the product flag, and the
`FileContentReader` protocol it takes is satisfied by the api's GitHub client
already. No re-export shim.

### `reva/product_catalog.py` (new, pure)

Functions with no GitHub access, unit-tested in `worker/tests` or
`api/tests` without a transport:

- `is_version_branch(name) -> bool`
- `parse_manifest(text) -> dict | None`
- `parse_product_yml(text) -> dict[str, ModuleMeta]` — tolerant: missing file,
  malformed YAML, non-mapping or wrong-typed entries degrade to `{}` and a
  returned error string, never raise.
- `merge_branch(modules_from_manifests, product_yml) -> list[BranchModule]`
- `merge_repo(branches: list[(version, [BranchModule])]) -> list[ProductModule]`
  — one row per technical name, a `versions` dict keyed by branch name, owner
  / tldr / features taken from the highest branch that has them.

### `GET /repo-docs/products` and `GET /repo-docs/products/{repository_id}`

Two endpoints in `api/app/routes/docs.py`, response models in
`api/app/schemas/docs.py`. The list is cheap (one config read per enabled
repo, cached) and the detail does the per-repo work, so the SPA fetches
details in parallel and renders each section as it arrives instead of
waiting for the slowest repo.

```
ProductList
  items: [ProductRepoRef]         # enabled repos whose config says product
ProductRepoRef
  repository_id, full_name, owner, name, html_url
ProductRepo                        # the detail
  repository_id, full_name, owner, name, html_url
  loaded_at: str                  # when this repo's data was fetched (cache age)
  branches: [str]                 # the version branches read, descending (max 3)
  ignored_branches: [str]         # older version branches not read
  modules: [ProductModule]
  issues: [RepoIssue]
  warnings: [str]                 # per-repo, non-fatal (see below)
ProductModule
  module: str                     # technical name
  name: str | None                # manifest name from the highest branch
  tldr: str | None
  owner: str | None
  price: "free" | number | None
  features: [str]
  has_yml_entry: bool             # false = rendered greyed, "no product.yml entry"
  versions: {branch: ModuleVersion}
ModuleVersion
  status: available | planned | discontinued
  version: str | None             # manifest version, None for planned
  eta: str | None
  note: str | None
  manifest_error: str | None
  readme_path: str | None         # "<module>/README.md" when present on that branch
RepoIssue
  number: int, title: str, url: str, state: in_progress | planned | wont_do
  assignee: str | None, pr_number: int | None, milestone: str | None
  created_at: str, closed_at: str | None
```

List: for every enabled repo (same list as `/repos`) load `.claude-review.yml`
through the moved loader (default branch, version-branch fallback as above);
keep those with `product` true. A `product_flag_cache` (TTL 300 s, per repo)
backs this and the tree-scope decision below.

Detail, for one repo (404 when it is not a product repo):

1. `get_branches`, keep version branches, take the three highest.
2. Per version branch: `_cached_tree` for that ref, derive module dirs from
   `<dir>/__manifest__.py` entries, fetch each manifest and `product.yml`
   through `_cached_file` (manifests in parallel with the `ThreadPoolExecutor`
   the titles endpoint already uses), merge, derive each module's status.
3. `list_issues` (new GitHub client method, open page plus closed-since page
   as above, PRs dropped) plus `list_pull_requests` (`state=open`, first page)
   for the linked-PR check; classify as above.
4. Merge branches into one module list, collect warnings.

Error handling: the page aggregates many repos, so one repo failing must not
404 the page. A `PermanentError`/`TransientError` from any GitHub call for a
repo lands as a `warnings` entry on that repo (or on a stub repo entry with
empty modules when branches cannot be listed), is logged, and records an ops
event `product_catalog_degraded` with the repo and the step, per the
"degradations are visible" invariant. A malformed `product.yml` is a warning
too. Only a DB failure or an unexpected exception propagates as 500.

Caching: a new `products_cache` (TTL 300 s, keyed by repository id) holds
each detail response with its `loaded_at`; manifests and `product.yml` ride
the existing `file_cache` and trees the existing `tree_cache`. Cold load for one repo with three branches
and ten modules is roughly 3 tree calls, 33 file calls and 1 issues call;
tolerable, and warm loads are free.

### Docs browser scope for product repos

Today `browser_in_scope` lists markdown under `custom_addons/` and the
repo-root `docs/`, plus HTML inside any `docs/` folder, so a product repo's
root-level addon docs are invisible. `_cached_tree` gains knowledge of the
repo's product flag (cached per repo, same loader as above): for a product
repo the addon roots are the top-level directories with a manifest, and the
scope widens to markdown anywhere under an addon root and HTML under an addon
root's `docs/`. Implemented as `browser_in_scope(path, addon_roots=())`,
default behaviour unchanged for every non-product repo. The grounding
predicate `in_scope` is **not** changed: no `_SCOPE_VERSION` bump, no
re-index. Grounding for product repos is deferred (see below).

The branch picker only lists `main`/`dev`/`test`/default. Version branches
are not added to it; the module README link opens the doc view with
`?ref=<branch>` directly, which the doc view already supports.

## Frontend

`docs-ui/src/api.js`: `listProducts()` and `getProduct(repoId)`.

`docs-ui/src/components/InternalModules.vue` replaces the placeholder:

- Loads the product list, then every detail in parallel; each section
  renders when its detail arrives, with a per-section loading and error
  state, so one slow or failing repo never blocks the others.
- One card per product repo, headed by the repo name linking to the repo's
  docs in the browser, the branches read, and "loaded hh:mm" from
  `loaded_at` so a stale cache is recognisable. Repos without a single module still render, with
  their warnings, so a misconfigured repo is visible rather than missing.
- A table, one row per module: module name (manifest name, technical name
  underneath), TL;DR, owner, price (column labelled "Price (one-time, net
  EUR)"; "free", the amount, or an em dash),
  one cell per version branch, then a features toggle. The version cell shows the manifest version linking to the module
  README on that branch, or a status pill: "planned" or "discontinued",
  with the ETA or note underneath; an em dash when the module
  is absent on that branch. A row whose every version is discontinued renders
  greyed with the note in place of the TL;DR. A module without a yml entry
  renders greyed with "no product.yml entry" under the technical name.
- The repo heading also names any ignored older branch.
- The toggle expands a panel under the row with the feature list and, below
  it, the module's full README rendered inline through the existing
  `renderMarkdown` pipeline (DOMPurify, highlight, relative images via
  `/raw`). The README is fetched lazily on first expand with the existing
  `getFile` call, from the highest version branch that has `readme_path`
  set, and the panel names that branch with an "open in docs" link to the
  doc view on it. No README on any branch: the panel says so. Expansion state
  is remembered per module via the existing `persist.js` helper like the tree
  expansion state.
- "Open issues" below the table: one line per issue with number, a state
  chip (in progress / planned / won't do), the title linking to GitHub, the
  milestone as ETA when present, and the assignee or linked PR number for
  in-progress ones. Won't-do lines are greyed and struck through. An empty
  list says so.
- Warnings render as a muted line under the card heading.

No change to `PageSwitch.vue` or `location.js`; the page is already routed.

## Review scope

Nothing beyond the `RepoConfig` validator. `review_all` in
`worker/worker/reviewer.py` already honours `review_all_paths`, so a product
repo's PRs are reviewed in full. One worker test loads a config with
`product: true` and asserts the review prefixes are not applied.

## Known limitations, accepted

- Linked-PR detection scans open PR titles and bodies for `#<n>`; a PR that
  names the Odoo ticket only in its branch is not matched, and the issue
  shows as planned until someone is assigned. REVA's ticket flow assigns, so
  the common case is covered.
- Only `README.md` renders inline; `README.rst` is outside the docs scope and
  shows as "no README".
- Data lags the caches by up to five minutes; the heading's `loaded_at`
  makes that visible.
- Owner is free text: spellings can drift and it links nowhere. Switching to
  a GitHub login is a one-line yml change later if wanted.
- No filter on the page; with a few dozen modules it gets long.
- Issues are per repo, so in a many-module repo only the title says which
  module an issue touches.

## Explicitly deferred

Root-level addons violate other `custom_addons/` assumptions that are left as
they are in this change; each needs its own look when a product repo is
actually onboarded:

- `reva/diff_utils.py` module naming (`custom_addons/<module>`), used for
  change-note module detection and the xml-only skill decision.
- The review skills in `prompts/skills/*.md` and `prompts/review_guidance.md`
  phrase their scope as `custom_addons/`.
- Ticket-analysis grounding `in_scope` in `reva/repo_docs.py`.
- TUI: nothing new lands in the DB, so no tab or endpoint. If product repos
  later get operational state (sync failures, counts), surface it then.
- Per-module issue mapping and milestone due dates as computed ETAs.
- Review-time validation of `product.yml` (parse and schema) as a finding on
  product-repo PRs, so a broken file is caught before someone opens the page.
- Review-time check that a product module's manifest `depends` stays within
  its own repo plus the utility repo (a dependency on a sibling the customer
  did not buy breaks the install).
- `cu_reva` itself must never be an enabled repo in the docs browser (decision
  2026-08-07); unaffected, the product flag does not change what `/repos`
  lists.

## Testing

- `reva/product_catalog.py`: pure unit tests for version-branch matching,
  manifest parsing incl. a broken manifest, yml tolerance (missing, malformed,
  wrong types, unknown module, unknown status, price as free/number/other),
  status (yml override, planned without directory), the three-branch cap, merge
  across branches (owner from highest branch, absent versions), issue
  classification (assignee, linked PR, not_planned within/after 90 days,
  completed dropped, PRs dropped).
- `RepoConfig`: product implies review_all_paths.
- `/repo-docs/products` and `/products/{id}` route tests with the existing
  `httpx` MockTransport fake: a non-product repo is skipped from the list and
  404s on detail, the version-branch config fallback, a product repo with two version
  branches renders modules and open issues, a GitHub failure on one repo
  yields a warning plus an ops event, owner drift across branches yields a
  warning, a repo with four version branches reads three and reports the
  fourth as ignored, closed-completed issues are not listed.
- `/repo-docs/repos/{id}/tree` lists root-level addon docs for a product repo
  and keeps the old scope for a non-product repo.
- Worker tests pass after the loader move.
- SPA: `npm run build` clean, plus a manual browser check against the dev
  stack with one product repo (cards, version cells, expand panel with
  features and inline README incl. a relative image, version link with
  `ref`, issue link). State honestly in the handoff if
  the browser check was not done.

## Deploy

api image rebuild (`/repo-docs` change and shared `reva/`), worker and
scheduler rebuild (shared `reva/` loader move), nginx rebuild for the SPA. No
migration. Cloudflare Access already gates `/docs` and `/repo-docs`.
