"""Stage forwarded images as files for the headless-CLI paths.

The Messages API takes image bytes inline; the CLI cannot. Both escalating
paths — a support turn and a ticket analysis that the planner flagged
`needs_repo_code` — therefore write the images as real files and hand Claude
the directory via `--add-dir`, where the already-allowed `Read` tool picks them
up. No new capability is granted.

Shared rather than copied: the SECU constraints below are the kind that rot
apart when two runners each keep their own version.
"""

from __future__ import annotations

import base64
import contextlib
import os
import tempfile

import structlog

from reva.db import writers
from reva.image_attachment import classify_image
from reva.types import ImageAttachment

logger = structlog.get_logger()


@contextlib.contextmanager
def staged_images(
    ctx,
    images: list[ImageAttachment],
    component: str,
    event_context: dict,
):
    """Yield (extra_dir, paths) for the CLI path, or (None, []) when there is
    nothing to stage.

    Deliberately OUTSIDE the clone: writing into the working tree would dirty it
    and cross the _scrub_clone boundary (SECU-1). Filenames come from the
    validated label, never from the untrusted `filename` field.

    A staging failure degrades to a code-grounded but image-blind run rather
    than failing the job — logged AND recorded as an ops event. `component` and
    `event_context` name the caller in that event ("support_answer" /
    "ticket_analysis" plus its run id).
    """
    if not images:
        yield None, []
        return
    try:
        with tempfile.TemporaryDirectory(prefix=f"reva-{component}-images-") as tmp:
            paths = []
            for image in images:
                media_type, data = classify_image(
                    image.filename, image.label, image.content_base64
                )
                ext = media_type.split("/", 1)[1]
                safe = image.label.lower().replace(" ", "-")  # "Image 1" -> image-1
                path = os.path.join(tmp, f"{safe}.{ext}")
                with open(path, "wb") as fh:
                    fh.write(data)
                paths.append(f"{image.label}: {path}")
            yield tmp, paths
    except (OSError, ValueError, base64.binascii.Error) as exc:
        logger.warning(f"{component}_image_staging_failed", error=str(exc),
                       **event_context)
        writers.record_ops_event(
            ctx.db, component, "warning", "image_staging_failed",
            {**event_context, "error": str(exc)[:300]},
        )
        yield None, []
