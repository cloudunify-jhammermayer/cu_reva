## Task: incremental (delta) review

Review the **incremental changes** in the Task Parameters. The diff is only the
commits added to this PR **since the last review** — not the full PR diff. Find
genuine bugs, security, performance, and maintainability issues introduced by
these new commits. The severity, category, confidence, and conduct rules in the
guidance above apply.

**Scope constraints:**
- Report only issues traceable to the changed lines in this delta diff.
- Do not report pre-existing issues in unchanged parts of the file.
- Use Read for surrounding context, but only flag what the new changes introduce.
  (Findings from earlier reviews are reconciled separately — focus on what's new.)

## Review process

1. Read the diff in the Task Parameters section.
2. Use Read to examine the full content of each changed file.
3. Use Read to check imports, parent classes, related models, and tests.
4. Use Grep to find callers of changed functions when the change is non-trivial.
5. Keep only findings you are confident about (confidence ≥ 0.7).
6. Write your findings as JSON to `output_path`.

## Output format

Use the Write tool to write a JSON file to `output_path` with exactly this
structure (do **not** include a `risk_level` — the system computes it):

```json
{
  "summary": "2-3 sentence overview of the new changes and key concerns",
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
- If the delta looks clean, return an empty `findings` array with an informative summary.
