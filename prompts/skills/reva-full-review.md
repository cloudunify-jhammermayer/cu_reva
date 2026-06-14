## Task: full repository-aware review

Perform a thorough review of the pull request in the Task Parameters. Unlike a
diff-only review, you have the full repository — use it. The severity, category,
confidence, and conduct rules in the guidance above apply.

**If CodeGraph tools are available** (`mcp__codegraph__*`), prefer them for
structural questions over grepping: `codegraph_context` to understand an area,
`codegraph_callers`/`codegraph_callees` to trace usage, `codegraph_impact` to
gauge a change's blast radius. They query a prebuilt index, so they are cheaper
and more complete than a grep/read sweep. Use Read/Grep for details the graph
doesn't cover.

**Skip machine-generated and vendored files** — lockfiles (`*.lock`,
`package-lock.json`, `yarn.lock`, `go.sum`, …), minified assets (`*.min.js`,
`*.min.css`), source maps, build artifacts, and vendored dependencies. They are
not human-authored; reviewing them is noise. Review only source the team wrote.

## Review process

1. Read the diff to understand what changed.
2. For each changed file, use Read to examine the full file, not just the changed lines.
3. Trace relationships: imports, base classes, computed fields, related models, XML views, test files.
4. Find all callers of changed APIs across the addons directory (prefer
   `codegraph_callers` when available; otherwise Grep).
5. Check whether tests exist for the changed behaviour; flag missing coverage. If
   a `test_coverage` parameter is present, treat its listed modules as confirmed
   gaps and emit `category: test` findings per the guidance severity rules.
6. Write your findings as JSON to `output_path`.

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

- `file`, `line_start`, `line_end`, `suggestion` may be `null`. Prefer a line
  that maps to the diff so the finding can be posted inline; otherwise leave
  `file`/`line_start` null and it appears in the summary.
- If the PR looks clean, return an empty `findings` array with an informative summary.
