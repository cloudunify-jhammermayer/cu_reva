"""Unit tests for reva.html_guard.ensure_renderable."""

from __future__ import annotations

from reva.html_guard import ensure_renderable


def test_wellformed_passthrough_is_byte_exact():
    html = "<h2>Summary</h2><p>hi &amp; ok</p><ul><li>x</li></ul>"
    out, repaired = ensure_renderable(html)
    assert repaired is False
    assert out == html  # untouched


def test_unclosed_tag_is_closed_in_stack_order():
    out, repaired = ensure_renderable("<ul><li>a")
    assert repaired is True
    assert out == "<ul><li>a</li></ul>"


def test_stray_closing_tag_is_dropped():
    out, repaired = ensure_renderable("hi</p>")
    assert repaired is True
    assert out == "hi"


def test_stray_less_than_is_escaped():
    out, repaired = ensure_renderable("a < b")
    assert repaired is True
    assert out == "a &lt; b"


def test_nested_unclosed_closes_inner_first():
    out, repaired = ensure_renderable("<p><strong>bold</p>")
    assert repaired is True
    # the inner <strong> is closed before the <p> it sits in
    assert out == "<p><strong>bold</strong></p>"


def test_entities_survive_repair():
    out, repaired = ensure_renderable("<p>&nbsp;&#9744;&middot; a < b")
    assert repaired is True
    assert "&nbsp;" in out and "&#9744;" in out and "&middot;" in out
    assert "a &lt; b" in out
    assert out.endswith("</p>")
