# Merge Note Project Switch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A per-project checkbox in Odoo (default off) decides whether a merged PR on a ticket or task without REVA issues posts REVA's merge note at all.

**Architecture:** Odoo only. A new Boolean on `project.project` is read at the top of `_apply_reva_change_summary`: a record without REVA issues whose project has the box unticked (or that has no project) drops the summary before anything is written; with the box ticked the existing behaviour runs (note, Merged PRs rows, ready flag, `ready` route or To-Do). REVA is unchanged and still gets a 200.

**Tech Stack:** Odoo 19 (Python, one XML view line, `de.po`), module `cu_reva_ticket_analysis`.

**Spec:** none. This plan implements the design brief Joseph approved in chat on 2026-10-02/04; the brief is restated under "Approved design" below and is the only source.

## Approved design

- New field `project.project.reva_merge_note_without_issues`, Boolean, default off, on the project form's REVA tab.
- For a record **without** REVA issues (`reva_issue_ids` empty):
  - box off, or the record has no project: nothing happens. No chatter note, no `reva.github.pr` row, no ready flag, no To-Do, no dedup key. The callback still answers 200.
  - box on: the note is posted and the ready effects of 19.0.57.2.0 fire (`reva_issues_ready`, `ready` route or "REVA: review & deploy" To-Do).
- A record **with** REVA issues is not affected by the box.
- The box does not depend on "Enable REVA".
- Known and accepted: REVA still drafts the note before Odoo drops it; merges dropped while the box was off are not replayed when it is ticked later.

## Global Constraints

- Two repos. Odoo repo: `/home/joseph/Projects/Cloudunify/Cloudunify`, branch `Prod`, module `custom_addons/cu_reva_ticket_analysis` (all Odoo paths below are relative to that module). REVA repo: `/home/joseph/Projects/Cloudunify/cu_reva`, branch `main`, docs only.
- **Never `git commit`, never `git push`.** Leave every change in the working tree. Joseph decides the commit route afterwards (amend `02bf52a` and force-push with lease, or a second commit); it is still open.
- Module version: `19.0.57.2.0` -> `19.0.57.3.0`. No migration script (a new Boolean defaults to false).
- No REVA code change, no contract change, no change in `cu_reva_connector`.
- Code comments and docs in English. German only in `i18n/de.po`.
- Surgical: touch only what the tasks name.
- Odoo test command (replace `<TAGS>`), run from anywhere:

```bash
A=/home/joseph/Projects/Cloudunify
$A/ast-odoo/.venv/bin/python $A/ast-odoo/odoo/odoo-bin -d cu_reva_test --db_host=/run/postgresql \
  --addons-path=$A/Cloudunify/custom_addons,$A/Cloudunify/3rd_party_addons,$A/ast-odoo/enterprise,$A/ast-odoo/odoo/addons \
  -u cu_reva_ticket_analysis,cu_reva_connector --test-enable --test-tags <TAGS> --stop-after-init \
  --http-port=8169 --log-level=warn --log-handler=odoo.tests.stats:INFO 2>&1 \
  | grep -E "FAIL: Test|ERROR: Test|odoo.tests.stats|tests.result|AssertionError"
```

- Reading the output: the log always contains `odoo.http: Exception during request handling` tracebacks from tests that post bad payloads on purpose. Judge only by `FAIL: Test` / `ERROR: Test` lines and the `odoo.tests.stats` counts.
- Known failure that predates this work and must be left alone: `TestReleaseMechanics.test_future_or_dateless_releases_are_not_overdue` (its fixture release date 2026-09-30 is in the past).

## Review Focus

Each line is pinned by a test in Task 1.

1. Record without REVA issues, project box off: the callback answers 200 and leaves no trace (no note, no PR row, not ready). Test: `test_a_summary_for_a_record_without_reva_issues_is_dropped_when_the_switch_is_off`.
2. Record with no project at all (helpdesk team without a project): treated like box off. Test: `test_a_record_without_a_project_gets_no_summary`.
3. Record with REVA issues in a project with the box off: still gets its summary. Test: `test_a_record_with_reva_issues_gets_its_summary_with_the_switch_off`.
4. The same summary sent again after the box was ticked: it is posted, because a dropped summary must not leave a dedup key behind. Test: `test_a_summary_dropped_while_off_is_posted_once_the_switch_is_on`.
5. Box unticked between two merges on the same task: the second merge fires nothing, the ready flag from the first stays. Test: `test_unticking_the_switch_stops_later_merges_and_keeps_the_ready_flag`.

