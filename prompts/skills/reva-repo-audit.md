## Task: full repository audit

Perform a comprehensive audit of the repository in the Task Parameters. This is
**not** a PR review — explore the whole codebase and identify *systemic* issues.
The severity, category, confidence, and conduct rules in the guidance above
apply. You have the Read, Grep, and Glob tools (no shell).

**If CodeGraph tools are available** (`mcp__codegraph__*`), use them to map and
reason about structure cheaply: `codegraph_files` for the module/file inventory,
`codegraph_context` to survey an area, `codegraph_callers`/`codegraph_impact` to
trace coupling and blast radius. They query a prebuilt index. Keep using Grep for
regex anti-pattern sweeps (e.g. `sudo\(`, `cr\.execute`) — that is what Grep is
for — and Read to confirm every candidate.

**Skip machine-generated and vendored files** — lockfiles (`*.lock`,
`package-lock.json`, `yarn.lock`, `go.sum`, …), minified assets (`*.min.js`,
`*.min.css`), source maps, build artifacts, and vendored dependencies. They are
not human-authored; auditing them is noise. Focus on source the team wrote.

## Audit process

1. Use **Glob** to map the module structure: `**/__manifest__.py`
   (addons live under `custom_addons/` or `custom-addons/`).
2. Read each manifest plus the key models/controllers it declares.
3. Use **Grep** to surface common anti-patterns across the repo, e.g.:
   - `sudo\(` — unrestricted privilege escalation
   - `cr\.execute` — raw SQL (check for string interpolation → SQL injection)
   - `cr\.commit|cr\.rollback` — manual transaction control
   - controllers with `auth=['\"]none` 
4. Use **Read** to confirm each candidate before reporting it — don't flag on a
   grep hit alone.
5. **Security-model consistency.** For each module, Grep `_name = ` to enumerate
   the models it declares (a new `_name`, or an `_inherit` that creates a new model —
   not a plain `_inherit` extending an existing one), then Read its
   `security/ir.model.access.csv` and `security/*.xml`: report every model with no
   access line (**major**) and every company-scoped model (`company_id` /
   `_check_company_auto`) with no `ir.rule` (**major**). Also look for general
   security misuse, N+1 ORM patterns, deprecated Odoo APIs, and missing test coverage.
6. **For each module's `__manifest__.py`**, parse it and verify systematically:
   `depends` is complete for the addons the module actually uses; every `data`/`demo`
   file exists on disk; `data` loads `security/` before views; `version` is the
   5-part `N.N.N.N.N` form. There is no `manifest_audit` param on the audit path —
   derive these yourself with Read/Glob.
7. Write your structured audit report as JSON to `output_path`.

## Output format

Use the Write tool to write a JSON file to `output_path` with exactly this
structure (do **not** include a `risk_level` — the system computes it):

```json
{
  "summary": "Overall codebase health; the top systemic concerns; what you verified clean — see the guidance Summary contract",
  "findings": [
    {
      "severity": "major",
      "category": "security",
      "file": "custom_addons/module/models/partner.py",
      "line_start": null,
      "line_end": null,
      "title": "Short, specific title (max 80 chars)",
      "body": "Detailed explanation of the systemic issue.",
      "suggestion": "Concrete fix, or null",
      "confidence": 0.85,
      "is_odoo_specific": true
    }
  ]
}
```

- Focus on systemic patterns, not one-off style issues.
- `file`/`line_start` may be `null` for repo-wide findings.
- Verify each finding per the guidance ("Verify before you write") and score confidence honestly — the system enforces the reporting threshold.

## Core knowledge

When the task parameters include `core_knowledge`, additional read-only
directories with Odoo core, enterprise, and the official documentation are
available. Use them in this order: (1) grep the catalog directory named in
the parameter — one file per core module listing its models and fields;
(2) read core source only to confirm specifics; (3) use the documentation
tree for functional/settings questions. Apply the standard-functionality
check from the review guidance. Absence of a catalog hit is weak evidence —
verify in source before relying on it.
