# REVA — What it does and how you use it

REVA is our in-house review & evaluation agent, built on Claude. It reviews every
pull request like a senior Odoo developer, turns customer tickets into structured
requirements and GitHub issues, keeps Odoo and GitHub in sync while the work
happens, and reports what it found and what it cost.

You never talk to REVA directly — you meet it where you already work: on GitHub
pull requests, on Odoo tickets and tasks, and in Google Chat.

## The loop at a glance

```
Odoo ticket ──▶ REVA analysis ──▶ GitHub issues (+ project board)
                                        │
                              developer opens a PR
                                        │
                        REVA reviews the PR (+ requirements check)
                                        │
                     issues close · board cards move · Odoo updates
                                        │
                    "ready for review/deploy" flag on the ticket
```

---

## For developers — on GitHub

### Automatic PR reviews
Open a PR (or push to one) on a connected repo — that's it. REVA waits 10 minutes
for you to stop pushing, then reviews the change against a full local clone of the
repo (it reads connected files, not just the diff) and posts:

- a **Check Run** (green/red — red only for findings at `major` severity or above,
  configurable per repo),
- a **PR review** with a summary and **inline comments** on the exact lines,
- at most 15 findings, each with severity, category, and a suggested fix where
  it makes sense.

Pushing again after a review only re-reviews what changed since the last look
(incremental review). Odoo view/QWeb XML is reviewed with a dedicated XML
reviewer; Odoo **upgrade/migration scripts** get a specialist migration review.

### Manual commands (PR comments)
| Comment | What you get |
|---|---|
| `/review` | Standard diff review, immediately (no debounce) |
| `/review-all` | Diff review of *every* changed file, not just `custom_addons/` |
| `/full-review` | Repo-aware review of the whole change |
| `/deep-review` | Same, on the strongest model (Opus) — for the gnarly ones |

### Ask REVA about its findings
Reply to any of REVA's inline comments with a question ("why is this a problem?",
"would X be better?") — REVA answers in the same thread, with the finding's full
context.

### Teach it, for free
Reply to an inline finding with a command (no AI call, instant):

| Command | Effect |
|---|---|
| `/dismiss [reason]` | "This finding was wrong" — recorded as negative feedback |
| `/mute <category>` | Stop findings of that category **on this repo** (e.g. `/mute style`) |
| `/unmute <category>` | Lift the mute |

Dismissals and fixes feed REVA's per-repo **learned memory**, so recurring noise
actually goes away over time.

### Requirements check
If the PR is linked to GitHub issues (closing keywords like `Fixes #12`, or the
sidebar "Development" link), REVA also checks: *does this PR actually do what the
issue asked?* Each linked issue gets a verdict — ✅ matches · 🟡 partial ·
❌ does not match · ❓ unclear — with a one-line justification. **Advisory only**:
it never turns the Check Run red.

### Repository audits
On demand (ops dashboard or API), REVA reviews an **entire repository** on the
default branch with the deep model. Major/critical findings are opened as GitHub
issues labelled `reva-audit`; re-runs never duplicate an open issue.

---

## For consultants — in Odoo

### Ticket analysis
On a Helpdesk ticket or Project task, click **Analyse with REVA**. Minutes later
the record carries a structured analysis (German or English input is fine,
attachments like a spec `.docx`/`.pdf` are read too):

- Summary — is this ticket clear enough to build?
- **Missing information** — the questions to ask the customer *before* work starts
- Odoo-specific notes (consultant-level observations)
- **Standard Odoo Coverage** — does stock Odoo already do this? Grounded in the
  official Odoo docs for the instance's version, with references.
- **Existing Customizations** — does one of *your* custom addons already do (or
  touch) this? Grounded in the addon docs in the project's own repository —
  shown when the project has a GitHub repo configured.
- A **dev-time estimate** per user story (low-end, AI-assisted hours) with
  assumptions and a total

### Create GitHub issues from a ticket
Click **Create Issues**: REVA plans 1–10 well-scoped GitHub issues (plus a parent
epic) with acceptance criteria, creates them in the project's repository, and
links them back onto the ticket. Optional per request: a fixed issue type
(BUG/FEAT/CR/…), a GitHub assignee, and a **project board + due date** (below).
Each issue carries its own hour estimate; the ticket shows the per-issue
estimates and the total.

### Project board, kept up to date automatically
If the request names a GitHub Projects board, REVA places every issue (and the
epic) on it with **Due date**, **Estimate**, **Priority**, and Status **Todo** —
and then moves the cards as work happens:

- a developer opens a PR that references the issue → card moves to **In Progress**
- REVA finishes reviewing that PR → card moves to **In review**
- the issue closes → GitHub's native **Done**

### The ticket stays in sync
- Issue closed or reopened on GitHub → the ticket's issue list updates.
- **All** issues closed → the ticket is flagged **ready for review/deployment**
  and the requester gets a to-do activity. (Deployment itself stays a human step —
  REVA never marks a ticket done.)
- A PR that closes one of the issues gets **merged** → a change note lands in the
  ticket's chatter: what changed, in consultant language.

### Timesheet wording review
Timesheet lines can be sent in batch to REVA, which flags/rewrites wording that
shouldn't reach a customer invoice ("fixed stupid bug" → something presentable)
and marks lines that need a human decision.

---

## For the team — oversight and reporting

- **Weekly report** in Google Chat: reviews done, findings by severity, cost,
  durations, per-repo and per-author breakdowns, top recurring findings.
- **Ops dashboard (TUI)**: live view of reviews, findings, failures (with one-key
  requeue), the debounce queue, ticket analyses with a full **per-ticket journey
  timeline** (analysis → issues → PRs → reviews → ready), audit results, feedback
  and mutes per repo, Odoo instances with per-instance spend, timesheet runs.
- **Docs site**: consultant-facing repo documentation, browsable at `/docs`.
- **Cost control**: every run's cost is recorded; a rolling 24-hour budget cap
  (globally and per Odoo instance) declines work instead of overspending.
  Typical real-world costs are small — see `technical.md`.

---

## Connecting a repository

1. Install the GitHub App on the repo (Contents read; PRs, Checks, Issues write).
2. Add the repo in the ops dashboard (Repos tab, `n`, `owner/name`).
3. Optional: commit a `.claude-review.yml` for per-repo tuning — review scope,
   size caps, severity gate, category mutes, custom reviewer instructions, and
   feature switches (requirements check, board sync, change notes, …). Sensible
   defaults apply without it.

## What REVA deliberately does *not* do

- It never merges, closes, or blocks anything by itself — the Check Run gate is
  the only enforcement, and its severity threshold is your choice per repo.
- The requirements check is advisory by construction.
- It never marks an Odoo ticket as done — deployment stays a human decision.
- It doesn't touch your project board's columns or options — it only moves cards
  between statuses that already exist, and only for issues it created itself.
- Raw customer ticket text is scrubbed from REVA's database after 30 days.
