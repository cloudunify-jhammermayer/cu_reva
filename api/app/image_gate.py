"""Accept-time gate for forwarded images, shared by the two routes that take them.

Support requests and ticket analyses accept the same `images` array from Odoo
(spec 2026-08-10-support-answer-images-design), so the gate lives here rather
than being copied: a cap that drifts between the two endpoints is a cap that
does not exist.

422 is the only error channel back to Odoo on both paths — worker failures land
in the DB where the consultant never sees them — so every rejection names the
offending image.
"""

from __future__ import annotations

from fastapi import HTTPException, status

from reva.image_attachment import (
    MAX_IMAGES,
    MAX_TOTAL_IMAGE_BYTES,
    classify_image,
)
from reva.types import ImageAttachment


def assert_images_acceptable(images: list[ImageAttachment]) -> None:
    """Count, per-image type/size, label shape, total budget, label uniqueness."""
    if len(images) > MAX_IMAGES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"images: at most {MAX_IMAGES} images per request, got {len(images)}",
        )
    seen_labels: set[str] = set()
    total = 0
    for image in images:
        try:
            _, data = classify_image(image.filename, image.label, image.content_base64)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"images: {exc}",
            ) from exc
        # Duplicate labels would make two blocks indistinguishable to the model
        # AND ambiguous against the [Image N] markers in the question text.
        if image.label in seen_labels:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"images: duplicate label {image.label!r}",
            )
        seen_labels.add(image.label)
        total += len(data)
        if total > MAX_TOTAL_IMAGE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"images: total decoded size exceeds {MAX_TOTAL_IMAGE_BYTES} bytes"
                ),
            )
