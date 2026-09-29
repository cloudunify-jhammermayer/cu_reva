# Change summary: affected modules, setup section, branch-linked tickets — design

Status: IMPLEMENTED · Date: 2026-09-29 · Repos: cu_reva (this) + Cloudunify `cu_reva_ticket_analysis` (counterpart section
at the end). Builds on the batched change-note delivery (spec
`archive/2026-07-11-work-status-and-ready-change-notes-design.md`), the release-log entries (spec
`archive/2026-09-04-release-log-change-notes-design.md`) and the ticket-level fallback of the PR-review ticket signal
(spec `archive/2026-07-20-pr-review-ticket-signal-design.md`).

## Need

The consultant who owns a ticket deploys the merged work to several environments. The merge summary in the Odoo
chatter names functional areas only, so the consultant cannot tell which technical modules to install or upgrade.
Decisions (Joseph, 2026-09-29):

- The summary lists the affected modules. Whether a module is installed is NOT REVA's concern: there are several
  environments per customer, the consultant checks each one.
- The drafted note gains a "setup after deployment" section.
- A merged PR that names its ticket only through the branch or title (no `closes #N`) gets a summary too.

Added 2026-09-30 (Joseph, on review of the plan):

- A PR that moves a git submodule names the submodule's path in the summary. Shared Cloudunify modules live in
  submodules outside `custom_addons/` (`3rd_party_addons/cu/queue`), so such a PR would otherwise list nothing.
- A record without REVA issues gets the header "Changes merged", without "ready for review/deploy".
- `modules` and `submodules` stay out of Odoo's dedup hash entirely.

## Behaviour

### 1. Affected modules

1. **At merge** (`worker/worker/change_note_runner.py`): once per job, REVA lists the PR's changed files
   (`GitHubClient.get_changed_files`, the paginated `/pulls/{n}/files` call the reviewer already uses) and maps each
   path through `reva.diff_utils.module_root`. The module's technical name is the directory directly under
   `custom_addons/` or `custom-addons/`. The list is deduplicated and sorted.
2. Paths outside those prefixes (CI files, docs, `odoo/`, `enterprise/`) contribute nothing, and neither does a
   file lying directly in the prefix (`custom_addons/README.md`). A module touched only by tests or translations is
   listed: it still has to be upgraded to load them. A renamed file counts for the module it left as well.
3. The list is stored on every `change_notes` row of that PR (`modules`, JSON list), independent of the note source
   (`claude` or `release-log`) and before the source is decided.
4. A re-run (RQ retry, budget deferral) fills `modules` on a row where it is still NULL and leaves a filled one
   alone.
5. **At delivery** (`worker/worker/change_note_delivery.py`): each `notes[]` item carries its row's `modules`. NULL
   (rows completed before this ships) is sent as `[]`.
6. The Claude prompt does not receive the list and keeps its rule against code identifiers. Module names reach the
   consultant only through the deterministic field.
7. **Submodules.** The same changed-files listing shows a moved, added or removed git submodule as one entry whose
   `patch` consists of `Subproject commit <sha>` lines only (checked 2026-09-30 against
   `Cloudunify/Cloudunify@64f9fb6`: `"@@ -1 +1 @@\n-Subproject commit 7386…\n+Subproject commit 0be8…"`). The paths
   of those entries, sorted, are stored beside `modules` (`submodules`, JSON list, same NULL / filled rules) and
   travel as `notes[].submodules`. REVA names the path only; which modules inside the submodule changed is not
   looked up. A submodule sitting directly under `custom_addons/` is reported here and not as a module.

### 2. Setup section in the drafted note

`prompts/change_note.md` gains a fourth item:

> 4. Setup after deployment: settings to configure, access groups to assign, scheduled actions to activate, data to
>    import. Name them as the user sees them in the interface. Omit this section when the change needs no setup.

No code change. The release-log path is unaffected: its entry already carries `### To-do`.

### 3. Branch-linked tickets

1. **Webhook** (`api/app/routes/webhooks.py`): a merged PR enqueues the change-note job when its body has closing
   refs OR `extract_ticket_id(head.ref, title)` finds a ticket. The job params gain `head_ref`. The per-repo
   `change_notes` switch gates both.
2. **Runner**: closing refs are resolved as today. Only when they resolve to no REVA ticket does the fallback run,
   the same precedence `board_status_runner` uses: `extract_ticket_id` then `resolve_ticket_by_id`.
3. **Ticket name**: the drafted note is written in the language of the ticket name. A fallback ticket has a name
   only when a `ticket_issue_runs` row exists for it. Without one, the user prompt tells Claude to write in the
   language of the PR title and description instead.
4. **Delivery rule**: today a summary ships when the ticket's REVA-created issues are non-empty and all closed, and
   no note is pending. A ticket with NO REVA-created issues never reaches that state. New rule:
   - issues exist: unchanged, wait until all are closed;
   - no issues exist: ship as soon as no note for the ticket is pending, which is once per merged PR.
5. The release-log lookup runs for fallback tickets exactly as for issue-linked ones.

## Contract

`tickets.change-summary` (`reva/odoo_contracts.py`):

```python
class ChangeSummaryNote(BaseModel):
    pr: PrRefPayload
    note_html: str
    modules: list[str] = []   # technical names, sorted, deduplicated per PR
    submodules: list[str] = []   # paths of the git submodules the PR moved, sorted
```

Both always on the wire, `[]` when nothing under the addons prefixes changed or no submodule moved. Both samples
get a `modules` and a `submodules` value.
Regenerate `contracts/` (`python -m reva.odoo_contracts generate`) and sync (`scripts/sync_contracts.sh ../Cloudunify`).

`release_log.modules` is unchanged. It is developer-written text with versions (`cu_auth 19.0.1.0.0`) and stays in
the caption; the new per-PR list is what the diff touched.

