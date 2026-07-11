# Review precision metrics + dismissal-rate alarm — Design

- **Date:** 2026-07-11
- **Status:** approved (design), not yet planned
- **Context:** feature brainstorm 2026-07-11. Research consensus (Graphite/Digital
  Applied 2025 rollout guides; Greptile's address-rate work): dismissal rate is the
  metric that predicts whether a team keeps an AI reviewer enabled — tune when
  >30% of findings are dismissed. REVA records the raw signals (dismissals, fixes,
  mutes per repo/category — the Feedback tab's data) but computes no rate, surfaces
  no trend, and alarms on nothing.

## Problem

A repo where REVA has become noise degrades silently: developers `/dismiss` (or
just ignore) findings, trust erodes, and nobody notices until the bot is resented.
The signals are already in the database.

## Design

1. **Metric definition** (one place, `reva/db/writers.py` or a query module):
   per repo over a rolling 30-day window —
   - `findings_posted` (inline findings actually posted, post-cap/post-mute),
   - `dismissed` (explicit `/dismiss`),
   - `fixed` (the learned-memory fix-detection outcome),
   - `dismissal_rate = dismissed / findings_posted`,
   - `addressed_rate = fixed / findings_posted`.
   Findings neither dismissed nor fixed count as unaddressed context, not as a
   third rate. Verify the exact outcome fields against the learned-memory tables
   during planning — the plan must map to what's persisted, not re-model it.
2. **API**: extend the existing feedback endpoint (`/api/v1` — whatever the TUI
   Feedback tab reads today) with the per-repo window aggregates. No new table —
   computed on read; the window is small.
3. **TUI** (Feedback tab): add per-repo `dismissed%` / `fixed%` columns next to
   the existing signal counts. A repo above the alarm threshold renders with the
   warning style the TUI already uses elsewhere.
4. **Weekly report**: one new section — per-repo precision (posted / dismissed% /
   fixed%), flagged rows marked. Costs nothing extra; the report already
   aggregates per repo.
5. **Alarm** (scheduler operational-alerts loop, same pattern as queue-depth):
   daily check — any repo with `findings_posted >= 10` in the window AND
   `dismissal_rate > REVA_DISMISSAL_RATE_ALERT` (default `0.30`, `0` disables)
   → Google Chat alert (needs the existing `GOOGLE_CHAT_WEBHOOK_URL`) + ops
   event (`component="feedback"`, `kind="dismissal_rate_high"`). One alert per
   repo per 7 days (persisted marker, same dedup approach as the weekly report),
   so a bad repo doesn't page daily.

## Explicitly out of scope

- No automatic behavior change (no auto-muting, no threshold-driven review
  changes) — the alarm informs a human who then tunes `.claude-review.yml`.
- No per-author metrics (harmful-metric territory per the research).
- No new DB tables; read-side aggregation only.

## Testing

- Metric math on seeded feedback rows (incl. zero-findings repos — no division
  by zero, excluded below the min-sample floor).
- Alarm: fires above threshold with sample floor met; silent below floor;
  respects the 7-day dedup; disabled at `0`; ops event recorded alongside chat.
- API/TUI: aggregate fields present; Go rendering test for the new columns.

## Expected behavior

Zero cost (no model calls). The Feedback tab answers "is REVA noisy on repo X?"
at a glance; the weekly report trends it; Chat pings when a repo crosses the
line with enough sample to mean it.
