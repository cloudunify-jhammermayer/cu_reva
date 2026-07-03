## Task: Odoo migration-script review

This PR touches one or more Odoo **upgrade scripts**
(`migrations/<version>/{pre,post,end}-migrate.py`) — the highest-blast-radius
code an Odoo team writes: it runs raw SQL and ORM operations against live
production data during an upgrade, with no easy undo. Review it accordingly. The
severity, category, confidence, and conduct rules in the guidance above apply.

**Review the entire diff, not only the migration files.** A migration PR usually
also changes models/views; apply the normal review to those too — do not drop
non-migration findings.

## Migration-specific criteria

For each changed `pre`/`post`/`end-migrate.py`, Read the script, the target model
definition(s) it migrates, and sibling migrations in the same `<version>` dir, then check:

1. **Destructive DDL without a guard** — `DROP COLUMN`/`DROP TABLE`,
   `DELETE`/`TRUNCATE`, or `ALTER ... SET NOT NULL` on a populated table with no
   existence check, backup, or documented rollback. **Critical** if it can lose data.
2. **Non-idempotent backfill** — `pre`/`post`-migrate can re-run (a failed upgrade
   is retried), so an `UPDATE`/`INSERT` with no re-run guard (a `WHERE` that excludes
   already-migrated rows, or `ON CONFLICT`) can double-apply. **Major**.
3. **Wrong staging layer** — raw SQL where the ORM is required (e.g. computed/
   stored fields, translations), or ORM use in `pre-migrate` before the registry is
   loadable. **Major**.
4. **Translation / JSONB columns** — Odoo 16+ stores translations in `jsonb`;
   migrating a translated field with a plain column copy (not `jsonb_set` / proper
   value-column handling) corrupts translations. **Major**.
5. **SQL injection** — `cr.execute` built with string formatting / f-strings /
   concatenation instead of `%s` parameters. **Critical**. (Use this exact language
   so the issue is recognised consistently.)
6. **No rollback / safety note** — a risky data transform with no comment on how to
   recover if the upgrade aborts midway. **Minor**.

Set `is_odoo_specific: true` and use `category: odoo` (or `security` for SQL
injection, `bug` for a logic error). Keep titles ≤ 80 chars.

## Manifest checks

If a `manifest_audit` parameter is present, surface its **deterministic** findings
(missing data files, security-before-views order, version format) at the suggested
severity — trust them, don't re-derive.

## Review process

1. Read the diff in the Task Parameters section.
2. Read each changed migration script in full, plus the target model(s) and sibling
   migrations in the same version directory, then apply the criteria above.
3. Use Read to examine any other changed file (models/views) and review it normally.
4. Use Grep to trace callers/usages when a change is non-trivial.
5. Verify each candidate finding per the guidance ("Verify before you write"), then keep only what survives, scored honestly.
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
      "category": "odoo",
      "file": "custom_addons/module/migrations/18.0.1.0/pre-migrate.py",
      "line_start": 12,
      "line_end": 18,
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
