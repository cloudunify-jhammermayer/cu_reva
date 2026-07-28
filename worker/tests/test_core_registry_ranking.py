"""Relevance ranking for CoreKnowledge.search_registry.

`search_docs` ranks with `ts_rank`; the registry had no ordering at all — just
`.limit()` over whatever order the DB returned. Making fields searchable
(ticket 6743) was therefore necessary but not sufficient: probing prod with the
planner-shaped terms `["kit", "optional", "bill of materials"]` filled all eight
slots with `l10n_in` leave-holiday fields and `mail.alias.mixin.optional`, while
`sale.order.line.is_optional` — the row that answers the question — never
appeared. The rows below are the real prod rows from that probe.
"""

from __future__ import annotations

import pytest

from reva.core_knowledge import CoreKnowledge
from reva.db import Base, Database, create_engine_from_url
from reva.db.models import (
    CoreKnowledgeVersion,
    OdooCoreField,
    OdooCoreModel,
    OdooCoreModule,
)

_V = "19.0"


def _field(model, field, ftype, module, string=None):
    return OdooCoreField(odoo_version=_V, model=model, field=field, ftype=ftype,
                         module=module, string=string)


@pytest.fixture()
def core() -> CoreKnowledge:
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Database(engine)
    with db.session() as s:
        s.add(CoreKnowledgeVersion(odoo_version=_V))
        s.add_all([
            # The answer to the question.
            _field("sale.order.line", "is_optional", "Boolean", "sale_management",
                   "Optional Line"),
            _field("sale.order.template.line", "is_optional", "Boolean",
                   "sale_management", "Optional Line"),
            # Relevant but less canonical.
            _field("product.template", "optional_product_ids", "Many2many", "sale",
                   "Optional Products"),
            _field("product.template", "pos_optional_product_ids", "Many2many",
                   "point_of_sale", "POS Optional Products"),
            # The noise that crowded the real answer out on prod. No UI label,
            # and a localization module nobody asking about BOMs cares about.
            _field("hr.leave.type", "l10n_in_is_limited_to_optional_days", "Boolean",
                   "l10n_in_hr_holidays"),
            _field("product.product", "is_kits", "Boolean", "mrp"),
        ])
        s.add_all([
            OdooCoreModel(odoo_version=_V, model="mail.alias.mixin.optional",
                          module="mail", kind="name", source_path="m.py",
                          description="Email Aliases Mixin (light)"),
            OdooCoreModel(odoo_version=_V, model="mail.test.alias.optional",
                          module="test_mail", kind="name", source_path="t.py",
                          description="Chatter Model using Optional Alias Mixin"),
            OdooCoreModel(odoo_version=_V, model="sale.order.line", module="sale",
                          kind="name", source_path="s.py",
                          description="Sales Order Line"),
        ])
        s.add_all([
            OdooCoreModule(odoo_version=_V, module="website_sale_mrp", source="odoo",
                           category="Website", summary="Manage Kit product inventory"),
            OdooCoreModule(odoo_version=_V, module="sale_management", source="odoo",
                           category="Sales", summary="Sales, quotations, orders"),
        ])
    return CoreKnowledge(db, "/nonexistent", [_V])


def _names(hits):
    return [h["name"] for h in hits]


def test_the_planner_shaped_terms_now_surface_the_answer(core):
    """The exact term set that failed on prod."""
    hits = core.search_registry(_V, ["kit", "optional", "bill of materials"])
    assert "sale.order.line.is_optional" in _names(hits)


def test_a_labelled_canonical_field_outranks_localization_noise(core):
    hits = core.search_registry(_V, ["optional"])
    names = _names(hits)
    assert names[0] == "sale.order.line.is_optional"
    assert names.index("sale.order.line.is_optional") < names.index(
        "hr.leave.type.l10n_in_is_limited_to_optional_days"
    )


def test_localization_and_test_modules_rank_last(core):
    """`l10n_*` and `test_*` are real core modules, so they stay searchable — a
    question about Indian holidays must still find them — but they must never
    displace a mainline hit."""
    hits = core.search_registry(_V, ["optional"], limit=3)
    modules = [h["module"] for h in hits]
    assert "l10n_in_hr_holidays" not in modules
    assert "test_mail" not in modules


def test_a_ui_label_match_beats_a_name_only_match(core):
    """Consultants search in labels. A field whose *label* matches is a better
    hit than one where the term only appears buried in the technical name."""
    hits = core.search_registry(_V, ["optional"])
    labelled = _names(hits).index("sale.order.line.is_optional")
    name_only = _names(hits).index("mail.alias.mixin.optional")
    assert labelled < name_only


def test_ranking_does_not_break_the_round_robin(core):
    """Each kind must still get representation — ranking orders within a kind,
    it does not let one kind monopolise the limit."""
    hits = core.search_registry(_V, ["optional", "sale", "kit"], limit=6)
    assert {h["kind"] for h in hits} == {"field", "model", "module"}


def test_an_unmatched_term_set_returns_nothing(core):
    assert core.search_registry(_V, ["payroll", "warehouse"]) == []
