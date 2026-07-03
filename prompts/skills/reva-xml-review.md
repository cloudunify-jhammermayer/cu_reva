## Task: Odoo XML / QWeb view review

This PR changes only Odoo XML — view definitions, QWeb templates, or data
records. Review them against Odoo's view conventions, using the clone to resolve
inheritance targets. The severity, category, confidence, and conduct rules in the
guidance above apply. Set `is_odoo_specific: true` and use `category: odoo` (or
`security` for a CSP/escaping issue, `bug` for a broken reference).

## View / QWeb criteria

1. **Inheritance targets resolve** — for every `<xpath expr="...">`, `inherit_id`,
   or `ref="..."`, Read the referenced view/record in the clone (Grep for the
   `id`/`name`) and confirm the target element/record exists. A target that doesn't
   exist is a runtime error → **major** `bug`. If the parent view lives under
   `odoo/` or `enterprise/`, you may Read it to resolve the target but **never report
   a finding on those third-party files** — only on the team's own XML.
2. **`t-esc` → `t-out`** — `t-esc` is deprecated in QWeb; prefer `t-out`. **Minor**.
3. **CSP: inline `<script>` / external CDN** — inline `<script>` blocks or assets
   loaded from an external CDN are blocked by Odoo 18+ Content-Security-Policy and
   break the page. **Major**. (Use this wording so the issue is recognised consistently.)
4. **Explicit `inherit_id`** — view inheritance must use an explicit `inherit_id`
   reference (required in Odoo 19). Flag implicit/missing inheritance.
5. **`<card>` in Kanban** — Odoo 19 Kanban views use the `<card>` element; flag the
   old structure where it applies. **Minor**.
6. **`noupdate="1"`** — data records that should not be overwritten on upgrade need
   `noupdate="1"`. **Minor** where appropriate.

Calibrate confidence on inheritance-resolution findings conservatively — a fuzzy
match against a parent view you could only partially read should be ≤ 0.8.

## Review process

1. Read the XML diff in the Task Parameters section.
2. Read each changed XML file in full, and Read/Grep the inheritance and ref targets
   in the clone to confirm they exist.
3. Apply the criteria above.
4. Verify each candidate finding per the guidance ("Verify before you write"), then keep only what survives, scored honestly.
5. Write your findings as JSON to `output_path`.

## Output format

Use the Write tool to write a JSON file to `output_path` with exactly this
structure (do **not** include a `risk_level` — the system computes it):

```json
{
  "summary": "What the view changes do; the top concern (or none); what you verified clean — see the guidance Summary contract",
  "findings": [
    {
      "severity": "major",
      "category": "odoo",
      "file": "custom_addons/module/views/partner_views.xml",
      "line_start": 12,
      "line_end": 14,
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
- If the views look clean, return an empty `findings` array with an informative summary.
