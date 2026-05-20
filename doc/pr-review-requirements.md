# PR Review Requirements Specification

## Purpose

This document defines what ARIA reviews, how it classifies findings, and what output contract Claude must follow. It is the authoritative reference for:

- Prompt engineering (what goes into `system.md`)
- Finding validation (what the worker enforces)
- GitHub output formatting (how findings appear on PRs)
- TUI metrics (how findings are aggregated)

---

## 1. Severity Levels

Every finding has exactly one severity level. Severity determines whether it blocks merging.

### 🔴 Critical

**Definition**: Will cause data loss, security vulnerability, production outage, or regulatory violation if merged.

**Action required**: Must fix before merge. Check Run = `failure`.

**Examples**:
- SQL injection (string-formatted queries with user input)
- Authentication bypass (missing auth check on endpoint)
- Credentials or secrets hardcoded in source code
- Data deletion without backup or confirmation
- Race condition that can corrupt transactional data
- Missing access control on a model (no `ir.model.access` for new Odoo model)
- `sudo()` in a public-facing controller without input validation
- Unvalidated redirect (open redirect vulnerability)
- Missing CSRF protection on state-changing endpoint
- Broken encryption or hashing (MD5 for passwords, ECB mode)

### 🟠 Major

**Definition**: Bug, logic error, or significant risk that will likely cause problems in production. Not an immediate security threat but will degrade quality or reliability.

**Action required**: Should fix before merge. Check Run = `failure`.

**Examples**:
- Logic error in conditional (wrong operator, missing edge case)
- N+1 query in a loop that will hit production data volumes
- Missing error handling on external API call
- Database migration that drops data without a fallback
- Incorrect `@api.depends` that will cause stale compute values
- Missing `ir.rule` for multi-company data isolation
- Using deprecated API (`name_get()`, `odoo.osv`, `_sql_constraints` in Odoo 19)
- Broken exception handling (bare `except:` swallowing errors)
- Missing transaction safety on multi-step operation
- Test that always passes regardless of behavior (assertion-free test)
- Type mismatch that will fail at runtime
- Missing validation on user input that reaches the database

### 🟡 Minor

**Definition**: Improvement opportunity. Code works but could be better. Not likely to cause problems but reduces maintainability or quality.

**Action required**: Consider fixing. Check Run = `neutral` (does not block).

**Examples**:
- Unused import or variable
- Missing docstring on a public method
- Complex method that should be split (>50 lines, >3 levels of nesting)
- Magic number without a named constant
- Inconsistent naming (mixing camelCase and snake_case)
- Missing type hints on function signature
- Suboptimal query that could use `search_fetch()` instead of `search()` + `browse()`
- Redundant code that could be simplified
- Missing `noupdate="1"` on XML data that shouldn't be overwritten on upgrade
- Using `t-esc` instead of `t-out` in QWeb templates (deprecated)
- Field naming not following Odoo convention (`partner` instead of `partner_id`)

### 🔵 Info

**Definition**: Observation, suggestion, or positive feedback. No action required.

**Action required**: None. Check Run unaffected.

**Examples**:
- "Good error handling pattern here."
- "Consider extracting this into a utility function for reuse."
- "This could benefit from a `CLAUDE.md` entry explaining the EDI format."
- "FYI: Odoo 19 has a new `_filtered_access` method that might simplify this."
- "Naming is clear and consistent throughout this module."
- "Test coverage is thorough — nice work."

---

## 2. Finding Categories

Every finding has exactly one category.

| Category | Scope |
|---|---|
| `bug` | Logic errors, incorrect behavior, edge cases, crashes |
| `security` | Injection, auth, access control, secrets, input validation |
| `performance` | N+1 queries, unnecessary computation, missing indexes, memory leaks |
| `maintainability` | Complexity, readability, naming, code structure, DRY violations |
| `test` | Missing tests, weak assertions, flaky test patterns |
| `docs` | Missing or incorrect documentation, docstrings, README |
| `style` | Formatting, convention violations, cosmetic issues |
| `architecture` | Design concerns, coupling, separation of concerns, API design |
| `odoo` | Odoo-specific issues (ORM misuse, security model, view inheritance, migration) |

The `odoo` category is only used when `framework: odoo` is set in the repo config. Odoo findings that also fit another category (e.g., a security issue related to `sudo()`) should use the more specific category (`security`) and set `is_odoo_specific: true`.

---

## 3. Confidence Scoring

Every finding includes a confidence score from 0.0 to 1.0.

| Range | Meaning | Example |
|---|---|---|
| 0.90–1.00 | Very confident. Clear, unambiguous issue. | SQL injection with obvious user input |
| 0.70–0.89 | Confident. Likely an issue, context supports it. | Missing error handling on API call |
| 0.50–0.69 | Moderate. Possible issue, worth the developer checking. | Potential performance concern (depends on data volume) |
| 0.30–0.49 | Low. Speculative, might be intentional. | "This looks complex — consider refactoring" |
| 0.00–0.29 | Very low. Uncertain observation. | "I'm not sure if this is intentional" |

