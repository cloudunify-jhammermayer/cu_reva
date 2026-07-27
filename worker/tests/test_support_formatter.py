"""Tests for reva.support_formatter — pure formatting, no IO."""

from __future__ import annotations

from reva.html_guard import ensure_renderable
from reva.support_formatter import format_support_html
from reva.types import SupportAnswerResult, SupportHandoff, SupportSource


def _source(kind="repo_doc", ref="docs/billing.md#run", title="Billing run") -> SupportSource:
    return SupportSource(kind=kind, ref=ref, title=title)


def _answered(**overrides) -> SupportAnswerResult:
    data = dict(
        request_kind="question",
        answer_status="answered",
        answer="Yes, the billing run can be restarted from the wizard.",
        sources=[_source()],
        language="en",
        confidence="high",
    )
    data.update(overrides)
    return SupportAnswerResult(**data)


def _partial(**overrides) -> SupportAnswerResult:
    data = dict(
        request_kind="question",
        answer_status="partially_answered",
        answer="Partially, but it depends on the invoicing policy.",
        open_questions=["Which invoicing policy is configured on the journal?"],
        sources=[_source()],
        language="en",
        confidence="high",
    )
    data.update(overrides)
    return SupportAnswerResult(**data)


def _cannot(**overrides) -> SupportAnswerResult:
    data = dict(
        request_kind="question",
        answer_status="cannot_answer",
        answer="",
        cannot_answer_reason="No documentation or code covers this billing scenario.",
        open_questions=["Which module raises the error?"],
        language="en",
        confidence="high",
    )
    data.update(overrides)
    return SupportAnswerResult(**data)


# --- answered ----------------------------------------------------------------


def test_answered_renders_answer_and_all_sources():
    result = _answered(
        sources=[
            _source(kind="core_doc", ref="core/account.md#run", title="Account run"),
            _source(kind="repo_code", ref="custom_addons/billing/models/run.py", title="Run model"),
        ]
    )
    html = format_support_html(result)
    assert "<h2>Answer</h2>" in html
    assert "Yes, the billing run can be restarted from the wizard." in html
    assert "<h2>Sources</h2>" in html
    assert "Account run" in html
    assert "core/account.md#run" in html
    assert "core doc" in html
    assert "Run model" in html
    assert "custom_addons/billing/models/run.py" in html
    assert "repo code" in html
    # cannot_answer content must never appear on an answered draft
    assert "Cannot Answer" not in html


def test_answered_surfaces_request_kind_and_status_badge():
    html = format_support_html(_answered(request_kind="mixed"))
    assert "answered" in html
    assert "question + change request" in html


# --- partially_answered --------------------------------------------------


def test_partially_answered_renders_answer_and_open_questions():
    html = format_support_html(_partial())
    assert "<h2>Answer</h2>" in html
    assert "Partially, but it depends on the invoicing policy." in html
    assert "<h2>Open Questions</h2>" in html
    assert "Which invoicing policy is configured on the journal?" in html
    assert "<h2>Sources</h2>" in html


# --- cannot_answer -------------------------------------------------------


def test_cannot_answer_renders_reason_and_open_questions_no_answer():
    html = format_support_html(_cannot())
    assert "<h2>Cannot Answer</h2>" in html
    assert "No documentation or code covers this billing scenario." in html
    assert "<h2>Open Questions</h2>" in html
    assert "Which module raises the error?" in html
    # No answer prose section at all — the product rule under test.
    assert "<h2>Answer</h2>" not in html
    assert "<h2>Sources</h2>" not in html


# --- handoff ---------------------------------------------------------------


def test_handoff_hint_surfaced_when_suggested():
    result = _answered(
        request_kind="mixed",
        handoff=SupportHandoff(
            suggest_analysis=True,
            suggest_issues=False,
            rationale="Standard Odoo does not cover the custom discount rule.",
        ),
    )
    html = format_support_html(result)
    assert "Next step" in html
    assert "Analyse Ticket" in html
    assert "Create Issues" not in html
    assert "Standard Odoo does not cover the custom discount rule." in html


def test_no_handoff_hint_when_not_suggested():
    html = format_support_html(_answered())
    assert "Next step" not in html


# --- empty lists -------------------------------------------------------------


def test_answered_with_no_sources_has_no_dangling_section():
    html = format_support_html(_answered(sources=[]))
    assert "<h2>Sources</h2>" not in html
    assert "<ul></ul>" not in html


def test_partially_answered_with_no_open_questions_has_no_dangling_section():
    html = format_support_html(_partial(open_questions=[]))
    assert "<h2>Open Questions</h2>" not in html


def test_cannot_answer_with_no_open_questions_has_no_dangling_section():
    html = format_support_html(_cannot(open_questions=[]))
    assert "<h2>Open Questions</h2>" not in html


# --- escaping ----------------------------------------------------------------


def test_escapes_html_metacharacters_in_answer():
    result = _answered(answer='Tom & Jerry <script>alert(1)</script>')
    html = format_support_html(result)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "Tom &amp; Jerry" in html


def test_escapes_html_metacharacters_in_cannot_answer_reason():
    result = _cannot(cannot_answer_reason='R&D <script>alert(1)</script> needed')
    html = format_support_html(result)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "R&amp;D" in html


def test_escapes_html_metacharacters_in_open_questions():
    result = _partial(open_questions=['<script>alert(1)</script> & more?'])
    html = format_support_html(result)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&amp; more?" in html


def test_escapes_html_metacharacters_in_source_fields():
    result = _answered(
        sources=[SupportSource(kind="repo_doc", ref="a & b", title="<script>x</script>")]
    )
    html = format_support_html(result)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "a &amp; b" in html


# --- ensure_renderable idempotency --------------------------------------------


def test_answered_output_survives_ensure_renderable_unchanged():
    html = format_support_html(_answered())
    out, repaired = ensure_renderable(html)
    assert repaired is False
    assert out == html


def test_partially_answered_output_survives_ensure_renderable_unchanged():
    html = format_support_html(_partial())
    out, repaired = ensure_renderable(html)
    assert repaired is False
    assert out == html


def test_cannot_answer_output_survives_ensure_renderable_unchanged():
    html = format_support_html(_cannot())
    out, repaired = ensure_renderable(html)
    assert repaired is False
    assert out == html


def test_output_with_escaped_metacharacters_survives_ensure_renderable_unchanged():
    result = _answered(answer='Tom & Jerry <script>alert(1)</script>')
    html = format_support_html(result)
    out, repaired = ensure_renderable(html)
    assert repaired is False
    assert out == html


def test_answer_paragraphs_split_on_blank_lines_and_stay_escaped():
    """A multi-paragraph draft must render as multiple <p> blocks, not one run —
    while still being escaped, since the model's output is shaped by untrusted
    customer text and this HTML lands in a consultant's Odoo view."""
    result = SupportAnswerResult(
        request_kind="question",
        answer_status="answered",
        answer="First paragraph.\n\nSecond <script>alert(1)</script> paragraph.",
        language="en",
        confidence="high",
    )
    html = format_support_html(result)
    assert html.count("<p>First paragraph.</p>") == 1
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    # two authored paragraphs -> two blocks in the Answer section
    answer_section = html.split("<h2>Answer</h2>")[1]
    assert answer_section.count("<p>") >= 2


def test_answer_with_trailing_blank_lines_has_no_empty_paragraph():
    result = SupportAnswerResult(
        request_kind="question",
        answer_status="answered",
        answer="Only one.\n\n\n",
        language="en",
        confidence="high",
    )
    html = format_support_html(result)
    assert "<p></p>" not in html
