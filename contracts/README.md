# Odoo<->REVA contracts

Generated JSON artifacts live in this directory. Do not edit the JSON files by
hand.

Source of truth: `reva/odoo_contracts.py` (payload models used at runtime plus
the `CONTRACTS` table). Regenerate after any contract change:

```bash
python -m reva.odoo_contracts generate
```

The worker test suite fails when this directory is stale. Sync into the
ast-odoo repo with `scripts/sync_contracts.sh <ast-odoo-path>`; the manifest's
`contracts_version` is the freshness pin the addon tests assert against.