---

### Task 1: Project switch, gate and tests

**Files:**
- Modify: `models/project_project.py` (new field after `reva_auto_support`)
- Modify: `views/project_project_views.xml` (one field line on the REVA tab)
- Modify: `i18n/de.po` (two entries)
- Modify: `wizard/cu_visibility_copy_wizard.py` (docstring list and `EXCLUDED_FIELDS`)
- Modify: `models/reva_mixin_callbacks.py` (`_apply_reva_change_summary`)
- Test: `tests/test_callback.py`, `tests/test_reva_service_routing.py`, `tests/test_traceability.py`, `tests/test_contracts.py`

**Interfaces:**
- Consumes: `reva.ticket.mixin._apply_reva_change_summary(notes, release_log=None) -> bool`, `_reva_notify_ready(route_note, todo_note)`, both already in `models/reva_mixin_callbacks.py`.
- Produces: `project.project.reva_merge_note_without_issues` (Boolean, default False). `_apply_reva_change_summary` returns `False` without side effects for a record without REVA issues whose project has it unticked.

- [x] **Step 1: Add the field**

In `models/project_project.py`, directly after the `reva_auto_support = fields.Boolean(...)` definition:

```python
    reva_merge_note_without_issues = fields.Boolean(
        string="Merge Note Without REVA Issues",
        default=False,
        help="Post REVA's \"Changes merged\" note, and mark the record ready for review/deploy, when a "
        "merged pull request names a ticket or task of this project that has no REVA issues (through its "
        "branch or title). Off: such a merge posts nothing. A record with REVA issues always gets its note.",
    )
```

- [x] **Step 2: Show it on the REVA tab**

In `views/project_project_views.xml`, after the line `<field name="reva_auto_support" invisible="not reva_enabled" />`:

```xml
                            <field name="reva_merge_note_without_issues" />
```

- [x] **Step 3: German translation**

In `i18n/de.po`, insert this entry directly **before** the entry whose msgid is `"Merged PRs"` (keep one blank line between entries):

```po
#. module: cu_reva_ticket_analysis
#: model:ir.model.fields,field_description:cu_reva_ticket_analysis.field_project_project__reva_merge_note_without_issues
msgid "Merge Note Without REVA Issues"
msgstr "Merge-Notiz ohne REVA-Issues"
```

and this entry directly **before** the entry whose msgid starts `"Pre-fill a new release's code freeze this many days before its release date."`:

```po
#. module: cu_reva_ticket_analysis
#: model:ir.model.fields,help:cu_reva_ticket_analysis.field_project_project__reva_merge_note_without_issues
msgid ""
"Post REVA's \"Changes merged\" note, and mark the record ready for "
"review/deploy, when a merged pull request names a ticket or task of this "
"project that has no REVA issues (through its branch or title). Off: such a "
"merge posts nothing. A record with REVA issues always gets its note."
msgstr ""
"Postet REVAs Notiz „Changes merged“ und markiert den Datensatz als bereit "
"für Review/Deployment, wenn ein gemergter Pull Request ein Ticket oder eine "
"Aufgabe dieses Projekts ohne REVA-Issues nennt (über Branch oder Titel). "
"Aus: Ein solcher Merge postet nichts. Ein Datensatz mit REVA-Issues erhält "
"seine Notiz immer."
```

- [x] **Step 4: Copy wizard bookkeeping**

In `wizard/cu_visibility_copy_wizard.py`:

In the module docstring, change

```
- `reva_enabled`, `reva_github_url`, `reva_auto_support`, and the five REVA
```

to

```
- `reva_enabled`, `reva_github_url`, `reva_auto_support`,
  `reva_merge_note_without_issues`, and the five REVA
```

In `EXCLUDED_FIELDS`, after `"reva_auto_support",` add:

```python
    "reva_merge_note_without_issues",
```

- [x] **Step 5: Give the existing change-summary tests a project with the box ticked**

These tests post summaries for records without REVA issues; with the box off by default they would all be dropped.

`tests/test_callback.py`, `TestRevaChangeSummaryCallback.setUpClass`: replace

```python
        cls.team = cls.env["helpdesk.team"].search([], limit=1)
        cls.ticket = cls.env["helpdesk.ticket"].create({"name": "Change Summary Ticket", "team_id": cls.team.id})
```

with

