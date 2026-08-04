"""The ticket tool schema must not invite the model to fill derived fields."""

from reva.ticket_tool import build_ticket_tool_schema


def _story_estimate_def(schema):
    return schema["input_schema"]["$defs"]["StoryEstimate"]["properties"]


def test_schema_offers_anchor_ref_and_drivers():
    props = _story_estimate_def(build_ticket_tool_schema())

    assert "anchor_ref" in props
    assert "complexity_drivers" in props


def test_schema_hides_the_code_derived_confidence():
    props = _story_estimate_def(build_ticket_tool_schema())

    assert "anchor_confidence" not in props


def test_schema_does_not_require_anchor_confidence():
    story = build_ticket_tool_schema()["input_schema"]["$defs"]["StoryEstimate"]

    assert "anchor_confidence" not in story.get("required", [])
