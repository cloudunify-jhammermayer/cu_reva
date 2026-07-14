"""existing_customizations: schema + rendering + backward compat."""

from __future__ import annotations

from reva.ticket_formatter import format_ticket_html
from reva.ticket_tool import build_ticket_tool_schema
from reva.types import (
    CustomizationFeature,
    ExistingCustomizations,
    TicketAnalysisResult,
)


def _result(**kwargs) -> TicketAnalysisResult:
    return TicketAnalysisResult(
        summary="s",
        existing_customizations=ExistingCustomizations(**kwargs),
    )


def test_defaults_are_backward_compatible():
    # An old persisted blob lacks the key entirely — must still validate.
    result = TicketAnalysisResult.model_validate({"summary": "s"})
    assert result.existing_customizations.coverage == "unknown"
    assert result.existing_customizations.features == []


def test_tool_schema_includes_existing_customizations():
    schema = build_ticket_tool_schema()
    assert "existing_customizations" in schema["input_schema"]["properties"]
    assert "existing_customizations" in schema["input_schema"]["required"]
    # Strict structured output stays on.
    assert schema["strict"] is True
    assert schema["input_schema"]["additionalProperties"] is False


def test_features_accept_json_string_list():
    # Claude sometimes returns a list as a JSON string; the validator unwraps it.
    ec = ExistingCustomizations.model_validate({
        "coverage": "partial",
        "features": '[{"name": "Custom PDF", "addon": "cu_sale"}]',
    })
    assert len(ec.features) == 1
    assert ec.features[0].addon == "cu_sale"


def test_html_renders_section():
    result = _result(
        coverage="partial",
        features=[CustomizationFeature(
            name="Custom quotation layout",
            addon="cu_sale_reports",
            how="extends the existing quotation PDF layout",
            reference="custom_addons/cu_sale_reports/README.md#layout",
            confidence="high",
        )],
        notes="Extending the existing addon is cheaper than new work.",
    )
    html = format_ticket_html(result)
    assert "<h2>Existing Customizations</h2>" in html
    assert "partial" in html
    assert "Custom quotation layout" in html
    assert "cu_sale_reports" in html
    assert "custom_addons/cu_sale_reports/README.md#layout" in html


def test_html_omits_section_when_unknown_and_empty():
    html = format_ticket_html(_result())
    assert "Existing Customizations" not in html