```python
        cls.project = cls.env["project.project"].create(
            {"name": "Change Summary Ticket Project", "reva_merge_note_without_issues": True}
        )
        cls.team = cls.env["helpdesk.team"].create(
            {"name": "Change Summary Team", "use_helpdesk_timesheet": True, "project_id": cls.project.id}
        )
        cls.ticket = cls.env["helpdesk.ticket"].create({"name": "Change Summary Ticket", "team_id": cls.team.id})
```

Same file, `test_header_on_a_task_says_ready_with_and_without_reva_issues`: replace

```python
        project = self.env["project.project"].create({"name": "Change Summary Project"})
```

with

```python
        project = self.env["project.project"].create(
            {"name": "Change Summary Project", "reva_merge_note_without_issues": True}
        )
```

`tests/test_traceability.py`, `TestRevaGithubPr.setUpClass`: replace

```python
        cls.team = cls.env["helpdesk.team"].search([], limit=1)
        cls.ticket = cls.env["helpdesk.ticket"].create({"name": "PR Ticket", "team_id": cls.team.id})
```

with

```python
        cls.project = cls.env["project.project"].create(
            {"name": "PR Ticket Project", "reva_merge_note_without_issues": True}
        )
        cls.team = cls.env["helpdesk.team"].create(
            {"name": "PR Ticket Team", "use_helpdesk_timesheet": True, "project_id": cls.project.id}
        )
        cls.ticket = cls.env["helpdesk.ticket"].create({"name": "PR Ticket", "team_id": cls.team.id})
```

`tests/test_contracts.py`, `test_change_summary_sample_is_accepted`: insert as the first lines of the test body:

```python
        project = self.env["project.project"].create(
            {"name": "Contract Merge Note Project", "reva_merge_note_without_issues": True}
        )
        self.ticket.team_id = self.env["helpdesk.team"].create(
            {"name": "Contract Merge Note Team", "use_helpdesk_timesheet": True, "project_id": project.id}
        )
```

`tests/test_reva_service_routing.py`, `setUpClass`: replace

```python
        cls.project = cls._make_project(name="Routing Project", reva_enabled=True)
```

with

```python
        cls.project = cls._make_project(
            name="Routing Project", reva_enabled=True, reva_merge_note_without_issues=True
        )
```

- [x] **Step 6: Write the failing tests**

`tests/test_callback.py`, in `TestRevaChangeSummaryCallback`, directly before `test_header_of_a_record_with_reva_issues_says_ready`:

```python
    def test_a_summary_for_a_record_without_reva_issues_is_dropped_when_the_switch_is_off(self):
        self.project.reva_merge_note_without_issues = False
        before = len(self.ticket.message_ids)
        resp = self._post(self.PATH, self._payload())
        self.assertEqual(resp.status_code, 200)
        self.ticket.invalidate_recordset()
        self.assertEqual(len(self.ticket.message_ids), before)
        self.assertFalse(self._prs())
        self.assertFalse(self.ticket.reva_issues_ready)

    def test_a_record_without_a_project_gets_no_summary(self):
        team = self.env["helpdesk.team"].create({"name": "No Project Team"})
        ticket = self.env["helpdesk.ticket"].create({"name": "No Project Ticket", "team_id": team.id})
        self.assertFalse(ticket.project_id)
        before = len(ticket.message_ids)
        resp = self._post(self.PATH, self._payload(ticket_id=ticket.id))
        self.assertEqual(resp.status_code, 200)
        ticket.invalidate_recordset()
        self.assertEqual(len(ticket.message_ids), before)
        self.assertFalse(self.env["reva.github.pr"].search([("helpdesk_ticket_id", "=", ticket.id)]))

    def test_a_record_with_reva_issues_gets_its_summary_with_the_switch_off(self):
        self.project.reva_merge_note_without_issues = False
        self.env["reva.github.issue"].create(
            {
                "helpdesk_ticket_id": self.ticket.id,
                "number": 42,
                "title": "Implement X",
                "url": "https://github.com/org/repo/issues/42",
                "state": "closed",
            }
        )
        self._post(self.PATH, self._payload())
        self.ticket.invalidate_recordset()
        self.assertIn("Changes merged — ready for review/deploy", self.ticket.message_ids[:1].body)
        self.assertEqual(len(self._prs()), 2)

    def test_a_summary_dropped_while_off_is_posted_once_the_switch_is_on(self):
        # A dropped summary must leave no dedup key behind, or the same body
        # sent again after the box is ticked would be taken for a replay.
        self.project.reva_merge_note_without_issues = False
        before = len(self.ticket.message_ids)
        self._post(self.PATH, self._payload())
        self.ticket.invalidate_recordset()
        self.assertEqual(len(self.ticket.message_ids), before)
        self.project.reva_merge_note_without_issues = True
        self._post(self.PATH, self._payload())
        self.ticket.invalidate_recordset()
        self.assertEqual(len(self.ticket.message_ids), before + 1)
```

