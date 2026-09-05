"""Find a ticket's entry in a repo's release logs (spec 2026-09-04-release-log-change-notes).

The customer repo keeps docs/releases/<name>.md, one entry per ticket. The merge
job asks whether the ticket has one (then Claude is not needed) and the ready-
time delivery re-reads it so the customer gets the final text. Read from the
repo's default branch through the GitHub API, the same way the docs site does.
"""

from __future__ import annotations

import posixpath

import structlog

from reva.db import writers
from reva.errors import TransientError
from reva.release_log import (
    RELEASE_DIR,
    ReleaseEntry,
    ReleaseLogDoc,
    ReleaseLogParseError,
    parse_release_log,
    render_entry_html,
)

logger = structlog.get_logger()


class ReleaseLogLookupError(Exception):
    """A non-transient GitHub error while looking up a ticket's release-log entry."""


def _release_log_paths(tree: dict) -> list[str]:
    paths = []
    for entry in tree.get("tree", []):
        path = entry.get("path", "")
        if (
            entry.get("type") == "blob"
            and posixpath.dirname(path) == RELEASE_DIR
            and path.endswith(".md")
        ):
            paths.append(path)
    return sorted(paths)


def find_release_entry(
    ctx, token: str, owner: str, name: str, ref: str, ticket_id: int, log
) -> tuple[ReleaseLogDoc, ReleaseEntry] | None:
    """(doc, entry) for the ticket, or None. Open logs win over frozen/shipped
    ones, then the alphabetically first file; several hits are an ops event so
    the duplicate gets cleaned up. An unparseable log is skipped with an ops
    event. GitHub errors propagate (a TransientError makes RQ retry the job)."""
    log = log or logger
    tree = ctx.github.get_tree(token, owner, name, ref)
    hits: list[tuple[str, ReleaseLogDoc, ReleaseEntry]] = []
    for path in _release_log_paths(tree):
        text = ctx.github.get_file_content(token, owner, name, path, ref)
        if text is None:
            continue
        try:
            doc = parse_release_log(text)
        except ReleaseLogParseError as exc:
            log.warning("release_log_parse_failed", repo=f"{owner}/{name}", path=path, error=str(exc))
            writers.record_ops_event(
                ctx.db, "release_log", "warning", "release_log_parse_failed",
                {"repo": f"{owner}/{name}", "path": path, "error": str(exc)[:300]},
            )
            continue
        entry = doc.entries.get(ticket_id)
        if entry is not None:
            hits.append((path, doc, entry))
    if not hits:
        return None
    if len(hits) > 1:
        writers.record_ops_event(
            ctx.db, "release_log", "info", "release_log_entry_ambiguous",
            {"repo": f"{owner}/{name}", "ticket_id": ticket_id, "paths": [h[0] for h in hits]},
        )
    hits.sort(key=lambda h: (h[1].status != "open", h[0]))
    _, doc, entry = hits[0]
    return doc, entry


def release_log_block(ctx, repo_full_name: str, ticket_id: int, log) -> dict | None:
    """The `release_log` block of the change-summary callback for a ticket, or
    None when the repo is unknown or carries no entry for it.

    A `TransientError` from the GitHub calls propagates (RQ retries the caller);
    any other GitHub error is an ops event plus `ReleaseLogLookupError`."""
    log = log or logger
    repo = writers.get_repository_by_full_name(ctx.db, repo_full_name)
    if repo is None:
        return None
    try:
        token = ctx.github.get_installation_token(repo["installation_id"])
        found = find_release_entry(
            ctx, token, repo["owner"], repo["name"], repo["default_branch"], ticket_id, log
        )
    except TransientError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("release_log_lookup_failed", repo=repo_full_name, ticket_id=ticket_id, error=str(exc))
        writers.record_ops_event(
            ctx.db, "release_log", "error", "release_log_lookup_failed",
            {"repo": repo_full_name, "ticket_id": ticket_id, "error": str(exc)[:300]},
        )
        raise ReleaseLogLookupError(str(exc)) from exc
    if found is None:
        return None
    doc, entry = found
    return {
        "release": doc.release,
        "ticket": ticket_id,
        "title": entry.title,
        "status": entry.status,
        "modules": list(entry.modules),
        "html": render_entry_html(entry),
    }


__all__ = ["ReleaseLogLookupError", "find_release_entry", "release_log_block"]
