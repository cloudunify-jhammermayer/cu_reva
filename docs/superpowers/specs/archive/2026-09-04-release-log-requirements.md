# Release log: what REVA has to do

Status: requirements, 2026-09-04 · Supersedes `2026-09-02-release-notes-design.md` (dropped 2026-09-04) · Plan:
`docs/superpowers/plans/archive/2026-09-04-release-log.md` · Counterpart:
wenatex_odoo `docs/superpowers/specs/2026-09-04-release-log-and-overview-design.md` and its plan.

## Context

Customer projects now keep one release log per release in their repo: `docs/releases/<name>.md` (German, one section per
ticket with `Gebaut` and `To-do`) and, generated from it and committed, `docs/releases/<name>.html`, an HTML **fragment**
with `rl-*` classes and no `<style>`/`<script>`. The release name is assigned up front ("lollipop") and equals the
name of the Odoo `cu.release` record. REVA does not draft release notes anymore. It does two things:

1. Style those fragments in the docs site, with one shared theme.
2. On request from Odoo, find the page for a release and hand Odoo its URL, the fragment and the theme CSS.

Nothing in the docs browser scope changes: `browser_in_scope` already admits `docs/**/*.html`, and `DocView.vue` already
routes `.html` through `renderHtml`, which keeps `class` attributes and strips `<style>` (the reason the theme must live
here).

## R1 — Theme for release pages (docs-ui)

- Add `sass` as a devDependency of `docs-ui` (Vite compiles `.scss` when it is present) and create
  `docs-ui/src/release-log.scss`, imported from `src/main.js` after `style.css`.
- Tokens on `.rl-page` (light set, the defaults) and on `.markdown-body .rl-page` (dark set, matching the site's
  `--bg-primary #1e1e1e` family) so the same fragment reads right in the docs site and inside Odoo's light iframe:
  ground, panel, line, ink, muted, accent teal (`#0f4c5c` light / `#5fb3c4` dark), status colours
  `--st-umgesetzt #1f7a4d`, `--st-weitgehend #5a8f2b`, `--st-teilweise #b7791f`, `--st-offen #8b918d`, pill ink.
  Design reference: the approved mock (Claude artifact 1f71a9c6), whose CSS is the starting point.
- Classes to style (emitted by the generator, contract for both sides): `rl-page`, `rl-masthead`, `rl-eyebrow`,
  `rl-meta` (dl), `rl-stats`, `rl-stat[data-st=…]`, `rl-stat-num`, `rl-stat-label`, `rl-tickets`,
  `rl-ticket[data-status=umgesetzt|weitgehend|teilweise|offen]`, `rl-stripe`, `rl-head`, `rl-id` (+ `small`),
  `rl-pill`, `rl-col`, `rl-label`, `rl-built`, `rl-chips`, `rl-chip`, `rl-todo`, `rl-notes`, `rl-note`. The stripe and
  pill colours come from the `data-status` attribute selector, not from inline styles.