`tests/test_reva_service_routing.py`, directly before `test_a_merge_summary_on_a_task_with_issues_leaves_ready_to_the_ready_callback`:

```python
    def test_the_merge_note_switch_is_off_by_default(self):
        fresh = self.env["project.project"].create({"name": "Fresh Project"})
        self.assertFalse(fresh.reva_merge_note_without_issues)

    def test_a_merge_with_the_project_switch_off_does_nothing(self):
        self.project.reva_merge_note_without_issues = False
        self._route(service=self.service)
        self._merged()
        self.assertFalse(self.task.reva_issues_ready)
        self.assertEqual(self.task.stage_id, self.build)
        self.assertFalse(self.task.activity_ids)

    def test_unticking_the_switch_stops_later_merges_and_keeps_the_ready_flag(self):
        self._route(service=self.service)
        self._merged(77)
        self.project.reva_merge_note_without_issues = False
        self._merged(78)
        self.assertTrue(self.task.reva_issues_ready)
        self.assertEqual(len(self._service_todos()), 1)
```

- [x] **Step 7: Run the tests to verify they fail**

Run the Odoo test command with `<TAGS>` =
`/cu_reva_ticket_analysis:TestRevaChangeSummaryCallback,/cu_reva_ticket_analysis:TestRevaServiceRouting,/cu_reva_ticket_analysis:TestRevaGithubPr,/cu_reva_ticket_analysis:TestTicketCallbackContracts`

Expected: exactly these five `FAIL: Test` lines, no `ERROR: Test` line:

- `test_a_summary_for_a_record_without_reva_issues_is_dropped_when_the_switch_is_off`
- `test_a_record_without_a_project_gets_no_summary`
- `test_a_summary_dropped_while_off_is_posted_once_the_switch_is_on`
- `test_a_merge_with_the_project_switch_off_does_nothing`
- `test_unticking_the_switch_stops_later_merges_and_keeps_the_ready_flag`

The other two new tests (`..._with_the_switch_off` for a record with issues, `..._off_by_default`) pass already; they guard behaviour the gate must not break.

- [x] **Step 8: Implement the gate**

In `models/reva_mixin_callbacks.py`, `_apply_reva_change_summary`.

Replace the last paragraph of the docstring

```python
        A record without REVA issues has no issue to close, so the merge is its
        ready event: each summary that is not a replay marks it ready and hands
        it over like the ready callback does (`_reva_notify_ready`).
        """
        self.ensure_one()
```

with

```python
        A record without REVA issues has no issue to close, so the merge is its
        ready event: each summary that is not a replay marks it ready and hands
        it over like the ready callback does (`_reva_notify_ready`). That whole
        path is opt-in per project (`reva_merge_note_without_issues`): unticked,
        or a record without a project, drops the summary before anything is
        written, the dedup key included.
        """
        self.ensure_one()
        # getattr: the abstract mixin has neither reva_issue_ids nor project_id.
        issueless = not getattr(self, "reva_issue_ids", False)
        if issueless:
            project = getattr(self, "project_id", self.env["project.project"].browse())
            if not project.reva_merge_note_without_issues:
                return False
```

At the end of the method, replace

```python
        # getattr: the abstract mixin has no reva_issue_ids. A record with REVA
        # issues turns ready through the ready callback, never from here.
        if not getattr(self, "reva_issue_ids", False):
```

with

```python
        # A record with REVA issues turns ready through the ready callback,
        # never from here.
        if issueless:
```

- [x] **Step 9: Run the tests to verify they pass**

Same command and tags as Step 7.
Expected: no `FAIL: Test` and no `ERROR: Test` line.

---

### Task 2: Version and documentation

**Files (Odoo module):**
- Modify: `__manifest__.py`, `CLAUDE.md`, `README.md`, `docs/consultant.md`, `docs/testguide.md`

**Files (REVA repo, `/home/joseph/Projects/Cloudunify/cu_reva`):**
- Modify: `README.md`, `HANDOFF.md`

**Interfaces:**
- Consumes: the field name `reva_merge_note_without_issues` and its label "Merge Note Without REVA Issues" from Task 1.
- Produces: nothing for later tasks.

