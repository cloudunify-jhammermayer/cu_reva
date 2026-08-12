"""Extension-gated validation of forwarded images (png / jpeg / gif / webp).

Odoo forwards the screenshots embedded in a ticket's description (spec
2026-08-10-support-answer-images-design). This is the image sibling of
reva.attachment_text: same posture, different accepted set — the filename
extension is the authoritative gate and the bytes are then verified against it
by magic number, so a .png renamed to .jpg is rejected rather than sent with a
media_type the API will reject.

Unlike attachments there is no extraction step: the bytes go to the Messages
API as an image content block, so classify_image is the whole module.

SECU: `label` is echoed verbatim into the prompt as the text block introducing
its image, which makes it an injection surface — it is pinned to "Image <n>".
`filename` is NEVER shown to the model; it exists only to carry the extension.
"""

from __future__ import annotations

import base64
import binascii
import os
import re

# Anthropic Messages API accepted image types. Animations are not supported —
# a GIF's first frame is what the model sees.
_ALLOWED_EXTENSIONS = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

# The only shape Odoo may send. Anything else is rejected rather than sanitised:
# this string is a text content block sitting outside the SECU-5 nonce fence,
# directly ahead of untrusted image bytes.
_LABEL_RE = re.compile(r"^Image \d{1,2}$")

# REVA caps, all well inside the API's (100 images/request for a 200k-context
# model, 10 MB base64 per image, 32 MB per request). A support mail is
# screenshots plus a signature, not an album; six leaves room for the prompt and
# keeps us under the >20-blocks rule that tightens the per-image dimension limit.
MAX_IMAGES = 6
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 8 * 1024 * 1024

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"
_GIF_MAGICS = (b"GIF87a", b"GIF89a")
_RIFF_MAGIC = b"RIFF"
_WEBP_MAGIC = b"WEBP"


def classify_image(filename: str, label: str, content_base64: str) -> tuple[str, bytes]:
    """Return (media_type, decoded_bytes) for a supported image.

    Raises ValueError (not PermanentError) so the api route maps it to a 422 at
    accept time while Odoo still shows the error — same error channel as
    reva.attachment_text.classify_attachment.
    """
    ext = os.path.splitext(filename)[1].lower()
    media_type = _ALLOWED_EXTENSIONS.get(ext)
    if media_type is None:
        raise ValueError(
            f"unsupported image {filename!r}; only .png, .jpg, .jpeg, .gif, .webp are accepted"
        )

    if not _LABEL_RE.match(label):
        raise ValueError(f"image label {label!r} must be of the form 'Image 1'")

    compact = "".join(content_base64.split())  # tolerate MIME-style line wrapping
    try:
        data = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"{filename}: content is not valid base64") from exc

    if not _matches_magic(ext, data):
        raise ValueError(f"{filename}: content does not match its {ext} extension")

    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(
            f"{filename} is too large ({len(data)} bytes, max {MAX_IMAGE_BYTES})"
        )

    return media_type, data


def _matches_magic(ext: str, data: bytes) -> bool:
    if ext == ".png":
        return data.startswith(_PNG_MAGIC)
    if ext in (".jpg", ".jpeg"):
        return data.startswith(_JPEG_MAGIC)
    if ext == ".gif":
        return data.startswith(_GIF_MAGICS)
    # WEBP is a RIFF container: "RIFF" <4-byte size> "WEBP".
    return data.startswith(_RIFF_MAGIC) and data[8:12] == _WEBP_MAGIC
