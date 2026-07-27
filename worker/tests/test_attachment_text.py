"""Tests for reva.attachment_text — extension-gated docx/pdf/txt extraction.

classify_attachment is the cheap accept-time gate (raises ValueError so the api
route maps it to a 422); extract_attachment_text is the worker-side extractor
(raises PermanentError, like reva.docx_text).
"""

from __future__ import annotations

import base64
import io
import zipfile

import pytest

from reva.attachment_text import classify_attachment, extract_attachment_text
from reva.errors import PermanentError

_W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _docx_b64(paragraph: str = "Spec body") -> str:
    document = (
        f'<?xml version="1.0"?><w:document {_W_NS}><w:body>'
        f"<w:p><w:r><w:t>{paragraph}</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("word/document.xml", document)
    return _b64(buf.getvalue())


def _pdf_b64(text: str) -> str:
    """Build a minimal, well-formed PDF with one text-showing operator and a
    correct xref table so pypdf extracts `text` deterministically."""
    stream = f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref_pos = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\n" % (len(objects) + 1)
    out += b"startxref\n%d\n%%%%EOF" % xref_pos
    return _b64(bytes(out))


# --- classify_attachment: the accept-time gate --------------------------------


def test_classify_accepts_txt_pdf_docx_by_extension():
    assert classify_attachment("notes.txt", _b64(b"hello"))[0] == "txt"
    assert classify_attachment("spec.pdf", _pdf_b64("hi"))[0] == "pdf"
    assert classify_attachment("spec.docx", _docx_b64())[0] == "docx"


def test_classify_accepts_md_by_extension():
    assert classify_attachment("notes.md", _b64(b"# Heading"))[0] == "md"


def test_classify_is_case_insensitive_on_extension():
    assert classify_attachment("NOTES.TXT", _b64(b"hello"))[0] == "txt"
    assert classify_attachment("SPEC.PDF", _pdf_b64("hi"))[0] == "pdf"
    assert classify_attachment("NOTES.MD", _b64(b"# Heading"))[0] == "md"


def test_classify_rejects_unsupported_extension():
    with pytest.raises(ValueError, match="unsupported attachment 'sheet.xlsx'"):
        classify_attachment("sheet.xlsx", _b64(b"PK\x03\x04whatever"))


def test_classify_rejects_filename_without_extension():
    with pytest.raises(ValueError, match="unsupported attachment 'README'"):
        classify_attachment("README", _b64(b"hello"))


def test_classify_rejects_invalid_base64():
    with pytest.raises(ValueError, match="not valid base64"):
        classify_attachment("notes.txt", "%%%not base64%%%")


def test_classify_rejects_content_not_matching_extension():
    # .pdf bytes without the %PDF- header
    with pytest.raises(ValueError, match="does not match its .pdf"):
        classify_attachment("spec.pdf", _b64(b"not a pdf"))
    # .docx bytes that aren't a zip
    with pytest.raises(ValueError, match="does not match its .docx"):
        classify_attachment("spec.docx", _b64(b"not a zip"))
    # .txt bytes that aren't utf-8 decodable
    with pytest.raises(ValueError, match="does not match its .txt"):
        classify_attachment("notes.txt", _b64(b"\xff\xfe\x00\x01binary"))
    # .md bytes that aren't utf-8 decodable
    with pytest.raises(ValueError, match="does not match its .md"):
        classify_attachment("notes.md", _b64(b"\xff\xfe\x00\x01binary"))


# --- extract_attachment_text: the worker-side extractor -----------------------


def test_extract_txt_returns_decoded_text():
    assert extract_attachment_text("notes.txt", _b64(b"Line one\nLine two")) == (
        "Line one\nLine two"
    )


def test_extract_txt_tolerates_utf8_bom():
    assert extract_attachment_text("notes.txt", _b64(b"\xef\xbb\xbfHello")) == "Hello"


def test_extract_md_returns_decoded_text():
    assert extract_attachment_text("notes.md", _b64(b"# Title\n\nBody text.")) == (
        "# Title\n\nBody text."
    )


def test_extract_pdf_returns_text():
    assert "Hello PDF" in extract_attachment_text("spec.pdf", _pdf_b64("Hello PDF"))


def test_extract_docx_delegates_to_docx_extractor():
    assert extract_attachment_text("spec.docx", _docx_b64("Requirement one")) == (
        "Requirement one"
    )


def test_extract_empty_txt_is_permanent():
    with pytest.raises(PermanentError, match="no extractable text"):
        extract_attachment_text("notes.txt", _b64(b"   \n  "))


def test_extract_oversized_txt_is_permanent():
    huge = ("word " * 100_000).encode()
    with pytest.raises(PermanentError, match="too large"):
        extract_attachment_text("notes.txt", _b64(huge))


def test_extract_oversized_md_is_permanent():
    huge = ("word " * 100_000).encode()
    with pytest.raises(PermanentError, match="too large"):
        extract_attachment_text("notes.md", _b64(huge))


def test_extract_corrupt_pdf_is_permanent():
    # Valid %PDF- header (passes classify) but a body pypdf cannot parse.
    with pytest.raises(PermanentError):
        extract_attachment_text("spec.pdf", _b64(b"%PDF-1.4\ngarbage not a pdf"))


def test_extract_unsupported_extension_is_permanent():
    with pytest.raises(PermanentError, match="unsupported attachment"):
        extract_attachment_text("sheet.xlsx", _b64(b"PK\x03\x04whatever"))