- Print: `@media print` keeps the cards readable on one column (the site's PDF export reuses it). The site's own
  print block forces a white page on `.markdown-body`, so the theme's print block resets the `.markdown-body .rl-page`
  tokens to the light set, or the cards print dark on white.
- A separate npm script `build:theme` (`sass src/release-log.scss ../reva/static/release-log.css --no-source-map`)
  writes the compiled theme to `reva/static/release-log.css`, committed and packaged with the shared library, because
  the **worker** sends the callback and its image ships neither `api/` nor node. It is not chained into `build`: the
  nginx image builds the SPA from `docs-ui/` alone and has no `reva/` directory to write to. Vite's own hashed output
  keeps serving the docs site.

Acceptance: open a repo's `docs/releases/lollipop.html` in the docs site: cards, pills and stats styled, dark theme; the
compiled CSS file exists and is identical to what the SCSS produces.

## R2 — Release-log lookup (API + worker)

Replaces the drafting design. No Claude call, no task material.

- Route `POST /api/v1/release-note` on the instance-key gate (`create_router`, `require_odoo_instance`), body exactly
  what `cu_reva_ticket_analysis` already sends: `{release_id, name, date, model_name, task_ids}`; respond `202
  {"note_id": <int>}` within 10 s (Odoo's timeout, otherwise it rolls the request back). A blank name, or one whose
  slug carries a path separator or a leading dot, is a 422 (it could never be a file under `docs/releases/`, and an
  unchecked slug would walk out of the folder into other repos of the GitHub App installation). One pending lookup per
  (instance, release): a re-submit within 30 minutes (Odoo's watchdog horizon) echoes the pending `note_id`; past it the
  stale row is superseded and a new job starts, the way the timesheet route dedups.
- Job `worker.release_note_tasks.run_release_note` (stable entry via `terminal_on_permanent`, runner in
  `worker/worker/release_note_runner.py`), `Retry(max=3, interval=[30, 120, 300])`, well inside Odoo's 30-minute watchdog.
- Slug: `name.strip().lower()` with whitespace replaced by `-`; path `docs/releases/<slug>.html`.
- Repos to search: when the request carries `github_url`, the page is read from that registered repo and nothing is
  scanned; the `odoo_instance` scan below applies to requests without one (older Odoo modules). Every enabled `repositories` row whose `.claude-review.yml` on the default branch declares
  `odoo_instance: <name>` equal to the calling instance's `odoo_instances.name`. `RepoConfig` gains the key; the file is
  fetched live per enabled repo with `worker.repo_config.load_repo_config`, one contents call each, the way reviews and
  audits read it (`repositories.config_cache` is a dead column from migration 001 that nothing writes or reads). Repos
  without the key are never searched. Read the page with `GithubClient.get_file_content(token, owner, name, path,
  default_branch)` from the repository row; do not go through the docs API's `_cached_tree`/`_cached_file`
  (module-private, and the file cache would serve a stale page).
- One hit: callback `POST {callback_url}/releases/release-note` through `OdooCallbackClient._post` with
  `{"release_id", "note_id", "status": "completed", "url", "html", "css"}` (unset fields are omitted on the wire,
  never sent as `null`: the shipped Odoo model types `html` as a plain `str`) where `url` is
  `{settings.docs_site_url}/docs/?repo=<repository_id>&path=docs/releases/<slug>.html`, `html` the fragment verbatim,
  `css` the content of `reva/static/release-log.css` (`reva.release_log.theme_css()`). `docs_site_url` is a new setting (`REVA_DOCS_SITE_URL`, e.g.
  `https://reva.cloudunify.at`).
- No hit: callback with `status: "failed"` and `error: "Kein Release-Log 'docs/releases/<slug>.html' in <repo list>"`.
  Several hits: first by ascending `repository_id`, plus an ops event `release_doc_ambiguous` listing all of them.
- Ops events, per the degradation rule: `release_doc_ambiguous` (info), `config_fetch_failed` and
  `config_parse_failed` (warning, one per repo whose `.claude-review.yml` could not be read or is not valid YAML /
  config), `page_fetch_failed` (warning, one per mapped repo whose page read failed for anything but a 404),
  `docs_site_url_unset` (warning), `release_note_callback_failed` and `release_note_failed_callback_error` (error).
  When every enabled repo was unreadable the failure text is `GitHub-Zugriff fehlgeschlagen für alle Repositories:
  <repo list>`, not the missing-key text. A missing release log is an outcome, not a degradation: failed callback only.
- Persistence: table `release_notes` (migration `048_release_notes.sql`) with `id` (= `note_id`), `odoo_instance_id`,
  `release_id`, `release_name`, `slug`, `status` (`pending|completed|failed`), `source_repo_id`, `source_path`, `url`,
  `error`, `created_at`, `completed_at`, `callback_sent_at`. Enough for the request log and a TUI list; no content is
  stored, the repo is the source of truth. `completed` means delivered: the row turns completed together with
  `callback_sent_at` after Odoo answered 200, so an RQ retry repeats the cheap lookup instead of needing stored content.
- Contracts (`reva/odoo_contracts.py`): inbound `release-note` (register the request model in `_inbound_models()`), callback
  `releases.release-note` with a `ReleaseNotePayload` model carrying the fields above (`html` and `css` optional strings,
  present on `completed`). `python -m reva.odoo_contracts generate`, then `scripts/sync_contracts.sh` into the Odoo
  repos; the drift tests pick it up.
- Odoo's callback route is already live: `cu_reva_ticket_analysis/routers/release_router.py`, `ReleaseNoteRequest` with
  `release_id`, `html`, `note_id`, `status`, `error`; it needs the two new optional fields `url` and `css` (see R3). It has
  no `model_config`, so Pydantic's default drops `url` and `css` silently until R3 adds them: the end-to-end test
  before R3 works, the stored note is just unstyled.

Acceptance: an instance with two mapped repos, one holding `docs/releases/lollipop.html`: request → 202 → callback
`completed` with the right `url`, the fragment and non-empty `css` within a minute. Unknown release → `failed` callback
with the German error. Instance without mapped repos → `failed`, error names the missing `odoo_instance` key.

## R3 — Odoo counterpart (cu_reva_ticket_analysis, separate ticket, listed for completeness)

- `cu.release`: new `reva_release_log_url` (Char), `release_note` keeps `sanitize=False` but now stores the composed
  document `<!doctype html><html><head><meta charset="utf-8"><style>{css}</style></head><body>{html}</body></html>`.
- `_apply_reva_release_note(html, status, error, url=None, css=None)`: on `completed` compose and store, set the URL.
- Form: link button "In REVA öffnen" (`reva_release_log_url`), the page rendered with a copy of core's `iframe_wrapper`
  widget (`web/static/src/views/fields/iframe_wrapper`) named `cu_release_log_iframe` with auto height (resize the
  iframe to its document's `scrollHeight` on load) instead of the A4 scaling; button label "Release-Log abrufen".
- Watchdog, chatter and staleness guards stay as they are.
- Two things the shipped module still has the old way: `release_note` uses the default sanitizer (no `sanitize=False`
  yet), and `action_request_release_note` refuses a release without tasks and announces "REVA is drafting". R3 sets
  `sanitize=False`, drops the task guard and rewords the notification; REVA accepts an empty `task_ids` already.

## R4 — Release name on created GitHub issues (create-issues path)

- Odoo already sends `release: {id, name, date}` on `ticket-analysis`, `support-request` and `create-issues` when the
  record ships in a release (`reva_mixin._reva_release_payload`); REVA's request models drop it today (Pydantic
  default). `CreateIssuesRequest` and `TicketIssueJobParams` gain `release: ReleaseRef | None`; the other two calls
  keep ignoring the block.
- `_format_issue_body` and `_format_parent_body` add a `**Release:** <name>` line right after the `**Branch:**` line
  when the name is non-empty. Not persisted on `ticket_issue_runs`, so a requeue rebuilt from the row carries no
  release line.
- Contract: the `create-issues` inbound schema picks the field up on regeneration; an extra sample carries the block.

## Out of scope

- Drafting text with Claude. The repo entry is written by the developer; REVA only looks it up.
- Framing the live docs site in Odoo (nginx `X-Frame-Options: DENY`, Cloudflare Access cookie).
- A REVA-side Markdown-to-cards renderer; the generated HTML fragment makes that unnecessary.

## Open

- `REVA_DOCS_SITE_URL`: worker `environment` in both compose files (`${REVA_DOCS_SITE_URL:-}`), `.env.example`, the
  env tables in `README.md` and `docs/setup-production.md`. Unset: the callback carries a relative `/docs/?...` URL
  and the worker records `docs_site_url_unset`. Resolved 2026-09-04.
- TUI: a **Releases** tab now, key `w`, cloned from the Timesheets tab (`GET /api/v1/release-notes`): note id,
  instance, release, status, source path, age; detail pane with URL and error. Resolved 2026-09-04.
