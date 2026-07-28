"""CoreKnowledge seam: validation, search fallback, overlap hints."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from reva.core_knowledge import CoreKnowledge, extract_added_definitions
from reva.db import Base, Database, create_engine_from_url
from reva.odoo_registry import load_version

FIXTURES = Path(__file__).parent / "fixtures" / "core"


@pytest.fixture()
def db() -> Database:
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


@pytest.fixture()
def core_dir(tmp_path, db) -> Path:
    vdir = tmp_path / "19.0"
    shutil.copytree(FIXTURES / "odoo", vdir / "odoo")
    shutil.copytree(FIXTURES / "documentation", vdir / "documentation")
    (vdir / "enterprise").mkdir()
    load_version(db, vdir, "19.0")
    return tmp_path


def _ck(db, core_dir, versions=("19.0",)) -> CoreKnowledge:
    return CoreKnowledge(db, str(core_dir), list(versions))


def test_validate_startup_ok(db, core_dir):
    _ck(db, core_dir).validate_startup()


def test_validate_startup_missing_version_dir(db, core_dir):
    with pytest.raises(RuntimeError, match="18.0"):
        _ck(db, core_dir, versions=("19.0", "18.0")).validate_startup()


def test_validate_startup_missing_registry(db, core_dir, tmp_path):
    vdir = tmp_path / "18.0"
    shutil.copytree(core_dir / "19.0", vdir)
    with pytest.raises(RuntimeError, match="registry"):
        _ck(db, core_dir, versions=("19.0", "18.0")).validate_startup()


def test_resolve(db, core_dir):
    ck = _ck(db, core_dir)
    assert ck.resolve("19.0") == "19.0"
    assert ck.resolve("18.0") is None
    assert ck.resolve(None) is None


def test_core_paths(db, core_dir):
    paths = _ck(db, core_dir).core_paths("19.0")
    assert any(p.endswith("19.0/odoo") for p in paths)
    assert any(p.endswith("19.0/documentation") for p in paths)
    assert len(paths) == 3


def test_search_docs_like_fallback(db, core_dir):
    # Membership, not ordering: the SQLite ilike fallback has no ts_rank, so
    # relevance ordering is a Postgres-only property (covered by the
    # REVA_TEST_POSTGRES_URL tier in test_pg_integration.py).
    hits = _ck(db, core_dir).search_docs("19.0", ["quotation", "template"])
    assert any(h["title"] == "Quotation templates" for h in hits)


def test_search_docs_matches_any_term(db, core_dir):
    # OR-of-terms, mirroring search_repo_docs: the planner sends up to 13
    # terms+modules, so demanding every one appear in a single section would
    # near-never match. "payroll"/"warehouse" are absent from the fixture.
    hits = _ck(db, core_dir).search_docs(
        "19.0", ["quotation", "template", "payroll", "warehouse"]
    )
    assert any(h["title"] == "Quotation templates" for h in hits)


def test_search_docs_empty_terms_returns_empty(db, core_dir):
    ck = _ck(db, core_dir)
    assert ck.search_docs("19.0", []) == []
    assert ck.search_docs("19.0", ["  ", ""]) == []


def test_search_registry(db, core_dir):
    hits = _ck(db, core_dir).search_registry("19.0", ["sales", "order"])
    names = {h["name"] for h in hits}
    assert "sale.order" in names or "sale_stub" in names


def test_search_registry_finds_core_fields(db, core_dir):
    """A stock FIELD is often the whole answer to "can Odoo already do this?",
    and this table was searched for modules and models only. Odoo 19's
    `sale.order.line.is_optional` was loaded and unreachable while a support
    answer told the customer the feature did not exist (ticket 6743,
    2026-07-28)."""
    hits = _ck(db, core_dir).search_registry("19.0", ["optional"])
    by_name = {h["name"]: h for h in hits}
    assert "sale.order.line.is_optional" in by_name
    hit = by_name["sale.order.line.is_optional"]
    assert hit["kind"] == "field"
    # The label is what makes the hit usable: type, UI string, owning module.
    assert "Boolean" in hit["summary"]
    assert "Optional Line" in hit["summary"]
    assert "sale_stub" in hit["summary"]


def test_search_registry_matches_a_field_by_its_ui_label(db, core_dir):
    """Consultants and customers speak in labels, not technical names, so the
    planner's terms are label-shaped."""
    hits = _ck(db, core_dir).search_registry("19.0", ["Optional Line"])
    assert any(h["name"] == "sale.order.line.is_optional" for h in hits)


def test_search_registry_does_not_let_one_kind_starve_another(db, core_dir):
    """Concatenating modules → models → fields under one `output[:limit]` meant
    a term matching enough modules pushed every field out of the block. Fields
    must survive a limit smaller than the number of module matches."""
    hits = _ck(db, core_dir).search_registry("19.0", ["sale", "optional"], limit=2)
    assert len(hits) == 2
    assert any(h["kind"] == "field" for h in hits)


def test_extract_added_definitions():
    diff = (
        "+++ b/custom_addons/x/models/approval.py\n"
        "+class Approval(models.Model):\n"
        '+    _name = "custom.approval"\n'
        "+    partner_id = fields.Many2one('res.partner')\n"
        "-    removed = fields.Char()\n"
        "+class SaleOrder(models.Model):\n"
        '+    _inherit = "sale.order"\n'
        "+    my_total = fields.Monetary()\n"
    )
    models, fields = extract_added_definitions(diff)
    assert models == ["custom.approval"]
    assert ("custom.approval", "partner_id") in fields
    assert ("sale.order", "my_total") in fields
    assert all(field != ("custom.approval", "removed") for field in fields)


def test_core_overlap_hints(db, core_dir):
    ck = _ck(db, core_dir)
    hints = ck.core_overlap(
        "19.0",
        added_models=["sale.order.approval"],
        added_fields=[("sale.order", "partner_id"), ("sale.order", "brand_new")],
    )
    joined = "\n".join(hints)
    assert "partner_id" in joined
    assert "brand_new" not in joined
    assert "sale.order" in joined
    assert len(hints) <= 10


def test_core_overlap_empty(db, core_dir):
    assert _ck(db, core_dir).core_overlap("19.0", [], []) == []