- [x] **Step 1: Version**

`__manifest__.py`: `"version": "19.0.57.2.0"` -> `"version": "19.0.57.3.0"`.
`CLAUDE.md`: `- **Version** 19.0.57.2.0 -` -> `- **Version** 19.0.57.3.0 -`.

- [x] **Step 2: Module `CLAUDE.md`**

Replace

```
since 19.0.57.2.0 a summary for a record
  without REVA issues is that record's ready event (flag, `ready` route or REVA's To-Do via `_reva_notify_ready`), once
  per summary that is not a replay.
```

with

```
since 19.0.57.2.0 a summary for a record
  without REVA issues is that record's ready event (flag, `ready` route or REVA's To-Do via `_reva_notify_ready`), once
  per summary that is not a replay; since 19.0.57.3.0 that path is opt-in per project
  (`project.project.reva_merge_note_without_issues`, default off): unticked, or no project, drops the summary before
  anything is written (no note, no PR record, no dedup key), still answering 200.
```

Replace

```
  `reva_auto_support` (auto support draft for mail-created records, default off) + the five review-timesheet override
```

with

```
  `reva_auto_support` (auto support draft for mail-created records, default off) +
  `reva_merge_note_without_issues` (merge note and ready event for records without REVA issues, default off) + the five review-timesheet override
```

Replace

```
  (`reva_enabled`/`reva_github_url`/`reva_auto_support`/the five review-timesheet overrides) are
```

with

```
  (`reva_enabled`/`reva_github_url`/`reva_auto_support`/`reva_merge_note_without_issues`/the five review-timesheet overrides) are
```

- [x] **Step 3: Module `README.md`**

In the settings table, add this row directly after the row that starts `| Auto Support (per project)`:

```
| Merge Note Without REVA Issues (per project) | Checkbox on the project form, REVA tab (`reva_merge_note_without_issues`, off by default) — a merged pull request that names a ticket/task without REVA issues through its branch or title posts the "Changes merged" note and marks the record ready. Off: such a merge posts nothing. Independent of Enable REVA |
```

Replace

```
records idempotently. For a record without REVA issues the summary is also its ready event: it sets the ready
flag and fires the `ready` route, or REVA's own "REVA: review & deploy" To-Do, once per merged PR. When the repository's release log has an entry for
```

with

```
records idempotently. For a record without REVA issues the summary is also its ready event: it sets the ready
flag and fires the `ready` route, or REVA's own "REVA: review & deploy" To-Do, once per merged PR. This is opt-in
per project (`reva_merge_note_without_issues`, off by default): with the box unticked, or for a record without a
project, the callback answers 200 and posts nothing. When the repository's release log has an entry for
```

- [x] **Step 4: Module `docs/consultant.md`**

Replace

```
A ticket without REVA issues, whose PR
   names it through the branch or title, gets the note at each merge instead. Each such merge also marks the ticket
   ready and hands it over like "all issues closed" does (the project's ready service, or the "REVA: review & deploy"
   To-Do for the assignees).
```

with

```
A ticket without REVA issues, whose PR
   names it through the branch or title, gets the note at each merge instead, but only when
   **Merge Note Without REVA Issues** is ticked on the project's REVA tab (off by default). Each such merge also marks
   the ticket ready and hands it over like "all issues closed" does (the project's ready service, or the
   "REVA: review & deploy" To-Do for the assignees). With the box unticked such a merge posts nothing.
```

- [x] **Step 5: Module `docs/testguide.md`**

Replace

```
a ticket with no REVA issues also turns **Ready** and gets the review & deploy To-Do)
```

with

```
a ticket with no REVA issues needs **Merge Note Without REVA Issues** ticked on its project, then also turns **Ready** and gets the review & deploy To-Do; unticked, nothing is posted)
```

- [x] **Step 6: REVA repo `README.md`**

Replace

```
merged PR, and Odoo (module 19.0.57.2.0) treats each such summary as the
ticket's ready event, the same as all issues closed.
```

with

```
merged PR, and Odoo treats each such summary as the ticket's ready event, the
same as all issues closed. Odoo only does so when the ticket's project has
"Merge Note Without REVA Issues" ticked (module 19.0.57.3.0, off by default);
otherwise it accepts the callback and posts nothing.
```

- [x] **Step 7: REVA repo `HANDOFF.md`**

Replace

