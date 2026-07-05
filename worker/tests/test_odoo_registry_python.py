"""AST-based extractor: models, fields, manifests (spec §2)."""

from __future__ import annotations

from pathlib import Path

from reva.odoo_registry import iter_addon_dirs, parse_module

FIXTURES = Path(__file__).parent / "fixtures" / "core" / "odoo" / "addons"


def test_iter_addon_dirs_finds_manifest_dirs():
    assert [d.name for d in iter_addon_dirs(FIXTURES)] == ["sale_stub"]


def test_manifest_parsed():
    info = parse_module(FIXTURES / "sale_stub", source="odoo")
    assert info.module == "sale_stub"
    assert info.source == "odoo"
    assert info.category == "Sales"
    assert info.summary == "Quotations, sales orders"
    assert info.depends == ["base", "account"]


def test_models_extracted_with_kind():
    info = parse_module(FIXTURES / "sale_stub", source="odoo")
    by = {(m.model, m.kind) for m in info.models}
    assert ("sale.order", "name") in by
    assert ("sale.order.line", "name") in by
    assert ("analytic.mixin", "inherit") in by
    assert ("res.partner", "inherit") in by
    order = next(m for m in info.models if m.model == "sale.order")
    assert order.description == "Sales Order"
    assert order.source_path.endswith("models/sale_order.py")


def test_fields_extracted():
    info = parse_module(FIXTURES / "sale_stub", source="odoo")
    fx = {(f.model, f.field): f for f in info.fields}
    assert fx[("sale.order", "partner_id")].ftype == "Many2one"
    assert fx[("sale.order", "partner_id")].string == "Customer"
    assert fx[("sale.order", "amount_total")].compute == "_compute_amounts"
    assert fx[("sale.order", "company_currency")].related == "company_id.currency_id"
    assert fx[("sale.order", "note")].ftype == "Text"
    assert ("res.partner", "sale_order_count") in fx


def test_syntax_error_file_skipped(tmp_path):
    bad = tmp_path / "broken"
    (bad / "models").mkdir(parents=True)
    (bad / "__manifest__.py").write_text('{"name": "Broken"}')
    (bad / "models" / "x.py").write_text("def broken(:\n")
    info = parse_module(bad, source="odoo")
    assert info.models == [] and info.fields == []
    assert info.parse_errors == 1
