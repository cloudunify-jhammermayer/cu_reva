# Tier 3 — Self-improvement: plan & status

**TLDR — why Tier 3.** Tiers 0–2 made REVA *review better*; Tier 3 makes it *learn*. The
Tier-1 data loop (feedback capture + the per-finding outcome ledger) is now live but mostly
fed by one weak signal (thread resolved/unresolved). Tier 3 turns REVA's own review threads
into a cheap, structured feedback channel and then uses that data to stop repeating findings a
team has already rejected — raising precision without prompt guesswork. It is the spine the
"does REVA actually get better over time?" story hangs off.

Roadmap (from `FEATURE_ROADMAP.md`), in dependency order:

| # | Feature | E/V | Depends on |
|---|---------|-----|-----------|
| A | **`/dismiss` & `/mute <category>` commands** | M/H | — (foundational) |
| B | Per-repo learned memory | L/H | A + real feedback data |
| C | Golden-PR replay harness | L/H | — (needs curated PRs) |
| D | A/B comparison runner | M/M | C |
| E | Quality dashboard (TUI) | M/M | Tier-1 data |
| F | RAG over prior findings | L/M | A/B feedback volume |
| G | Per-author trust digest | S/M | Tier-1 data |

**Sequencing.** Start with **A** — it's the only Tier-3 item with no data/curation
prerequisite, it's fully unit-testable here, and it produces the structured signal **B** needs.
Everything else either depends on A's data or on a curated golden set we don't have yet.

---

## Feature A — `/dismiss` & `/mute` inline commands (this slice)

### Problem
Today, replying to one of REVA's inline finding comments enqueues a **paid** conversational
Claude reply (`run_comment_reply`). There is no cheap, structured way for a developer to say
"this finding is wrong" or "stop flagging this category" — and the dormant `review_feedback`
table only gets the coarse thread-resolved/unresolved signal. Result: no clean per-finding
reject signal, and no way to suppress a category a team doesn't care about.

### What ships
Three trusted-user commands, recognised as **replies on REVA's inline comments**, handled
structurally at **zero Claude cost** (they short-circuit before `run_comment_reply`):

- **`/dismiss`** `[reason]` — record a structured *negative* signal on this finding
  (`review_feedback`, `reaction="dismissed"`). The cleanest per-finding reject label for Tier-3 B.
- **`/mute <category>`** — mute a finding category **repo-wide**; REVA stops posting findings in
  that category. (`<category>` defaults to the replied-to finding's category if omitted.)
- **`/unmute <category>`** — lift a mute.

Mute delivers immediate, observable behaviour now; dismiss is pure capture that feeds B.

### Design
- **Routing.** In `_handle_review_comment`, parse the reply's first token. If it's a known
  command → handle it structurally and **return** (no paid reply). Otherwise enqueue the reply
  exactly as today. The handler gains a `db` argument (the dispatcher already has `db`).
- **Authorization & anti-loop.** Reuse the existing gates: `_is_bot_sender` (no loops) and
  `_TRUSTED_ASSOCIATIONS` (owner/member/collaborator only) — same bar as slash commands.
- **`/dismiss`** → `lookup_finding_by_comment_id(in_reply_to_id)` → `record_feedback(...,
  reaction="dismissed", is_positive=False)`. Idempotent on the existing unique constraint. If the
  reply isn't on a REVA finding thread, log and ignore.
- **`/mute` / `/unmute`** → validate `<category>` against the canonical `Category` set
  (`reva/types.py`); upsert the repo (like `_handle_issue_comment`) to get `repository_id`; write
  to a new **`muted_categories`** table (migration `016`). Idempotent per `(repository_id,
  category)`; `/unmute` deactivates.
- **Suppression.** `Reviewer.execute` drops findings whose category is muted for the repo,
  **after** Odoo calibration and **before** capping (mirrors `_drop_thirdparty_findings`), via a
  new `RepoLookup.get_muted_categories(repository_id) -> set[str]`. Logs `findings_dropped_muted`.
- **Discoverability.** Document the commands in `README.md`; add a one-line hint to the PR review
  body so developers find them.

### Schema (migration `016_muted_categories.sql`)
```
muted_categories(
  id, repository_id FK, category TEXT, muted_by TEXT,
  active BOOLEAN default true, created_at, updated_at,
  UNIQUE(repository_id, category)
)
```
No change to `review_feedback` beyond a new `reaction` *value* (`"dismissed"`).

### Safety notes
- Muting is a **trusted-user, reversible (`/unmute`), audited (`muted_by` + timestamps)** action.
  It can hide real issues (e.g. muting `security` repo-wide) — clearly logged (`findings_dropped_muted`),
  and `block_on_severity` still governs whatever remains. Documented as a deliberate team choice.
- Zero Claude cost: these commands never reach `run_comment_reply`.

### Testability
Fully unit-testable here (no live CLI): command parsing/routing in `_handle_review_comment`
(dismiss records feedback + does NOT enqueue a reply; an ordinary reply still enqueues); `/mute`
writes/validates a category and `/unmute` deactivates; `get_muted_categories`; the reviewer drops
muted-category findings before capping; unknown category / non-finding thread are no-ops.

### Test plan
- `api/tests/test_webhooks.py`: `/dismiss` records feedback, no reply enqueued; `/mute security`
  writes a row; `/mute` invalid category ignored; `/unmute` deactivates; plain reply still enqueues;
  untrusted/bot sender ignored.
- `worker/tests/test_db.py`: mute/unmute upsert + idempotency; `get_muted_categories` returns active only.
- `worker/tests/test_reviewer.py`: a muted-category finding is dropped (and risk recomputed on the
  remainder); unmuted categories pass through.

### Milestones
- **M1** — this plan. ✅
- **M2** — `/dismiss` capture: webhook routing + `reaction="dismissed"` feedback + tests. ✅
- **M3** — `muted_categories` (migration 016) + `/mute` `/unmute` + reviewer suppression + tests. ✅
- **M4** — discoverability (README + review-body hint) + full suites + commit. ✅

**Shipped.** Feature A complete: worker 615 / api 151 / scheduler 28, ruff clean. No prompt change.

No prompt/skill change → no CHANGELOG version bump (these are handler/pipeline features).

### Owed later (not this slice)
- **B (learned memory)** consumes the `dismissed` signals: derive a per-repo "what this team
  rejects" block and inject it into the prompt. A's data is the input; B is its own slice.