## Storage

`db/migrations/052_change_notes_modules.sql`:

```sql
ALTER TABLE change_notes ADD COLUMN IF NOT EXISTS modules JSONB;
ALTER TABLE change_notes ADD COLUMN IF NOT EXISTS submodules JSONB;
```

`ChangeNote.modules` and `ChangeNote.submodules`, both `Mapped[Any | None] = mapped_column(JSON)`, in
`reva/db/models.py`. Writers: a small `record_change_note_modules(db, note_id, modules, submodules)` that writes
both together; `get_undelivered_change_notes` returns both columns.

## Errors

| Case | Behaviour |
|---|---|
| `TransientError` listing the changed files | propagates, RQ retries the job (as every GitHub call in it) |
| Any other error listing the changed files | `modules` and `submodules` stay NULL, ops event `change_note` / `modules_lookup_failed` (warning), the note is still generated and delivered |
| Changed-files listing truncated (`MAX_FILE_PAGES`) | the modules found are sent; the client already logs `github_changed_files_truncated` |
| Fallback ticket unknown to REVA and no active default instance | ops event `odoo_callback` / `no_default_instance` (warning), no note — same as the work-status fallback |
| Odoo rejects the summary (4xx, e.g. the branch number is not a record id) | existing `change_summary_rejected` ops event, rows stay undelivered |

## TUI / API

`api/app/queries/ticket_journeys.py`: the `change_note_posted` event summary appends the modules
(`acme/widgets#7 → internal note (completed) · cu_auth, cu_sale`). The TUI renders the summary string as it
arrives, so `tui/` stays untouched.

## Tests

- `worker/tests`: module derivation (prefix spellings, dedupe, non-addon paths, tests-only module); submodule
  detection (moved, added, a normal file whose patch merely contains the words); runner stores
  modules and submodules on both note sources; NULL filled on re-run; lookup failure records the ops event and still completes the
  note; fallback ticket resolution and its precedence below closing refs; language line when the ticket name is
  unknown; delivery rule for a ticket without issues, and unchanged waiting for a ticket with open issues.
- `worker/tests/test_change_note_delivery.py`: `modules` on the payload, NULL as `[]`.
- `worker/tests/test_prompt_files.py`: the new prompt section.
- `api/tests/test_webhooks.py`: enqueue for a branch-only PR, `head_ref` in the params, no enqueue when neither
  source yields a ticket.
- `api/tests/test_v1_ticket_journeys.py`: modules in the event summary.
- Contract samples validate against the regenerated schema.
- Not covered by unit tests: the migration SQL (tests build tables from the ORM models). Validated on the first
  staging boot.

## Risks

- **Wrong record.** A branch like `feat/123` whose number is not the intended Odoo record makes REVA post to record
  123 of the resolved model if it exists. The work-status fallback already writes to the same record on the same
  evidence, so the trust level does not change, but a chatter note is more visible than a status flag.
- **More Claude spend.** Branch-only PRs did not produce notes so far. They now draw on `REVA_DAILY_BUDGET_USD`.
- **Summary per merge.** A ticket without REVA issues gets one chatter note per merged PR. Its header is
  "Changes merged", without "ready for review/deploy", because nothing signals that the ticket is ready.
- **One branch, several target branches (open).** The webhook does not look at the PR's base branch. If the same
  ticket branch is merged into a staging branch and later into the production branch through two PRs, each merge
  costs a draft and posts a note, and neither note says which branch it went into. Holds for `closes #N` PRs today
  already; branch-linked tickets make it more frequent. Not addressed here.
- **Submodule hint is a path.** It tells the consultant that `3rd_party_addons/cu/queue` moved, not which modules
  inside it changed.

## Out of scope

- Marking modules as new, upgrade hints, install-state checks.
- Sending PRs whose note failed or was budget-skipped.
- Filtering the diff handed to Claude.
- Modules outside `custom_addons/` / `custom-addons/`, including naming the modules inside a moved submodule.
- Filtering merges by base branch, or naming the base branch in the summary.

## Counterpart: Odoo (`cu_reva_ticket_analysis`, next module version)

- `routers/reva_router.py`: `ChangeSummaryNote.modules` and `.submodules`, both
  `list[str] = Field(default_factory=list)`.
- `models/reva_mixin_callbacks.py::_apply_reva_change_summary`: one line directly under the header, the sorted union
  of all notes' modules: `<p><strong>Modules:</strong> cu_auth, cu_sale</p>`. Below it, the sorted union of all
  notes' submodules: `<p><strong>Submodules updated:</strong> 3rd_party_addons/cu/queue</p>`. Each is omitted when
  its union is empty. Hard-coded English like the existing header.
- Header: "Changes merged — ready for review/deploy" when the record has REVA issues (`reva_issue_ids`), otherwise
  "Changes merged".
- Synced contract files, `tests/test_contracts.py`, `tests/test_callback.py`, module version bump.
- No new fields, models, views or security entries.
- The idempotency hash leaves `modules` and `submodules` out entirely. Two sends with the same PRs and note texts
  are the same summary: a summary sent before the keys existed and replayed after the upgrade keeps its hash, and
  so does a retry that carries the lists after a first send without them (the lookup failed, Odoo posted, the
  response timed out, the re-run filled the lists).

## Deploy order

Odoo first: every registered instance is upgraded to the new module version before the REVA worker is redeployed.
The wire fields are safe in either order (the shipped Odoo models ignore an unknown `modules` / `submodules` key, the
new Odoo model defaults a missing one to `[]`, and the dedup hash ignores both). The header is not: an instance still
on the old module heads the per-merge summary of a ticket without REVA issues "Changes merged — ready for
review/deploy".
