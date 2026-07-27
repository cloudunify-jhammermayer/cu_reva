"""Schema + validation tests for the submit_support_answer tool contract."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from reva.support_tool import (
    SUPPORT_TOOL_NAME,
    build_support_tool_schema,
    support_tool_choice,
)
from reva.types import SupportAnswerResult


def _answer(**overrides):
    payload = {
        "request_kind": "question",
        "answer_status": "answered",
        "answer": "<p>Answer</p>",
        "cannot_answer_reason": None,
        "open_questions": [],
        "sources": [],
        "handoff": {},
        "language": "en",
        "confidence": "high",
    }
    payload.update(overrides)
    return SupportAnswerResult.model_validate(payload)


def test_tool_schema_shape():
    schema = build_support_tool_schema()
    assert schema["name"] == SUPPORT_TOOL_NAME == "submit_support_answer"
    assert schema["strict"] is True
    inp = schema["input_schema"]
    assert inp["additionalProperties"] is False
    assert set(inp["required"]) == set(SupportAnswerResult.model_fields.keys())
    assert set(inp["properties"].keys()) == set(SupportAnswerResult.model_fields.keys())


def test_tool_choice_forces_tool():
    assert support_tool_choice() == {"type": "tool", "name": SUPPORT_TOOL_NAME}


def test_answered_with_html_validates():
    result = _answer(answer_status="answered", answer="<p>It's in Settings.</p>")
    assert result.answer == "<p>It's in Settings.</p>"
    assert result.cannot_answer_reason is None


def test_cannot_answer_with_reason_and_empty_html_validates():
    result = _answer(
        answer_status="cannot_answer",
        answer="",
        cannot_answer_reason="No pricing rule found for this partner category.",
    )
    assert result.answer_status == "cannot_answer"
    assert result.answer == ""
    assert result.cannot_answer_reason


def test_cannot_answer_with_nonempty_html_is_rejected():
    with pytest.raises(ValidationError):
        _answer(
            answer_status="cannot_answer",
            answer="<p>Here is a guess...</p>",
            cannot_answer_reason="Not enough information.",
        )


def test_cannot_answer_with_no_reason_is_rejected():
    with pytest.raises(ValidationError):
        _answer(answer_status="cannot_answer", answer="", cannot_answer_reason=None)
    with pytest.raises(ValidationError):
        _answer(answer_status="cannot_answer", answer="", cannot_answer_reason="   ")


def test_answer_is_truncated_not_rejected():
    result = _answer(answer="x" * 25000)
    assert len(result.answer) == 20000
    assert result.answer.endswith("...")


def test_cannot_answer_reason_is_truncated_not_rejected():
    result = _answer(
        answer_status="cannot_answer",
        answer="",
        cannot_answer_reason="z" * 3000,
    )
    assert len(result.cannot_answer_reason) == 2000
    assert result.cannot_answer_reason.endswith("...")


def test_open_question_items_are_truncated_not_rejected():
    result = _answer(
        answer_status="partially_answered",
        open_questions=["y" * 600],
    )
    assert len(result.open_questions[0]) == 500
    assert result.open_questions[0].endswith("...")


def test_source_ref_and_title_are_truncated_not_rejected():
    result = _answer(
        sources=[{"kind": "repo_code", "ref": "a" * 400, "title": "b" * 300}]
    )
    assert len(result.sources[0].ref) == 300
    assert len(result.sources[0].title) == 200


def test_handoff_rationale_is_truncated_not_rejected():
    result = _answer(handoff={"rationale": "r" * 1500})
    assert len(result.handoff.rationale) == 1000


def test_rejects_unknown_answer_status():
    with pytest.raises(ValidationError):
        _answer(answer_status="ignored")


def test_rejects_unknown_language():
    with pytest.raises(ValidationError):
        _answer(language="fr")


def test_schema_carries_no_keywords_the_messages_api_rejects():
    """A strict tool schema must not carry numeric range keywords.

    The API answers `tools.0.custom: For 'number' type, properties maximum,
    minimum are not supported` and the whole call 400s. This escaped every
    existing test because the answerer tests use a MockTransport that accepts
    any schema — they proved the shape we intended, not the shape the API
    takes. Walk the real generated schema instead.
    """
    banned = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
              "multipleOf", "minLength", "maxLength", "pattern",
              "minItems", "maxItems"}

    def walk(node, path="input_schema"):
        if isinstance(node, dict):
            for key, value in node.items():
                assert key not in banned, f"{path}.{key} is rejected by the Messages API"
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{path}[{i}]")

    walk(build_support_tool_schema()["input_schema"])


def test_confidence_is_the_same_enum_as_every_other_reva_output():
    """It was a float; the model answered "medium" against a `type: number`
    field even under strict:true. Strict does not reliably coerce numeric
    types, so this matches MissingInfoItem/CoverageFeature/StoryEstimate —
    the shape the model already emits."""
    import pytest as _pytest
    from pydantic import ValidationError

    assert _answered(confidence="medium").confidence == "medium"
    assert _answered(confidence="high").confidence == "high"
    with _pytest.raises(ValidationError):
        _answered(confidence=0.9)


def test_confidence_is_a_string_enum_in_the_tool_schema():
    prop = build_support_tool_schema()["input_schema"]["properties"]["confidence"]
    assert prop.get("type") != "number"
    assert set(prop.get("enum", [])) == {"high", "medium", "low"}


def _answered(**over):
    from reva.types import SupportAnswerResult

    payload = dict(
        request_kind="question", answer_status="answered",
        answer="Enable it under Settings.", language="en", confidence="high",
    )
    payload.update(over)
    return SupportAnswerResult(**payload)
