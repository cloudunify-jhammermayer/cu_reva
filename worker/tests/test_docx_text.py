"""Tests for reva.docx_text — consultant DOCX extraction (Contract 1)."""

from __future__ import annotations

import base64
import io
import zipfile

import pytest

from reva.docx_text import decode_docx_base64, extract_docx_text
from reva.errors import PermanentError

_W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _docx_b64(document_xml: str) -> str:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("word/document.xml", document_xml)
    return base64.b64encode(buf.getvalue()).decode()


def _doc(body: str) -> str:
    return f'<?xml version="1.0"?><w:document {_W_NS}><w:body>{body}</w:body></w:document>'


def test_extracts_paragraphs_and_tables_in_order():
    content = _docx_b64(_doc(
        "<w:p><w:r><w:t>Requirement one: </w:t></w:r><w:r><w:t>login form</w:t></w:r></w:p>"
        "<w:p><w:r><w:t/></w:r></w:p>"  # empty paragraph dropped
        "<w:tbl><w:tr>"
        "<w:tc><w:p><w:r><w:t>Field</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>Mandatory</w:t></w:r></w:p></w:tc>"
        "</w:tr><w:tr>"
        "<w:tc><w:p><w:r><w:t>Email</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>yes</w:t></w:r></w:p></w:tc>"
        "</w:tr></w:tbl>"
        "<w:p><w:r><w:t>Closing note</w:t></w:r></w:p>"
    ))

    assert extract_docx_text(content) == (
        "Requirement one: login form\n"
        "Field | Mandatory\n"
        "Email | yes\n"
        "Closing note"
    )


def test_invalid_base64_is_permanent():
    with pytest.raises(PermanentError, match="base64"):
        extract_docx_text("not base64!!")


def test_non_zip_payload_is_permanent():
    content = base64.b64encode(b"plain text, not a zip").decode()
    with pytest.raises(PermanentError, match="zip"):
        extract_docx_text(content)


def test_zip_without_document_xml_is_permanent():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("other.txt", "hi")
    with pytest.raises(PermanentError, match="invalid description_docx"):
        extract_docx_text(base64.b64encode(buf.getvalue()).decode())


def test_document_without_text_is_permanent():
    content = _docx_b64(_doc("<w:p><w:r><w:t> </w:t></w:r></w:p>"))
    with pytest.raises(PermanentError, match="no extractable text"):
        extract_docx_text(content)


def test_entity_expansion_attack_is_rejected_not_expanded():
    """defusedxml: a billion-laughs style payload must fail as invalid input,
    never expand."""
    evil = (
        '<?xml version="1.0"?><!DOCTYPE w:document [<!ENTITY a "aaaa">'
        '<!ENTITY b "&a;&a;&a;&a;">]>'
        f'<w:document {_W_NS}><w:body><w:p><w:r><w:t>&b;</w:t></w:r></w:p>'
        "</w:body></w:document>"
    )
    with pytest.raises(PermanentError, match="invalid description_docx"):
        extract_docx_text(_docx_b64(evil))


def test_decode_docx_base64_raises_value_error_for_route():
    with pytest.raises(ValueError):
        decode_docx_base64("%%%")
    with pytest.raises(ValueError, match="zip"):
        decode_docx_base64(base64.b64encode(b"nope").decode())


def test_whitespace_wrapped_base64_is_accepted():
    content = _docx_b64(_doc("<w:p><w:r><w:t>Wrapped ok</w:t></w:r></w:p>"))
    wrapped = "\n".join(content[i:i + 76] for i in range(0, len(content), 76))
    assert extract_docx_text(wrapped) == "Wrapped ok"


def test_strict_ooxml_namespace_extracts():
    """Word's 'Strict Open XML Document (*.docx)' uses a different namespace —
    the extractor derives it from the root tag instead of hard-coding."""
    strict = (
        '<?xml version="1.0"?>'
        '<w:document xmlns:w="http://purl.oclc.org/ooxml/wordprocessingml/main">'
        "<w:body><w:p><w:r><w:t>Strict format text</w:t></w:r></w:p></w:body>"
        "</w:document>"
    )
    assert extract_docx_text(_docx_b64(strict)) == "Strict format text"


def test_content_control_paragraphs_are_extracted():
    """Consultant templates wrap body content in w:sdt content controls —
    those paragraphs must extract, without duplicating table-cell text."""
    content = _docx_b64(_doc(
        "<w:sdt><w:sdtContent>"
        "<w:p><w:r><w:t>Inside a content control</w:t></w:r></w:p>"
        "</w:sdtContent></w:sdt>"
        "<w:p><w:r><w:t>Plain paragraph</w:t></w:r></w:p>"
    ))
    assert extract_docx_text(content) == "Inside a content control\nPlain paragraph"


def test_zip_bomb_declared_size_is_rejected():
    """A document.xml whose declared inflated size exceeds the cap must be
    rejected before decompression (zipfile reads at most the declared size,
    so the declared value is an effective bound)."""
    import zlib

    big = b"<w:document>" + b" " * (60 * 1024 * 1024) + b"</w:document>"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", big)
    payload = base64.b64encode(buf.getvalue()).decode()
    assert len(payload) < 2_000_000  # the bomb is small on the wire

    with pytest.raises(PermanentError, match="inflates to"):
        extract_docx_text(payload)
    del big


def test_oversized_extracted_text_is_actionable_permanent_error():
    content = _docx_b64(_doc(
        "<w:p><w:r><w:t>" + "Requirement text. " * 20000 + "</w:t></w:r></w:p>"
    ))
    with pytest.raises(PermanentError, match="too large to plan from"):
        extract_docx_text(content)
