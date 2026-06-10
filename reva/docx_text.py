"""Extract plain text from a .docx for the issue planner.

Consultants deliver task specs as Word files (Contract 1, description_docx).
The Claude Messages API has no docx document block, so the worker extracts the
text itself. Stdlib + defusedxml on purpose: a .docx is a zip whose
word/document.xml holds the body — paragraphs (w:p) and tables (w:tbl), also
when nested in containers like content controls (w:sdt), carry a spec's
content; headers/footers/textboxes are ignored.

The input is customer-supplied and untrusted:
  - defusedxml refuses entity declarations (XXE / billion-laughs),
  - the inflated size of document.xml is capped before decompression
    (zip-bomb; Python's zipfile reads at most the declared file_size, so the
    declared value is an effective bound),
  - the extracted text is capped so a huge-but-legitimate document fails with
    an actionable message instead of an opaque Claude 400 at plan time.
"""

from __future__ import annotations

import base64
import binascii
import io
import zipfile
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from xml.etree.ElementTree import ParseError

from reva.errors import PermanentError

# Zip local-file-header magic — cheap pre-check shared with the api route.
DOCX_MAGIC = b"PK\x03\x04"

# Inflated document.xml cap. Real specs run well under 10 MB of XML; a deflate
# bomb reaches GBs from a few MB compressed.
_MAX_DOCUMENT_XML_BYTES = 50 * 1024 * 1024
# Extracted-text cap (~75k tokens) so the planning prompt stays inside the
# model's context window with room for the system prompt and the plan output.
MAX_EXTRACTED_CHARS = 300_000


def decode_docx_base64(content_base64: str) -> bytes:
    """Decode and sanity-check a base64 docx payload.

    Tolerates embedded whitespace/newlines (MIME-style wrapping). Raises
    ValueError (not PermanentError) so the api route can map it to a 422 at
    accept time, while Odoo still shows the error to the user."""
    compact = "".join(content_base64.split())
    try:
        data = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"content_base64 is not valid base64: {exc}") from exc
    if not data.startswith(DOCX_MAGIC):
        raise ValueError("content is not a .docx (zip) file")
    return data


def extract_docx_text(content_base64: str) -> str:
    """Return the document body text: one line per paragraph, tables as
    ' | '-joined cell rows. Raises PermanentError on a corrupt or oversized
    document, or one without extractable text (retrying cannot fix the input).
    """
    try:
        data = decode_docx_base64(content_base64)
    except ValueError as exc:
        raise PermanentError(f"invalid description_docx: {exc}") from exc

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > _MAX_DOCUMENT_XML_BYTES:
                raise PermanentError(
                    "invalid description_docx: document.xml inflates to "
                    f"{info.file_size} bytes (max {_MAX_DOCUMENT_XML_BYTES})"
                )
            document = archive.read("word/document.xml")
        root = ElementTree.fromstring(document)
    except (zipfile.BadZipFile, KeyError, ParseError, DefusedXmlException) as exc:
        raise PermanentError(f"invalid description_docx: {exc}") from exc

    # Word emits the transitional OOXML namespace; "Strict Open XML Document"
    # uses a different one. Derive it from the root tag so both extract.
    ns = root.tag[: root.tag.index("}") + 1] if root.tag.startswith("{") else ""
    body = root.find(f"{ns}body")

    lines: list[str] = []
    _collect_blocks(body if body is not None else root, ns, lines)

    text = "\n".join(lines).strip()
    if not text:
        raise PermanentError(
            "invalid description_docx: document contains no extractable text"
        )
    if len(text) > MAX_EXTRACTED_CHARS:
        raise PermanentError(
            f"description_docx is too large to plan from ({len(text)} characters, "
            f"max {MAX_EXTRACTED_CHARS}); split the work into smaller documents"
        )
    return text


def _collect_blocks(parent, ns: str, lines: list[str]) -> None:
    """Walk block-level content in document order. Containers (e.g. w:sdt
    content controls, common in consultant Word templates) are recursed into;
    paragraphs inside tables are rendered only as part of their table row."""
    for element in parent:
        if element.tag == f"{ns}p":
            text = _paragraph_text(element, ns)
            if text:
                lines.append(text)
        elif element.tag == f"{ns}tbl":
            for row in element.iter(f"{ns}tr"):
                cells = [
                    " ".join(
                        _paragraph_text(p, ns) for p in cell.iter(f"{ns}p")
                    ).strip()
                    for cell in row.findall(f"{ns}tc")
                ]
                if any(cells):
                    lines.append(" | ".join(cells))
        else:
            _collect_blocks(element, ns, lines)


def _paragraph_text(paragraph, ns: str) -> str:
    return "".join(t.text or "" for t in paragraph.iter(f"{ns}t")).strip()
