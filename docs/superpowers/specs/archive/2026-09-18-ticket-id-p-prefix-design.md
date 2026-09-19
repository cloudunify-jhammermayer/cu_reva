# `P`-prefixed ticket ids in the PR ticket signal — Design

- **Date:** 2026-09-18
- **Status:** implemented 2026-09-18; merged to `main` 2026-09-19.
- **Repos:** cu_reva only. No Odoo change, no contract change, no migration.
- **Context:** requested by Joseph 2026-09-18. Addendum to the ticket-level PR signal
  (`2026-07-20-pr-review-ticket-signal-design.md`), which extracts the Odoo
  ticket id from the PR's head branch or title when no REVA-created issue is linked.
- **Sequencing:** the support-answer-images work is in progress in the same working
  tree and **has priority**. This change shares no file with it; ship it as its own
  commit (see *Files*) and never fold it into that work's commit.

## Problem

Developers write the Odoo task reference as `P7624` (the display ref for a project
task, the counterpart of `H1213` for a helpdesk ticket). The extractor only accepts a
bare number or an `H`-prefixed one, so a PR carrying `P<id>` resolves to no ticket and
REVA sends Odoo nothing: no reviewed badge, no "REVA reviewed PR" chatter line, and —
the reported symptom — **no auto review time entry** on the task.

Observed on `Cloudunify/hackmair-platzhirsch`:

| PR | Head branch | Title | Extracted before |
|----|-------------|-------|------------------|
| #1 | `feat/P7624` | `[CONF] P7624 render cu_template v1.5.0` | nothing |
| #2 | `stage` | `[CONF] P7624 promote cu_template bootstrap to production` | nothing |

## Decision

An optional leading `P` (case-insensitive) is accepted wherever an optional `H` is
today, and means `project.task`. `H` and bare numbers are unchanged.

**A `P` reference never matches a helpdesk ticket** (Joseph, 2026-09-18). Task and
helpdesk ids are separate sequences, so helpdesk ticket 7624 and task 7624 are
different records. Until now the extracted model was only a guess: the lookup matched
REVA's DB by number alone and let that row's model win. For `P` the model is a filter.

| Reference | Model | DB lookup |
|-----------|-------|-----------|
| `7624` | `project.task` | guess — a DB row of either model wins (unchanged) |
| `P7624` / `p7624` | `project.task` | **strict — only `project.task` rows may match** |
| `H1213` / `h1213` | `helpdesk.ticket` | guess — a DB row of either model wins (unchanged) |

A strict reference with no `project.task` row in REVA's DB goes to the active
`is_default` instance as `project.task`; with no default instance it is unknown
(existing `no_default_instance` ops event) — it never borrows the helpdesk row.

Applies to all three patterns in `reva/ticket_links.py`, in the existing precedence
(head branch → title tag → title token):

- branch: `feat/P7624`
- title tag: `[CONF] P7624 …`
- title token: `… conf/P7624 …`

## Change

- The three regexes replace `(h)?` with `([hp])?`; group 1 is the prefix letter.
- `extract_ticket_id` returns `(ticket_id, model_name, strict)` instead of
  `(ticket_id, model_name)`. `strict` is True only for an explicit `P`.
- `resolve_ticket_by_id` gains keyword-only `strict_model: bool = False`. When set, both
  DB rungs (`ticket_issue_runs`, `ticket_analyses`) additionally filter on
  `model_name == default_model`. Default off, so every existing caller and the
  bare/`H` paths behave exactly as before.
- `board_status_runner` (the only caller of both) passes the flag through.

Unchanged on purpose: the 9-digit cap, the rejection of id `0`, the version-dot guard
(`[MIG] 17.0 …` is not a ticket), and the requirement that digits follow the prefix
directly (`feat/portal` is not a ticket, same as `feat/hotfix`).

## Files

- `reva/ticket_links.py` — regexes, `extract_ticket_id`, `resolve_ticket_by_id`.
- `worker/worker/board_status_runner.py` — unpack and pass `strict_model` (one call site).
- `worker/tests/test_ticket_links.py` — new tests; existing `extract_ticket_id`
  expectations gain the third element (`False` for bare and `H`).
- `worker/tests/test_board_status_runner.py` — one end-to-end test.
- this spec and its plan, `docs/superpowers/plans/archive/2026-09-18-ticket-id-p-prefix.md`.

## Testing

Test-first, in two rounds; every new test was watched failing before the code changed.

Extraction (`test_ticket_links.py`):

- `test_extract_p_prefixed_branch_is_project_task` — `feat/P7624` → `(7624, "project.task", True)`
- `test_extract_p_prefix_is_case_insensitive` — `feat/p7624`
- `test_extract_p_prefixed_title_tag_is_project_task` — branch `stage` + the real PR #2 title
- `test_extract_p_prefixed_title_token_is_project_task` — `conf/P7624`
- `test_extract_bare_p_without_digits_is_not_a_ticket` — `feat/portal` (guard, passes before and after)

Strict lookup (`test_ticket_links.py`):

- `test_resolve_by_id_strict_model_ignores_helpdesk_issue_run` — helpdesk issue run for
  the same repo and number is skipped; resolves to the default instance as `project.task`
- `test_resolve_by_id_strict_model_ignores_helpdesk_analysis` — a newer helpdesk analysis
  loses to an older `project.task` one
- `test_resolve_by_id_strict_model_without_default_is_none` — only a helpdesk row and no
  default instance → `None`

End to end (`test_board_status_runner.py`):

- `test_fallback_p_prefixed_ticket_never_lands_on_helpdesk` — helpdesk ticket 97 known for
  the repo, PR branch `feat/P97` → callback carries `project.task` 97. Before the change
  this sent `helpdesk.ticket` 97.

Focused files: 94 passed. Full worker suite: 1727 passed, 15 skipped. Ruff clean.

## Rollout

Deploy the **worker** only — `extract_ticket_id` has one caller,
`worker/worker/board_status_runner.py`. API and scheduler are unaffected.

Nothing is sent retroactively: the runner skips merged/closed PRs, so PR #1 and #2
above stay unbooked. The next open PR with a `P<id>` reference is the first to map.

A resolved ticket still only books when the existing preconditions hold: a REVA review
finishes on the open PR (`review_done` → `in_review`), the repo has not set
`work_status: false`, the task exists in Odoo and has a project, and a REVA timesheet
employee is configured on the project or in the system parameters. For a ticket REVA
has never seen, an active `is_default` Odoo instance must exist.

## Open

- **`H` is still only a hint.** The mirror case — `H1213` landing on `project.task`
  1213 because that is the only 1213 REVA has seen — is unchanged; only `P` was asked
  for. Making `H` strict too is one expression at the two return sites of
  `extract_ticket_id` (`prefix == "p"` → `prefix in ("h", "p")`). Bare numbers must stay
  a guess: they predate the prefix convention and can be either model.

## Out of scope

- Re-sending signals for already merged PRs.
- Any Odoo-side change (`cu_reva_ticket_analysis` receives a plain `ticket_id` + `model_name`).
- Making `H` strict (see *Open*).
