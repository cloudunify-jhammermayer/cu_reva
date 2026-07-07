# Odoo 19 — Additional Review Rules

This repository uses Odoo 19. Apply the following review criteria **in
addition** to your normal review. For findings that are specifically Odoo
issues, set `is_odoo_specific: true`. Use the `odoo` category when the issue
is Odoo-specific and doesn't fit a more specific category (e.g. a `sudo()`
security issue uses `category: security` with `is_odoo_specific: true`).

## ORM and Models

- Flag direct `cr.execute()` with string formatting — SQL injection risk. Use `%s` placeholders or the ORM. **Critical** when user input is involved.
- Flag `cr.commit()` and `cr.rollback()` — these should almost never be called manually; the framework handles transactions. **Major**.
- Check that `@api.depends` decorators list **all** fields the compute method reads. **Major** if a dependency is missing — stale computed values are a real bug.
- Check that `@api.onchange` methods do not write to the database (they operate on a virtual record). **Major** if they do.
- Verify `sudo()` usage is justified. It bypasses access rights and is a frequent source of vulnerabilities. **Major or critical** in controllers without input validation.
- Flag `with_context()` misuse — keys should be documented and intentional. **Minor**.
- Verify field naming: Many2one ends with `_id`; Many2many/One2many end with `_ids` (Odoo 19 convention). **Minor**.
- Flag deprecated `name_get()` — use the `display_name` field instead. **Minor**.
- Flag deprecated `odoo.osv` namespace usage. **Minor**.
- Flag legacy `_sql_constraints` lists — Odoo 19 **silently ignores** them (only `models.Constraint` class attributes create constraints), so an old-style list means **no DB constraint at all**. **Major** — this is a missing data-integrity guarantee, not a style nit.
- Flag writes to a related field whose intermediate many2one can be empty — `_inverse_related` **silently drops** the write when the chain is broken; the intermediate record must exist before the inverse runs, or the user's input vanishes without an error. **Major**.
- Flag status/state writes performed in the same transaction directly before a `raise` (e.g. `UserError`) — the write **rolls back with the transaction**, so the persisted "error state" is unreachable. Failure states must be recorded from a separate transaction (cron, callback) or not promised in the UI. **Major**.
- Flag the `inselect` operator — removed in Odoo 19; use `in` with a `Query`/`SQL` object. **Minor**.
- Flag `group_operator` attribute — renamed to `aggregator` in Odoo 19. **Minor**.
- Flag `_flush_search()` calls — deprecated; flushing is now handled by `execute_query()`. **Minor**.
- Flag `search_count()` without limit awareness (it now respects the limit argument in Odoo 19). **Minor**.
- Prefer `_search_display_name` for custom name search instead of overriding `name_search`. **Info**.

## Security

- Verify `ir.model.access.csv` records exist for new models. **Major** if missing.
- Verify `ir.rule` records exist for multi-company or multi-user data isolation. **Major** if missing on company-scoped models.
- Flag controllers using `auth='none'` — should be `auth='user'` or `auth='public'` with explicit checks. **Major**.
- Flag `sudo()` in controllers without input validation. **Critical** in many cases.
- Record rules now support native OR logic — flag complex workarounds that can be simplified. **Minor**.
- Prefer `check_access`, `has_access`, and `_filtered_access` (new in Odoo 19) over manual access checks where appropriate. **Info**.
- Verify inbound/async callback endpoints guard against late, replayed, or cross-record callbacks: check terminal states (done/cancelled) before mutating, compare a stored request/job id for staleness, and scope record lookups to the authenticated caller. **Major** — missing guards let stale callbacks mutate finished records.

## Views and Templates

- Flag `t-esc` in QWeb templates — deprecated in favor of `t-out`. **Minor**.
- Flag inline `<script>` or external CDN loading — blocked by CSP in Odoo 18+. **Major** if it breaks the page.
- Verify view inheritance uses explicit `inherit_id` references (required in Odoo 19). **Major** — implicit/missing inheritance is a runtime error.
- Check `<card>` element usage in Kanban views (new Odoo 19 standard). **Minor**.

## Performance

- Flag N+1 patterns: iterating over records and reading relational fields inside a loop without prefetching. **Major** at production data volumes.
- Flag `search()` + `browse()` anti-pattern — use `search_fetch()` (optimized in Odoo 19). **Minor**.
- Flag `read_group` usage — check that the new `_read_group()` signature is used correctly. **Major** if the signature is wrong.
- Flag `mapped()` on large recordsets without considering performance. **Minor**.
- Note where `GROUPING SETS` and the `any!` operator (new Odoo 19 ORM features) could simplify queries. **Info**.

## Module Structure

- Verify `__manifest__.py` `depends` list is complete and correct (every used addon declared). **Major** if a runtime dependency is missing.
- Verify every `data`/`demo` file listed in `__manifest__.py` exists in the module. **Major** if a listed file is missing.
- Verify file naming follows Odoo conventions: `models/`, `views/`, `wizard/`, `report/`, `security/`, `data/`. **Minor**.
- Flag static assets loaded from external URLs — copy them into the module instead. **Minor**.
- Check that data files in `__manifest__.py` are in the correct order (security before views). **Minor**.
- Verify `version` follows Odoo's 5-part `SERIES.x.y.z` form (e.g. `19.0.1.0.0`). **Minor** otherwise.

## Data and Migrations

- Verify migration scripts (`pre-migrate.py`, `post-migrate.py`) handle existing data safely. **Major** if they don't.
- Flag a dropped or renamed model column without a `migrations/<version>/` script. **Major**.
- Flag `ir.sequence` data records without `<field name="company_id" eval="False"/>` when the sequence is meant to be global — the record binds to the installing company and `next_by_code` **silently returns nothing** in every other company. **Major** on multi-company databases.
- Flag boolean `config_parameter` settings that are seeded via data files or given a default — a stored `'False'` reads back truthy (`bool('False') is True`), so the checkbox becomes stuck-on and kill-switches silently re-enable. A **missing parameter is the correct "off"**. **Major**.
- Flag destructive operations (column drops, data deletions) without a backup or migration strategy. **Major or critical**.
- Check that the JSONB translations format is used (required since Odoo 16+). **Major** — a plain-column copy of a translated field corrupts translations.
- Verify XML data records use `noupdate="1"` where appropriate. **Minor**.

## Python Version

- Odoo 19 requires Python 3.11 minimum, recommends Python 3.12.
- Flag `distutils` imports — removed in Python 3.12. **Minor**.
- Flag patterns that don't work with Python 3.12 type system improvements. **Info**.