**Guidance for Claude**: Use 0.9+ sparingly — only for findings you could defend in a code review meeting. Most findings should be 0.6–0.85. If you're below 0.5, consider whether the finding is worth reporting.

**Configuration**: The default config posts all findings (min_confidence: 0.0). Repos can raise the threshold in `.claude-review.yml` to suppress low-confidence noise.

---

## 4. Risk Level

Every review has an overall risk level assessment.

| Risk Level | Definition |
|---|---|
| `low` | No significant concerns. Minor improvements possible. |
| `medium` | Some issues worth addressing. No blocking problems. |
| `high` | Major issues present. Should not merge without fixes. |
| `critical` | Critical issues present. Must not merge. |

Risk level mapping:
- 0 findings or only info → `low`
- Only minor findings → `low` or `medium` (based on count)
- Any major finding → `high`
- Any critical finding → `critical`

---

## 5. Structured Output Contract

Claude MUST respond with this exact JSON structure. No preamble, no markdown fences, just raw JSON.

```json
{
  "summary": "string — 2-4 sentence overview of the PR quality and key concerns",
  "risk_level": "low | medium | high | critical",
  "findings": [
    {
      "severity": "info | minor | major | critical",
      "category": "bug | security | performance | maintainability | test | docs | style | architecture | odoo",
      "file": "string | null — relative file path from repo root",
      "line_start": "integer | null — first line of the issue",
      "line_end": "integer | null — last line of the issue (same as line_start for single-line)",
      "title": "string — max 80 chars, specific and actionable",
      "body": "string — detailed explanation of what's wrong and why it matters",
      "suggestion": "string | null — concrete code fix or approach",
      "confidence": "float — 0.0 to 1.0",
      "is_odoo_specific": "boolean — true if this is an Odoo-specific finding"
    }
  ]
}
```

### Validation Rules (Enforced by Worker)

1. Response must be valid JSON.
2. `summary` must be present and non-empty.
3. `risk_level` must be one of: `low`, `medium`, `high`, `critical`.
4. `findings` must be an array (can be empty).
5. Each finding must have: `severity`, `category`, `title`, `body`, `confidence`.
6. `severity` must be one of: `info`, `minor`, `major`, `critical`.
7. `category` must be one of the defined categories.
8. `confidence` must be a float between 0.0 and 1.0.
9. If `file` is present, it must match a file in the diff.
10. Maximum 15 findings. If Claude returns more, only the top 15 by severity × confidence are kept.
11. The worker forces structured output via Claude's `tool_use` mechanism (`submit_review` tool). Free-form JSON responses are rejected as a permanent failure.
12. In addition to the 1000-line diff guard, the worker estimates input tokens (`len(diff) / 4`) and declines reviews whose diff alone would exceed the context budget (default 60k tokens). This prevents context-window failures even when the line count is under the configured limit.

### What Happens on Validation Failure

If Claude's response fails validation, the worker:
1. Stores the raw response in `review_runs.raw_response` for debugging.
2. Marks the review as `failed` with `error_class = "permanent"`.
3. Posts a Check Run with conclusion `failure` and a note that the review engine produced an invalid response.
4. Sends a failure notification to Google Chat.
5. Does NOT retry (this is a prompt issue, not a transient error).

---

## 6. Odoo 19 Specific Review Checklist

When `framework: odoo` is configured, ARIA applies these additional checks:

### Must Check (Critical / Major if violated)

| Check | Severity |
|---|---|
| Direct `cr.execute()` with string formatting → SQL injection | Critical |
| `sudo()` in controller with `auth='public'` without input validation | Critical |
| New model without `ir.model.access.csv` entry | Major |
| Multi-company model without `ir.rule` | Major |
| Controller with `auth='none'` | Major |
| Manual `cr.commit()` or `cr.rollback()` without justification | Major |
| `@api.depends` missing a field that the compute reads | Major |
| `@api.onchange` writing to database | Major |
| Migration script with destructive operation and no rollback plan | Major |

### Should Check (Minor if violated)

| Check | Severity |
|---|---|
| Using `name_get()` instead of `display_name` | Minor |
| Using `odoo.osv` namespace | Minor |
| Using `_sql_constraints` instead of `Constraint` class | Minor |
| Using `inselect` operator (removed in 19) | Minor |
| Using `group_operator` instead of `aggregator` | Minor |
| Using `_flush_search()` (deprecated) | Minor |
| Using `t-esc` instead of `t-out` in templates | Minor |
| `search()` + `browse()` instead of `search_fetch()` | Minor |
| Many2one field not ending with `_id` | Minor |
| Many2many/One2many not ending with `_ids` | Minor |
| Missing `noupdate="1"` on config data | Minor |
| Inline `<script>` or CDN loading (CSP violation) | Minor |
| `__manifest__.py` data files in wrong order | Minor |
| Static assets from external URLs | Minor |

### Good to Note (Info)

