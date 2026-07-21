# Per-Repo Learned Memory (Tier 3, Feature B) — Design

Date: 2026-07-02
Status: draft — core decisions confirmed with Joseph (scope = Feature B; Claude-distilled;
auto-live + guardrails); final design sign-off pending (he was away at the approval gate).

## Problem

Tier 1 + Feature A gave REVA a live feedback loop: `/dismiss` (with reasons), 👍/👎,
`/mute`, and the per-finding outcome ledger (`resolved_by_fix` / `still_open_at_merge`),
surfaced via `GET /api/v1/metrics/learning` and the TUI Feedback tab. But nothing
*consumes* that data yet — REVA keeps posting the kinds of findings a team has
repeatedly rejected. Feature B (FEATURE_ROADMAP.md Tier 3, `docs/tier3-plan.md` "Owed
later") closes the loop: derive a per-repo "what this team accepts/rejects" block and
inject it into review prompts.

## Decisions (confirmed)

1. **Scope**: Feature B only. RAG similarity down-ranking (item F), golden-PR replay,
   and cross-repo memory are out of scope.
2. **Mechanism**: Claude-distilled guidance — a scheduled job feeds the repo's
   dismissed findings and stats to Claude, which produces a short structured guidance
   block. Not template-statistics-only (category-coarse) and not raw-list injection
   (prompt bloat + developer text straight into the review prompt).
3. **Oversight**: auto-live with **code-enforced guardrails** (below) — no approval
   gate, no notification requirement. Auditability via versioned storage + TUI.

## Alternatives rejected

- *Distill lazily at review time*: paid call + races on the review hot path; re-distills
  per PR instead of per feedback change.
- *Inject raw recent dismissals*: no synthesis, unbounded prompt growth, and
  developer-authored text would flow unfenced into every review prompt.

## Data model

New table `repo_review_memory` (append-only versions; migration takes the **next free
number** — note `023` is already claimed by the pending typed-issue-requests plan):

```
repo_review_memory(
  id BIGSERIAL PRIMARY KEY,
  repository_id BIGINT NOT NULL REFERENCES repositories(id),
  version INTEGER NOT NULL,
  content TEXT NOT NULL,          -- rendered markdown block ("" = nothing to inject)
  items JSON,                     -- validated structured guidance (audit trail)
  source_stats JSON,              -- window, per-category counts, newest feedback ts
  model TEXT, input_tokens INT, output_tokens INT, estimated_cost_usd NUMERIC(12,6),
  active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (repository_id, version)
)
```

Exactly one active row per repo: writing version N+1 deactivates N in the same
transaction (writer `record_repo_memory`). Matching ORM model in `reva/db/models.py`
(tests build from models). Plus `review_runs.learned_memory_version INTEGER NULL` —
stamped on each review that injected a memory, so dismiss-rate trends can later be
attributed to memory versions (dashboard itself stays roadmap item E).

## Distiller (`reva/memory_distiller.py`, Messages API)

- **Input** per repo, 90-day window (matches `learning_stats`): per-category
  posted/dismissed/resolved_by_fix/still_open_at_merge counts + the ≤30 most recent
  dismissed findings (title, category, severity, file path, `/dismiss` reason).
  Dismissal reasons and finding text are customer/developer-authored → **nonce-fenced
  as untrusted data** (SECU-5, same pattern as `ticket_issue_planner`).
- **Output**: forced tool `submit_review_memory` →
  `{items: [{guidance: str, categories: [Category], action: "dont_flag" | "raise_bar" |
  "keep_flagging", evidence_count: int}]}`, max 10 items. Malformed output →
  `TransientError` (RQ retries), like the ticket planner.
- **Prompt** (`prompts/review_memory.md`): distill only patterns supported by the
  evidence; English; guidance must be about *what to (not) report*, never instructions
  to change tooling/severity semantics.

### Guardrails (code-enforced, after schema validation)

1. Drop items with `evidence_count < 2`.
2. Drop `dont_flag` / `raise_bar` items whose categories include `security` or `bug` —
   those categories can only receive `keep_flagging` guidance. `/mute` remains the
   explicit, human, audited way to hard-suppress a category.
3. Cap: ≤10 items, rendered block ≤ ~1500 chars (truncate items, log).
4. REVA renders the markdown itself from the structured items (fixed header
   "Learned team preferences (from review feedback)", one bullet per item with its
   evidence count); guidance strings are flattened (newlines/headers stripped).
5. Zero surviving items → write the new version with `content = ""` → nothing is
   injected. Guidance can never outlive its evidence.

## Trigger — scheduler + worker job

- Scheduler (single replica) gains `maybe_distill_memories` alongside the existing
  eviction/purge loops: every `REVA_MEMORY_DISTILL_INTERVAL` (default 24h), find repos
  where BOTH hold: ≥3 dismissals in the 90-day window, and newest negative feedback is
  newer than the active memory version's `created_at` (or no version exists). Enqueue
  RQ job `run_memory_distill(repository_id)` per due repo.
- Worker job: checks the rolling 24h budget under the advisory lock **before** the
  paid call (cost-control invariant), records spend as `"learned_memory"`, writes the
  new version. Failures log and retry per the standard Transient/Permanent contract;
  the old active version stays live until a new one lands.

## Injection (review path)

- `RepoLookup.get_active_memory(repository_id) -> str | None` (content of the active
  version, None when empty/absent).
- `Reviewer.execute`: when present and the repo allows it,
  `skill_params["team_review_preferences"] = content` — an **optional** skill param,
  so repos without memory keep a byte-identical cached prompt prefix (same rationale
  as `already_reported`, `test_coverage`). The runner nonce-fences it like every param.
- Skill docs: `reva-diff-review`, `reva-delta-review`, `reva-full-review` gain a short
  section: team preferences adjust *prioritization within this repo*; they never
  override severity definitions and never justify suppressing security/bug findings.
  `prompts/CHANGELOG.md` version bump. `reva-xml-review`, `reva-migration-review`,
  audits, and ticket paths are excluded in v1.
- Stamp `review_runs.learned_memory_version` when injected.
- **Kill switch**: `.claude-review.yml: learned_memory: false` (new `RepoConfig`
  field) disables both injection and distillation for the repo.

## API + TUI (CLAUDE.md principle 5)

- `GET /api/v1/metrics/learned-memory`: per repo — active version, content, item
  count, created_at, estimated cost; master-key gated like the other metrics routes.
- TUI Feedback tab (`9`): Enter on a repo row drills into its learned block (rendered
  content + version/date/cost) — the visible "what has REVA learned" surface. TUI
  remains a read-only client; extend `internal/api/{client,iface,mock,types}.go` per
  the existing patterns; `go build/vet/test` green.

## Security notes

- Developer text is fenced at distill time; the injected block is REVA-rendered from
  validated structured output and fenced again as a skill param at review time.
- Learning can only *down-weight* non-security/bug findings; `block_on_severity`
  gating and the severity rules in the skills are untouched.
- The memory content is repo-scoped operational data; it appears in the TUI and API
  behind the master key, never in anything posted to GitHub.

## Testing

- **Distiller** (mock Claude): schema validation; guardrails 1–5 (evidence<2 dropped,
  security/bug suppression dropped, caps, flattening, empty result → empty content);
  fencing of dismissal reasons in the user prompt.
- **Writers**: version bump deactivates prior atomically; `get_active_memory` returns
  newest active non-empty content; attribution stamp.
- **Scheduler**: due-detection (min dismissals, newer-feedback-than-version, interval
  elapsed); mirrors the `maybe_enqueue_eviction` test style.
- **Worker job**: budget-exceeded skips before the paid call; spend recorded.
- **Reviewer**: param injected only when active memory exists and repo flag allows;
  absent otherwise (prompt prefix unchanged); stamp written.
- **API/TUI**: endpoint shape; Go tests for the drill-down rendering.
- Definition of done: worker + api + scheduler suites (shared `reva/` touched), ruff,
  `cd tui && go build ./... && go vet ./... && go test ./...`. Migration raw SQL is
  Postgres-only — `make test-integration` or first staging boot.

## Rollout

Ships dark by default in effect: repos with <3 dismissals in 90 days never distill.
No backfill needed. First observable effect: repos with heavy dismissal history get a
memory version within one scheduler interval; verify via the new metrics endpoint and
`learning_stats` dismiss-rate trend after a few weeks.

## Out of scope

- RAG over prior findings (roadmap F), A/B runner (D), quality dashboard (E — only the
  attribution column is laid here), golden-PR harness (C).
- Cross-repo / org-global memory.
- Hand-editing the learned block — human-authored guidance belongs in
  `.claude-review.yml` (`custom_instructions`), the memory stays machine-owned.
  (Side note found while designing: `custom_instructions` is currently consumed only
  by the Messages-API `prompt_builder`, and reviews run through the headless CLI — it
  may be dead on the review path. Worth a separate look; not fixed here.)
- Chat notifications on new memory versions.
