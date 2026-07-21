# Scanner feed (GitHub security alerts as review context) — design

**Date:** 2026-07-05
**Component:** `cu_reva` — `reva/github_client.py` (3 alert readers), new `reva/scanner_feed.py`, `Reviewer` param wiring, secret-severity floor, guidance section, RepoConfig kill switch.
**Status:** Design approved (Q&A with Joseph, 2026-07-05), pending implementation plan.

## Problem

Customer repos carry GitHub security signals (code-scanning alerts,
Dependabot alerts, secret-scanning alerts) that the review never sees — REVA
can praise a diff whose file has an open CodeQL alert or whose dependency
bump is the vulnerable one. Roadmap Tier-5 M/H, never planned.

## Context (verified 2026-07-05)

- `GitHubClient._get(token, path, params, …)` is the shared REST helper
  (Bearer + api-version headers, Transient/Permanent mapping,
  `allow_404`) — the three alert endpoints slot in as thin readers.
- The `manifest_audit` pattern is the template: deterministic pre-computed
  context as an optional nonce-fenced skill param, omitted when empty,
  fail-open on collection errors.
- Deterministic severity calibration exists (`_calibrate_odoo_severity`) —
  the template for the secret-alert floor.
- Ops-event invariant is live; per-repo config via `RepoConfig`.

### Locked decisions

1. **v1 sources = the three GitHub security APIs only**: code-scanning
   alerts, Dependabot alerts, secret-scanning alerts. **CI-artifact ingestion
   (semgrep/gitleaks JSON) was considered and explicitly dropped by Joseph
   (2026-07-05) as the fragile quarter — revisit only when a concrete need
   appears.**
2. Relevance filtering: code-scanning → alerts on the PR's **changed files**;
   Dependabot → only when the diff touches dependency manifests
   (`requirements*.txt`, `pyproject.toml`, `package.json`, `__manifest__.py`
   dependency edits); secret-scanning → **all open alerts, always** (small,
   critical).
3. Presentation: one optional fenced `scanner_alerts` skill param, ≤20
   entries, deduped, each `tool | rule | severity | file:line | one-liner`.
   Guidance: *hints to verdict, not findings to copy* — confirm in the
   diff/clone, cite the customer's file.
4. **Deterministic floor:** a produced finding whose file:line matches an
   open secret-scanning alert location is raised to `critical` (post-hoc,
   the calibration pattern).
5. Available on **all review modes**; per-repo kill switch
   `.claude-review.yml scanner_feed: false`; fail-open per source (a repo
   without GitHub Advanced Security or missing App permission → source
   skipped + ONE ops event per run, `component="scanner_feed"`,
   severity `warning`).
6. **Operator prerequisite:** GitHub App permission bump — code scanning
   (`security_events: read`), Dependabot alerts (read), secret scanning
   (read). Until granted, every source 403s and the feature is silently a
   no-op (visible via ops events).

### Explicitly out of scope

- CI-artifact ingestion (dropped, see above). Uploading REVA's own findings
  AS code-scanning alerts (reverse direction). Blocking behavior based on
  alerts. Alert state management (dismissing/resolving alerts from REVA).

## Design

**GitHub readers (`reva/github_client.py`):**
`list_code_scanning_alerts(token, owner, repo) -> list[dict]`,
`list_dependabot_alerts(token, owner, repo) -> list[dict]`,
`list_secret_scanning_alerts(token, owner, repo) -> list[dict]` — each: one
`_get` page (`state=open`, `per_page=100`; one page is plenty for context),
`allow_404`-tolerant, 403 mapped to a typed `ScannerUnavailable` outcome
rather than an exception bubbling out.

**Collector (`reva/scanner_feed.py`):**
`collect(github, token, owner, repo, changed_files) -> ScannerFeed` with
`ScannerFeed(entries: list[ScannerEntry], unavailable: list[str])`;
`ScannerEntry(tool, rule, severity, file, line, description)`. Normalizes
the three payload shapes, applies the relevance filters + cap (priority:
secrets > code-scanning > dependabot), and `format_param(feed) -> str` for
the skill param. Pure + unit-testable on fixture payloads.

**Reviewer wiring:** next to `manifest_audit`: collect (fail-open,
try/except → ops event), attach `skill_params["scanner_alerts"]` when
non-empty. Post-hoc: `_floor_secret_findings(findings, feed)` raises
matching findings to `critical`. Guidance section in `review_guidance.md`
(CHANGELOG bump — **coordinate with the triage plan: whichever lands second
uses the next version number**).

## Error handling

| Case | Behavior |
|---|---|
| Source 403/404 (no GHAS, missing permission, feature off) | source skipped; listed in `ScannerFeed.unavailable`; one ops event per run |
| Transient GitHub error during collection | whole feed skipped this run (fail-open) + ops event; review proceeds |
| >20 relevant alerts | capped by priority; note appended to the param ("N more omitted") |
| Alert without a location | included with `file="-"` (dependabot) or skipped (code-scanning needs a path to be actionable) |

## Testing

Readers against the existing GitHub-client test pattern (mock transport:
one happy page, 403 → unavailable, 404 → unavailable); collector on fixture
payloads (filtering, cap priority, normalization); reviewer wiring (param
attached/omitted, fail-open + ops event, secret floor end-to-end on a fake
finding); prompt version test. Staging gate: one repo with GHAS enabled —
confirm alerts appear fenced in the prompt and a planted secret alert floors
its finding.
