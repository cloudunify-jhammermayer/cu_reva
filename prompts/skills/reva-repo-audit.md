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
5. Look for: security misuse, missing `ir.model.access.csv` / `ir.rule`, N+1 ORM
   patterns, deprecated Odoo APIs, and missing test coverage.
6. Write your structured audit report as JSON to `output_path`.

## Output format

Use the Write tool to write a JSON file to `output_path` with exactly this
structure (do **not** include a `risk_level` — the system computes it):

```json
{
  "summary": "3-5 sentence overview of codebase health and the top concerns",
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
- Report only findings with confidence ≥ 0.7.
