## Task: diff review

Review the GitHub pull request described in the Task Parameters. Find genuine
bugs, security vulnerabilities, performance problems, and maintainability
issues — not style nitpicks unless they cause real harm. The severity,
category, confidence, and conduct rules in the guidance above apply.

## Review process

1. Read the diff in the Task Parameters section.
2. Use Read to examine the full content of each changed file.
3. Use Read to check imports, parent classes, related models, and tests for the changed code.
4. Use Grep to find callers of changed functions when the change is non-trivial.
5. If a `test_coverage` parameter is present, verify the listed modules genuinely
   lack tests for the new logic and, if so, emit a `category: test` finding per the
   guidance severity rules.
6. Keep only findings you are confident about (confidence ≥ 0.7).
7. Write your findings as JSON to `output_path`.

## Manifest checks

If a `manifest_audit` parameter is present, a module's `__manifest__.py` changed
and REVA ran **deterministic** structural checks (missing data files,
security-before-views order, version format). Surface those as findings at the
suggested severity — trust them, don't re-derive. Additionally, for the changed
module, check that addons used in the changed files (`from odoo.addons.X`, cross-addon
`ref=`/`inherit_id`) are declared in `depends`; flag a missing one as **major**
`is_odoo_specific: true`. Scope this to what the changed files reveal.

## Output format

Use the Write tool to write a JSON file to `output_path` with exactly this
structure (do **not** include a `risk_level` — the system computes it):

```json
{
  "summary": "2-3 sentence overview of the PR quality and key concerns",
  "findings": [
    {
      "severity": "critical",
      "category": "bug",
      "file": "custom_addons/module/models/partner.py",
      "line_start": 42,
      "line_end": 45,
      "title": "Short, specific title (max 80 chars)",
      "body": "What's wrong and why it matters.",
      "suggestion": "Concrete fix, or null",
      "confidence": 0.9,
      "is_odoo_specific": true
    }
  ]
}
```

- `file`, `line_start`, `line_end`, `suggestion` may be `null`.
- `line_start`/`line_end` are line numbers on the new (post-change) side.
- If the PR looks clean, return an empty `findings` array with an informative summary.
