"""Extension-gated extraction of attachment text (docx / pdf / txt / md).

Odoo forwards consultant files (Contract 1 description_docx, and the ticket
analysis attachment). The accepted set is .docx / .pdf / .txt / .md; everything
else is rejected so the api route returns a 422 and Odoo shows the error.

The filename extension is the authoritative gate — .xlsx/.pptx and .docx all
share the zip magic, so content sniffing alone can't tell them apart — and the
bytes are then verified against that extension. classify_attachment is the cheap
accept-time gate (raises ValueError so the route maps it to a 422);
extract_attachment_text is the worker-side extractor (raises PermanentError,
mirroring reva.docx_text).
"""

from __future__ import annotations

import base64
import binascii
import io
import os

from reva.docx_text import DOCX_MAGIC, MAX_EXTRACTED_CHARS, extract_docx_text
from reva.errors import PermanentError

_PDF_MAGIC = b"%PDF-"
_ALLOWED_EXTENSIONS = {".docx", ".pdf", ".txt", ".md"}


def classify_attachment(filename: str, content_base64: str) -> tuple[str, bytes]:
    """Return (kind, decoded_bytes) for a supported attachment.

    kind is "docx" | "pdf" | "txt" | "md". The extension gates the type; the
    bytes are verified against it (cheap — the pdf check is just the %PDF-
    prefix, no parse). Raises ValueError (not PermanentError) so the api route
    maps it to a 422 at accept time while Odoo still shows the error.
    """
    ext = os.path.splitext(filename)[1].lower()
    if ext not in _ALLOWED_EXTENSIONS:
        raise ValueError(
            f"unsupported attachment {filename!r}; only .docx, .pdf, .txt, .md are accepted"
        )

    compact = "".join(content_base64.split())  # tolerate MIME-style line wrapping
    try:
        data = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"{filename}: content is not valid base64") from exc

    if ext == ".docx":
        matches = data.startswith(DOCX_MAGIC)
    elif ext == ".pdf":
        matches = data.startswith(_PDF_MAGIC)
    else:  # .txt / .md
        matches = _is_utf8(data)
    if not matches:
        raise ValueError(f"{filename}: content does not match its {ext} extension")

    return ext.lstrip("."), data


def extract_attachment_text(filename: str, content_base64: str) -> str:
    """Return the attachment's text. Raises PermanentError on a corrupt or
    oversized file, or one without extractable text (retrying can't fix the
    input) — mirrors reva.docx_text.extract_docx_text."""
    try:
        kind, data = classify_attachment(filename, content_base64)
    except ValueError as exc:
        raise PermanentError(f"invalid attachment: {exc}") from exc

    if kind == "docx":
        return extract_docx_text(content_base64)
    if kind == "pdf":
        text = _extract_pdf_text(data, filename)
    else:
        text = data.decode("utf-8-sig")
    return _capped(text, filename)


def _is_utf8(data: bytes) -> bool:
    try:
        data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False
    return True


def _capped(text: str, filename: str) -> str:
    """Shared empty/oversized guard for the txt and pdf paths (docx has its own
    inside extract_docx_text)."""
    text = text.strip()
    if not text:
        raise PermanentError(f"invalid attachment: {filename} contains no extractable text")
    if len(text) > MAX_EXTRACTED_CHARS:
        raise PermanentError(
            f"attachment {filename} is too large to process ({len(text)} characters, "
            f"max {MAX_EXTRACTED_CHARS})"
        )
    return text


def _extract_pdf_text(data: bytes, filename: str) -> str:
    # pypdf is a worker-only extraction dep (worker/requirements.txt), imported
    # lazily so the api/scheduler images can import this module to classify
    # attachments without pulling pypdf in. Text only — no rendering, no JS.
    import pypdf

    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:
        raise PermanentError(f"invalid attachment: {filename}: {exc}") from exc
    return "\n".join(pages)
