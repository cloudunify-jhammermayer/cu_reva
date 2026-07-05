"""End-to-end load of a fixture version dir (spec §2)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from reva.db import Base, Database, create_engine_from_url
from reva.db.models import (
    CoreKnowledgeVersion,
    OdooCoreModel,
    OdooCoreModule,
    OdooDocsSection,
)
from reva.odoo_registry import load_version

FIXTURES = Path(__file__).parent / "fixtures" / "core"


@pytest.fixture()
def db() -> Database:
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


@pytest.fixture()
def version_dir(tmp_path) -> Path:
    vdir = tmp_path / "19.0"
    shutil.copytree(FIXTURES / "odoo", vdir / "odoo")
    shutil.copytree(FIXTURES / "documentation", vdir / "documentation")
    (vdir / "enterprise").mkdir()
    return vdir


def test_load_populates_all_tables(db, version_dir):
    counts = load_version(db, version_dir, "19.0")
    assert counts["modules"] == 1
    assert counts["models"] >= 4
    assert counts["fields"] >= 5
    assert counts["sections"] == 3

    with db.session() as s:
        assert s.query(OdooCoreModule).filter_by(odoo_version="19.0").count() == 1
        assert s.query(OdooDocsSection).count() == 3
        bookkeeping = s.query(CoreKnowledgeVersion).one()
        assert bookkeeping.odoo_version == "19.0"
        assert bookkeeping.sections == 3


def test_load_is_idempotent_replace(db, version_dir):
    load_version(db, version_dir, "19.0")
    load_version(db, version_dir, "19.0")
    with db.session() as s:
        assert s.query(OdooCoreModule).count() == 1
        assert s.query(OdooCoreModel).count() >= 4
        assert s.query(CoreKnowledgeVersion).count() == 1


def test_catalog_written(db, version_dir):
    load_version(db, version_dir, "19.0")
    catalog = version_dir / "catalog" / "sale_stub.md"
    text = catalog.read_text()
    assert "sale.order" in text
    assert "partner_id" in text
    assert "depends: base, account" in text


def test_missing_enterprise_tolerated(db, version_dir):
    shutil.rmtree(version_dir / "enterprise")
    counts = load_version(db, version_dir, "19.0")
    assert counts["modules"] == 1
