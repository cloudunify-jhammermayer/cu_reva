"""Boundary repair for the escalated-CLI output drift.

Every payload here is a real one from prod. Between 2026-07-27 and 07-28 all
five code-grounded ticket analyses died in validation and delivered nothing —
$8.44 of CLI time discarded, none of it even recorded as spend:

    77 / 4591  $1.71   question, high|medium
    78 / 4595  $2.10   question, "hoch — <rationale>", existing_customizations
                       as a bare string
    81 / 6694  $1.18   question
    82 / 6759  $1.90   question, medium|low
    84 / 6759  $1.55   question, "high – <rationale>", medium

The CLI path has no tool schema to constrain it — the skill file is the whole
contract — so prompts v2.15 states the field names and enums, and these
validators recover the run when the model ignores them anyway. Same stance as
`_unwrap_json_list`: repair the shape, never invent content.
"""

from __future__ import annotations

import pytest

from reva.types import ExistingCustomizations, MissingInfoItem, TicketAnalysisResult


# --- missing_info: `question` is the field name the model reaches for ---------


def test_question_is_accepted_as_text():
    item = MissingInfoItem.model_validate(
        {"question": "Wie entsteht der Einkaufsprozess?", "confidence": "certain"}
    )
    assert item.text == "Wie entsteht der Einkaufsprozess?"
    assert item.confidence == "certain"


def test_text_still_wins_when_both_are_present():
    item = MissingInfoItem.model_validate(
        {"text": "canonical", "question": "drifted"}
    )
    assert item.text == "canonical"


def test_a_genuinely_empty_item_is_still_rejected():
    """Repair must not manufacture a question out of nothing."""
    with pytest.raises(ValueError):
        MissingInfoItem.model_validate({"confidence": "certain"})


# --- missing_info: the estimates enum, in two languages, plus a rationale -----


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("high", "certain"),
        ("medium", "likely"),
        ("low", "possible"),
        ("hoch", "certain"),
        ("mittel", "likely"),
        ("niedrig", "possible"),
        # Analyses 78 and 84: the rationale was appended to the enum. Both an
        # en-dash and an em-dash appeared, and the case varied.
        ("high – im Code nicht ableitbar", "certain"),
        ("hoch — im Projekt eindeutig bestätigt, keine Vermutung", "certain"),
        ("mittel — abgeleitet aus dem Kontext", "likely"),
        ("medium – im Ticket nicht spezifiziert", "likely"),
        ("Medium", "likely"),
        # The canonical values must survive untouched.
        ("certain", "certain"),
        ("likely", "likely"),
        ("possible", "possible"),
    ],
)
def test_confidence_synonyms_are_mapped(raw, expected):
    assert MissingInfoItem.model_validate({"text": "t", "confidence": raw}).confidence == expected


def test_an_unmappable_confidence_falls_back_to_the_default():
    """A value we cannot interpret must not cost the whole analysis. `likely` is
    the field's own default, so this is the same outcome as omitting it."""
    item = MissingInfoItem.model_validate({"text": "t", "confidence": "banana"})
    assert item.confidence == "likely"


def test_confidence_may_still_be_omitted():
    assert MissingInfoItem.model_validate({"text": "t"}).confidence == "likely"


# --- existing_customizations: analysis 78 returned prose ---------------------


def test_a_bare_string_becomes_notes():
    ec = ExistingCustomizations.model_validate(
        "Im Projekt ist bereits eine Lösung vorhanden, die als Auslöser dienen soll."
    )
    assert ec.coverage == "unknown"          # prose is not an assessment
    assert ec.features == []
    assert "Auslöser" in ec.notes


def test_an_empty_string_stays_empty_rather_than_becoming_notes():
    ec = ExistingCustomizations.model_validate("   ")
    assert ec.coverage == "unknown"
    assert ec.notes == ""


# --- the whole payload, end to end -------------------------------------------


def test_analysis_84_now_validates():
    """The exact shape that failed with 8 validation errors on 2026-07-28."""
    result = TicketAnalysisResult.model_validate({
        "summary": "Der Einkaufsprozess soll automatisiert werden.",
        "missing_info": [
            {"question": "Wie entsteht der Bedarf?",
             "confidence": "high – im Code nicht ableitbar"},
            {"question": "Wenn eine Bestellung entsteht?",
             "confidence": "high – Geschäftsentscheidung, nicht aus Code ableitbar"},
            {"question": "Ist ein Produkt gemeint?",
             "confidence": "medium – im Ticket nicht spezifiziert"},
            {"question": "Bezieht sich das auf Einkauf?", "confidence": "medium"},
        ],
    })
    assert [item.text for item in result.missing_info] == [
        "Wie entsteht der Bedarf?",
        "Wenn eine Bestellung entsteht?",
        "Ist ein Produkt gemeint?",
        "Bezieht sich das auf Einkauf?",
    ]
    assert [item.confidence for item in result.missing_info] == [
        "certain", "certain", "likely", "likely",
    ]


def test_analysis_78_now_validates():
    """9 validation errors: German enums with rationales, plus
    existing_customizations as prose."""
    result = TicketAnalysisResult.model_validate({
        "summary": "Abwesenheitsregelung.",
        "missing_info": [
            {"question": "Die im Ticket genannte Regel?",
             "confidence": "hoch — im Projekt eindeutig bestätigt, keine Vermutung"},
            {"question": "Die beschriebene Ableitung?",
             "confidence": "mittel — abgeleitet aus dem Kontext"},
        ],
        "existing_customizations":
            "Im Projekt ist bereits eine Lösung vorhanden, die als Auslöser dienen soll.",
    })
    assert [item.confidence for item in result.missing_info] == ["certain", "likely"]
    assert "Auslöser" in result.existing_customizations.notes


def test_repair_does_not_weaken_the_degenerate_content_guard():
    """The two mechanisms are independent: repair fixes drifted keys and enums,
    the guard still refuses a field carrying the model's own tool-call syntax."""
    with pytest.raises(ValueError, match="tool-call syntax"):
        TicketAnalysisResult.model_validate({
            "summary": '<parameter name="summary">text</parameter>',
        })