```
   19.0.57.2.0 (2026-10-02) each such summary is the ticket's ready event:
   ready flag, `ready` route or the "REVA: review & deploy" To-Do.
```

with

```
   19.0.57.2.0 (2026-10-02) each such summary is the ticket's ready event:
   ready flag, `ready` route or the "REVA: review & deploy" To-Do. Since
   19.0.57.3.0 Odoo posts it only when the ticket's project has "Merge Note
   Without REVA Issues" ticked (off by default); REVA still drafts and sends it.
```

- [x] **Step 8: Verify the replacements landed**

```bash
cd /home/joseph/Projects/Cloudunify
grep -c "reva_merge_note_without_issues" Cloudunify/custom_addons/cu_reva_ticket_analysis/CLAUDE.md Cloudunify/custom_addons/cu_reva_ticket_analysis/README.md
grep -c "Merge Note Without REVA Issues" Cloudunify/custom_addons/cu_reva_ticket_analysis/docs/consultant.md Cloudunify/custom_addons/cu_reva_ticket_analysis/docs/testguide.md cu_reva/README.md
grep -c "Merge Note" cu_reva/HANDOFF.md
grep -n '"version"' Cloudunify/custom_addons/cu_reva_ticket_analysis/__manifest__.py
```

Expected: `CLAUDE.md:3`, `README.md:2`, then `1` for each of the three files, `1` for `HANDOFF.md`, and `"version": "19.0.57.3.0"`.

---

### Task 3: Full verification and hand-off

**Files:**
- Move: `docs/superpowers/plans/2026-10-04-merge-note-project-switch.md` -> `docs/superpowers/plans/archive/` (REVA repo; part of the shipping change, per the repo's CLAUDE.md)

**Interfaces:**
- Consumes: the finished working trees of Tasks 1 and 2.
- Produces: a verified, uncommitted change in both repos for Joseph to commit.

- [x] **Step 1: Full module suites**

Run the Odoo test command with `<TAGS>` = `/cu_reva_ticket_analysis,/cu_reva_connector`.

Expected: `cu_reva_connector: 42 tests`, `cu_reva_ticket_analysis: 1360 tests` (1353 + 7 new), and exactly one `FAIL: Test` line: `TestReleaseMechanics.test_future_or_dateless_releases_are_not_overdue` (predates this work). Any other `FAIL: Test` or `ERROR: Test` line is a defect of this change: fix it before going on.

- [x] **Step 2: Field shows on the form**

```bash
A=/home/joseph/Projects/Cloudunify
$A/ast-odoo/.venv/bin/python $A/ast-odoo/odoo/odoo-bin shell -d cu_reva_test --db_host=/run/postgresql \
  --addons-path=$A/Cloudunify/custom_addons,$A/Cloudunify/3rd_party_addons,$A/ast-odoo/enterprise,$A/ast-odoo/odoo/addons \
  --log-level=warn <<'EOF'
arch = env["project.project"].get_view(view_type="form")["arch"]
print("on form:", 'name="reva_merge_note_without_issues"' in arch)
print("default:", env["project.project"].default_get(["reva_merge_note_without_issues"]))
EOF
```

Expected: `on form: True` and `default: {'reva_merge_note_without_issues': False}`.

- [x] **Step 3: Archive this plan**

```bash
cd /home/joseph/Projects/Cloudunify/cu_reva
git mv docs/superpowers/plans/2026-10-04-merge-note-project-switch.md docs/superpowers/plans/archive/ 2>/dev/null \
  || mv docs/superpowers/plans/2026-10-04-merge-note-project-switch.md docs/superpowers/plans/archive/
```

- [x] **Step 4: Report the state, do not commit**

```bash
cd /home/joseph/Projects/Cloudunify/Cloudunify && git status --short
cd /home/joseph/Projects/Cloudunify/cu_reva && git status --short
```

Expected in the Odoo repo: fourteen module files of Tasks 1 and 2 modified (`__manifest__.py`, `CLAUDE.md`, `README.md`, `docs/consultant.md`, `docs/testguide.md`, `i18n/de.po`, `models/project_project.py`, `models/reva_mixin_callbacks.py`, `views/project_project_views.xml`, `wizard/cu_visibility_copy_wizard.py`, four test files), plus the untracked `.claude/`. Expected in the REVA repo: `README.md`, `HANDOFF.md` and the plan file.

Report to Joseph: test counts, the one pre-existing failure, and that the commit route is his call. After the module upgrade, a project needs the box ticked before its issue-less tickets get merge notes again (Wenatex included).
