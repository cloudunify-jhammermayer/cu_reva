"""RST heading splitter for odoo/documentation (spec §2)."""

from __future__ import annotations

from pathlib import Path

from reva.odoo_registry import iter_rst_files, split_rst_sections

DOCS = Path(__file__).parent / "fixtures" / "core" / "documentation"


def test_iter_finds_rst():
    files = list(iter_rst_files(DOCS))
    assert len(files) == 1 and files[0].name == "sale.rst"


def test_sections_split_on_headings():
    sections = split_rst_sections(next(iter_rst_files(DOCS)), DOCS)
    titles = [s.title for s in sections]
    assert titles == ["Sales Orders", "Quotation templates", "Online signature"]
    assert all(s.path == "content/applications/sales/sale.rst" for s in sections)
    quot = sections[1]
    assert "Templates pre-fill" in quot.body
    assert "Online signature" not in quot.body
    assert quot.anchor == "quotation-templates"


def test_body_capped():
    sections = split_rst_sections(next(iter_rst_files(DOCS)), DOCS)
    assert all(len(s.body) <= 2000 for s in sections)
