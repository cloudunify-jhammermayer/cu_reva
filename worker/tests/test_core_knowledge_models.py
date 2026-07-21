"""Registry tables + version plumbing (core-knowledge spec §2, §5)."""

from __future__ import annotations

import pytest

from reva.db import Base, Database, create_engine_from_url, writers
from reva.db.models import (
    CoreKnowledgeVersion,
    OdooCoreField,
    OdooCoreModel,
    OdooCoreModule,
    OdooDocsSection,
)
from reva.types import RepoConfig


@pytest.fixture()
def db() -> Database:
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def test_registry_rows_roundtrip(db):
    with db.session() as s:
        s.add(OdooCoreModule(
            odoo_version="19.0",
            module="sale",
            source="odoo",
            category="Sales",
            summary="Quotations & orders",
            depends=["base", "account"],
        ))
        s.add(OdooCoreModel(
            odoo_version="19.0",
            model="sale.order",
            module="sale",
            kind="name",
            source_path="addons/sale/models/sale_order.py",
            description="Sales Order",
        ))
        s.add(OdooCoreField(
            odoo_version="19.0",
            model="sale.order",
            field="partner_id",
            ftype="Many2one",
            module="sale",
            string="Customer",
            compute=None,
            related=None,
        ))
        s.add(OdooDocsSection(
            odoo_version="19.0",
            path="applications/sales/sale.rst",
            anchor="quotations",
            title="Quotations",
            body="Create quotations ...",
        ))
        s.add(CoreKnowledgeVersion(
            odoo_version="19.0",
            modules=1,
            models=1,
            fields=1,
            sections=1,
        ))
    with db.session() as s:
        assert s.query(OdooCoreModule).one().depends == ["base", "account"]
        assert s.query(CoreKnowledgeVersion).one().loaded_at is not None


def test_instance_odoo_version_field(db):
    iid = writers.create_odoo_instance(
        db,
        name="acme",
        key_hash="h",
        key_prefix="reva_odoo_x",
        callback_url="",
        callback_api_key_enc="",
    )
    assert writers.get_odoo_instance(db, iid)["odoo_version"] is None
    assert writers.update_odoo_instance(db, iid, odoo_version="19.0")
    assert writers.get_odoo_instance(db, iid)["odoo_version"] == "19.0"


def test_instance_odoo_version_create(db):
    iid = writers.create_odoo_instance(
        db,
        name="acme",
        key_hash="h",
        key_prefix="reva_odoo_x",
        callback_url="",
        callback_api_key_enc="",
        odoo_version="18.0",
    )
    assert writers.get_odoo_instance(db, iid)["odoo_version"] == "18.0"


def test_repo_config_odoo_version():
    assert RepoConfig().odoo_version == "19.0"  # org baseline default
    assert RepoConfig(odoo_version="18.0").odoo_version == "18.0"
    assert RepoConfig(odoo_version=None).odoo_version is None  # explicit opt-out
    # Unquoted YAML numbers (float 17.0, int 19) are coerced to "<major>.0"
    # strings instead of silently collapsing to the default via a ValidationError.
    assert RepoConfig.model_validate({"odoo_version": 17.0}).odoo_version == "17.0"
    assert RepoConfig.model_validate({"odoo_version": 19}).odoo_version == "19.0"
