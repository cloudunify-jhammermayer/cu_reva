# docs/ — guides, design specs & plans

The **authoritative** docs for REVA as it actually works today. Setup and
operations guides live here directly (`setup-local.md`, `setup-production.md`,
`egress-lockdown.md`, `security-scanning-setup.md`, `ticket-analysis.md`,
`odoo-module.md`). Design specs and plans live under `docs/superpowers/`,
written with the brainstorming → spec → plan workflow.

```
docs/superpowers/
  specs/    Design specs — the "what & why" for each feature (approved before build)
  plans/    Implementation plans — the step-by-step "how", checked off as built
```

## Key specs

| Spec | Covers |
|---|---|
| `specs/2026-05-25-headless-claude-design.md` | **The current architecture** — dual Claude clients (Messages API + headless CLI), repo clone cache, skills, repo audit. Start here. |
| `specs/2026-05-26-incremental-review-design.md` | Delta / incremental reviews (compare diff since last reviewed SHA). |
| `specs/2026-05-22-odoo-reva-ticket-analysis-design.md` | Odoo ticket analysis. |
| `specs/2026-05-22-follow-up-check-design.md` | Inline-comment reply follow-ups. |
| `plans/*` | The matching implementation plans (e.g. headless-claude, incremental-review, security-hardening). |

## Roadmap & feature work

| Doc | Covers |
|---|---|
| `../FEATURE_ROADMAP.md` | The 6-tier roadmap. |
| `tier0-plan.md`, `tier1-plan.md`, `tier2-plan.md` | Per-tier plans + status (Tiers 0–2 shipped). |
| `tier2-detailed-plans.md` | Exhaustive, verified per-feature plans for Tier-2 features 4–9. |
| `tier2-staging-runbook.md` | **What to do before testing Tier-2 features 4–9 on a live worker** — setup + per-feature pass/fail scenarios. |

## Source of truth

New design work lands here as a spec + plan; module behaviour is documented in
each module's `README.md`. When an older spec and the code disagree, the code
(and the READMEs) win.
