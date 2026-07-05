"""standard_coverage: schema + rendering."""

from __future__ import annotations

from reva.ticket_formatter import format_ticket_html
from reva.ticket_tool import build_ticket_tool_schema
from reva.types import CoverageFeature, StandardCoverage, TicketAnalysisResult


def _result(**coverage_kwargs) -> TicketAnalysisResult:
    return TicketAnalysisResult(
        summary="s",
        standard_coverage=StandardCoverage(**coverage_kwargs),
    )


def test_defaults_are_backward_compatible():
    result = TicketAnalysisResult(summary="s")
    assert result.standard_coverage.coverage == "unknown"
    assert result.standard_coverage.features == []


def test_tool_schema_includes_standard_coverage():
    schema = build_ticket_tool_schema()
    assert "standard_coverage" in schema["input_schema"]["properties"]
    assert "standard_coverage" in schema["input_schema"]["required"]


def test_html_renders_coverage_section():
    result = _result(
        coverage="partial",
        features=[CoverageFeature(
            name="Quotation templates",
            module="sale_management",
            kind="feature",
            how="Enable under Sales > Configuration > Settings",
            reference="applications/sales/sale.rst#quotation-templates",
            confidence="high",
        )],
        notes="Custom layout still needs a small extension.",
    )
    html = format_ticket_html(result)
    assert "<h2>Standard Odoo Coverage</h2>" in html
    assert "partial" in html
    assert "Quotation templates" in html
    assert "sale_management" in html


def test_html_omits_section_when_unknown_and_empty():
    html = format_ticket_html(_result())
    assert "Standard Odoo Coverage" not in html