| Check | Severity |
|---|---|
| New ORM features available (`any!` operator, GROUPING SETS) | Info |
| `check_access` / `has_access` / `_filtered_access` available | Info |
| Native OR in record rules available (simplification opportunity) | Info |
| `search_fetch()` optimization available | Info |
| Good use of Odoo 19 patterns | Info (positive feedback) |

---

## 7. Examples of Good vs Bad Findings

### Good Finding

```json
{
  "severity": "critical",
  "category": "security",
  "file": "controllers/partner_api.py",
  "line_start": 42,
  "line_end": 42,
  "title": "SQL injection via string-formatted query",
  "body": "The query on line 42 uses f-string formatting to insert `partner_name` directly into SQL. This parameter comes from the HTTP request body and is not sanitized. An attacker can inject arbitrary SQL to read, modify, or delete database records.",
  "suggestion": "Use parameterized queries:\n\nself.env.cr.execute(\n    \"SELECT id FROM res_partner WHERE name = %s\",\n    [partner_name]\n)",
  "confidence": 0.97,
  "is_odoo_specific": true
}
```

Why it's good:
- Specific file and line.
- Explains *what* is wrong and *why* it matters.
- Provides a concrete, copy-pasteable fix.
- High confidence is justified (clear vulnerability).
- Correct category and severity.

### Bad Finding

```json
{
  "severity": "major",
  "category": "maintainability",
  "file": null,
  "line_start": null,
  "line_end": null,
  "title": "Code could be improved",
  "body": "Some of the code in this PR could be refactored.",
  "suggestion": null,
  "confidence": 0.3
}
```

Why it's bad:
- No file or line reference.
- Vague title and body — not actionable.
- No suggestion.
- "Major" severity for a vague observation is wrong.
- Low confidence confirms it shouldn't have been reported.

### Good Info Finding (Positive Feedback)

```json
{
  "severity": "info",
  "category": "odoo",
  "file": "models/sale_order.py",
  "line_start": 15,
  "line_end": 28,
  "title": "Clean use of search_fetch() for optimized queries",
  "body": "Good use of the Odoo 19 search_fetch() method here instead of the older search() + browse() pattern. This reduces the number of database queries from 2 to 1.",
  "suggestion": null,
  "confidence": 0.90,
  "is_odoo_specific": true
}
```

Why it's good: Positive feedback reinforces good patterns and builds developer trust in ARIA.

---

## 8. Review Body Template

The PR review body follows this structure:

```markdown
## 🔍 ARIA Review

{summary}

### Findings

| # | Severity | Category | File | Title | Confidence |
|---|---|---|---|---|---|
| 1 | 🔴 Critical | security | controllers/partner_api.py:42 | SQL injection via string-formatted query | 0.97 |
| 2 | 🟠 Major | odoo | models/partner.py:15 | Missing ir.model.access for new model | 0.88 |
| 3 | 🟡 Minor | maintainability | utils/helpers.py:30 | Unused import | 0.92 |

**Risk Level**: {risk_level}

---

*ARIA {prompt_version} | {model} | {duration} | ${cost}*
*React with 👍 or 👎 on individual comments to help me improve.*
```

---

## 9. Check Run Blocking Matrix

| Findings present | Check Run conclusion | Blocks merge? |
|---|---|---|
| None | `success` | No |
| Info only | `success` | No |
| Minor only | `neutral` | No |
| Any major | `failure` | **Yes** |
| Any critical | `failure` | **Yes** |
| Review declined | `neutral` | No |
| Review failed (error) | `failure` | **Yes** (safety) |
| Review stale | `skipped` | No |

---

## 10. Feedback Quality Tracking

Developer reactions on ARIA's inline comments are tracked:

| Reaction | Mapped to | Meaning |
|---|---|---|
| 👍 (+1) | `positive` | Finding was helpful/accurate |
| 👎 (-1) | `negative` | Finding was unhelpful/wrong |
| 😕 (confused) | `negative` | Finding was confusing or unclear |
| ❤️ (heart) | `positive` | Strong agreement |
| 🎉 (hooray) | `positive` | Finding was very helpful |

The TUI metrics view shows:
- Overall approval rate (thumbs up / total reactions)
- Approval rate by category (which types of findings are most useful?)
- Approval rate by severity (are critical findings accurate?)
- Worst-performing finding types (candidates for prompt improvement)

---

## 11. What ARIA Should NOT Do

1. **Never auto-approve PRs.** ARIA provides analysis, not approval.
2. **Never execute code from the PR.** Only read diffs and files.
3. **Never suggest security workarounds.** If something needs fixing, say so directly.
4. **Never comment on author's skill level.** Focus on the code, not the person.
5. **Never post duplicate comments on the same SHA.** Idempotency is enforced.
6. **Never leak prompt content or system instructions in comments.** Claude's response is parsed as JSON — no raw output reaches GitHub.
7. **Never report on files not in the diff** (unless in deep review mode).
8. **Never flag personal coding style preferences** unless they violate documented project conventions.
