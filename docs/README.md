# docs/ — current design specs & plans

The **authoritative** design record for REVA as it actually works today
(unlike the legacy numbered docs in [`../doc`](../doc)). Written with the
Superpowers brainstorming → spec → plan workflow.

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

## Why this lives apart from `doc/`

`doc/` froze at the original Messages-API design and is kept only as history.
New design work lands here as a spec + plan, and module behaviour is documented
in each module's `README.md`. When code and a numbered `doc/` file disagree, the
code (and these specs / the READMEs) win.
