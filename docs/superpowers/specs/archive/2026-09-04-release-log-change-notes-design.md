# Release-log entries replace drafted change notes — design

Status: DESIGN · Date: 2026-09-04 · Repos: cu_reva (this) + Cloudunify `cu_reva_ticket_analysis` (counterpart section
at the end). Builds on `docs/superpowers/specs/archive/2026-09-04-release-log-requirements.md` (the release-log page
REVA fetches for Odoo releases) and the batched change-note delivery of spec 2026-07-11.

## Need

When a PR merges, REVA has Claude draft a customer-facing change note per PR and, once the ticket is ready, posts them
as one "Changes merged" chatter note in Odoo. Customer repos now keep a release log (`docs/releases/<name>.md`) with a
developer-written entry per ticket: `## <ticket> — <title>`, `- Status`, `- Module`, `### Gebaut`, `### To-do`. That
entry is the text the customer should get. Decisions (Joseph, 2026-09-04): when the entry exists, the Claude draft is
dropped, not kept alongside; the chatter note carries both Gebaut and To-do.

## Behaviour

1. **At merge** (`worker/worker/change_note_runner.py`, unchanged trigger): for each Odoo ticket the PR closes, REVA
   reads every `docs/releases/*.md` on the repo's default branch and looks for the section `## <ticket_id> — …`.
   - Found: the `change_notes` row is recorded with `source = "release-log"`, zero tokens and cost, and an empty
     `note_html`; Claude is not called and the budget gate is skipped.
   - Not found (no release log in the repo, or the ticket has no entry yet): Claude drafts the note exactly as today.
   - Several logs carry the entry: `status: open` logs win, then the alphabetically first file; ops event
     `release_log_entry_ambiguous` (info).
   - A GitHub error while reading the logs is `TransientError` (RQ retries the merge job); an unparseable log is
     skipped with ops event `release_log_parse_failed` (warning) and the ticket falls back to Claude.
2. **At delivery** (`worker/worker/change_note_delivery.py`, unchanged trigger: ticket ready and no note pending):
   when at least one of the ticket's notes has `source = "release-log"`, REVA re-reads the entry from the repo (the
   final state at ready time is what the customer should see, later PRs may have refined it) and sends it once as
   `release_log` on the `tickets.change-summary` callback:
   `{release, ticket, title, status, modules: [..], html}`. The per-PR `notes` items stay (Odoo creates its PR
   records from them); a release-log-covered PR carries `note_html: ""`. Entry gone at delivery time: the summary
   goes out without the block, ops event `release_log_entry_missing` (warning).
3. **Rendering** (`reva/release_log.py`): a parser for the documented format (frontmatter `release`, `status`,
   `date`; `## <ticket> — <title>`; `- Status: <word>`; `- Module: <name> <version> · …`; `### Gebaut` paragraphs or a
   list; `### To-do` list; anything else ignored) and a renderer to the tag set Odoo's sanitizer keeps and the existing
   notes use: paragraphs → `<p>`, `- ` items → `<ul><li>`, `_…_` → `<em>`, `**…**` → `<strong>`, text HTML-escaped,
   no links. `html` = `<p><strong>Gebaut</strong></p>` + Gebaut + `<p><strong>To-do</strong></p>` + To-do (a section
   without items is omitted).

## Contract

`tickets.change-summary` (reva->odoo) gains the optional `release_log` object (`ReleaseLogEntryPayload`: `release:
str`, `ticket: int`, `title: str`, `status: str`, `modules: list[str]`, `html: str`); omitted when None
(`exclude_none`). The existing `notes` items are unchanged in shape; `note_html` may be `""`. Regenerate `contracts/`,
sync to `../Cloudunify`, bump the pin. An Odoo module without the field ignores it (Pydantic default) and posts the PR
lines only, so REVA can ship first.

## Data

New column `change_notes.source` (`claude` | `release-log`, default `claude`; migration `049_change_notes_source.sql`)
marks the rows — `model_name` already holds the Odoo model. Cost aggregation is unaffected (0 cost rows).

## Ops events

`release_log/info/release_log_entry_ambiguous`, `release_log/warning/release_log_parse_failed`,
`release_log/warning/release_log_entry_missing`. Existing change-note and summary events stay.

## Tests

- Parser and renderer against a fixture cut from the real `docs/releases/lollipop.md` (7 entries): entry lookup by
  ticket id, missing ticket, frontmatter, multi-paragraph Gebaut, To-do with `_menu path_` italics, an entry without
  To-do, `Entscheidungen`/`Nicht in diesem Release` ignored, HTML escaping of `<`/`&` in prose.
- Runner: covered ticket → no Claude call, row model `release-log`, cost 0; uncovered ticket → Claude path unchanged;
  two logs with the entry → open one wins + ops event; GitHub transient → re-raised; parse failure → fallback + event.
- Delivery: summary payload carries `release_log` with the rendered html and `note_html: ""` items; entry missing →
  no block + event; a ticket with only Claude notes → payload unchanged (no `release_log` key on the wire).
- Contracts: regenerated, drift test, inbound/outbound sample validation.

## Out of scope

- Changing when the summary is sent (still at ticket ready).
- The per-PR `/tickets/change-note` legacy path (untouched, still slated for removal).
- Release logs on branches other than the default branch.

## Counterpart: Odoo (`cu_reva_ticket_analysis`, next module version)

- `ChangeSummaryRequest.release_log: ReleaseLogBlock | None = None` (`release`, `ticket`, `title`, `status`,
  `modules: list[str]`, `html`).
- `_apply_reva_change_summary(notes, release_log=None)`: header as today; when `release_log` is given, post
  `<p><strong>{ticket} — {title}</strong> · Release {release} · {status} · {modules joined by ", "}</p>` followed by
  `html_sanitize(html)`, then the PR lines (a note with empty `note_html` renders its PR line only). Dedup key: the
  notes hash as today when `release_log` is None, otherwise the hash of `{notes, release_log}`.
- Tests: block posted once, sanitized, PR records still created, replay deduplicated, legacy payload unchanged; the
  synced `tickets.change-summary` sample (which will carry the block) replayed through the route.
- README/CLAUDE.md paragraphs on the batched note.
