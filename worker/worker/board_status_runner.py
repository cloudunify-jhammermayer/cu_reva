"""GitHub Projects board Status sync — the linked-PR legs native workflows
can't express (board-status spec 2026-07-10).

Native project workflows own added->Todo and closed/merged->Done; this job
owns only: linked PR active -> "In Progress", REVA review completed ->
"In review". Existing options only — never creates fields or options, never
touches Todo/Done. Fail-soft by design: the board is a bonus; every
degradation logs AND records an ops event (component="board_status")."""

from __future__ import annotations

from collections import defaultdict

import structlog
import yaml

from reva.db import writers
from reva.errors import TransientError
from reva.github_urls import parse_github_project_url
from reva.ticket_links import parse_closing_refs
from reva.types import RepoConfig
from worker.runner import get_context

logger = structlog.get_logger()

_OPTION_BY_TRIGGER = {"pr_active": "In Progress", "review_done": "In review"}


def run_board_status_update(job_params: dict) -> dict:
    ctx = get_context()
    repo = job_params["repo_full_name"].lower()
    pr_number = job_params["pr_number"]
    trigger = job_params["trigger"]
    option_name = _OPTION_BY_TRIGGER.get(trigger)
    if option_name is None:
        return {"status": "unknown_trigger"}
    owner, name = repo.split("/", 1)
    log = logger.bind(repo=repo, pr=pr_number, trigger=trigger)

    try:
        token = ctx.github.get_installation_token(job_params["installation_id"])
        pr = ctx.github.get_pull_request(token, owner, name, pr_number)
    except TransientError:
        raise
    except Exception as exc:  # noqa: BLE001 — this job persists nothing; ops events are its only observability surface
        log.warning("board_status_pr_fetch_failed", exc_info=True)
        writers.record_ops_event(
            ctx.db, "board_status", "warning", "pr_fetch_failed",
            {"repo": repo, "pr": pr_number, "error": str(exc)[:300]},
        )
        return {"status": "failed"}

    if trigger == "review_done" and (pr.get("merged") or pr.get("state") == "closed"):
        # A merge/close landed mid-review: don't drag a Done card back to
        # "In review". Normal lifecycle, not degradation — no ops event.
        log.debug("board_status_pr_closed")
        return {"status": "pr_closed"}

    refs = parse_closing_refs(pr.get("body"))
    # Full union with GitHub's authoritative link set (sidebar links have no
    # body keyword — and they can coexist with body refs, so this is not a
    # fallback). Degrades to body refs visibly, like the reviewer path.
    try:
        for n in ctx.github.get_closing_issue_numbers(token, owner, name, pr_number):
            if n not in refs:
                refs.append(n)
    except TransientError:
        raise
    except Exception as exc:  # noqa: BLE001 — degrade, stay visible
        log.warning("board_status_link_resolution_failed", exc_info=True)
        writers.record_ops_event(
            ctx.db, "board_status", "warning", "link_resolution_failed",
            {"repo": repo, "pr": pr_number, "error": str(exc)[:300]},
        )
    items = writers.get_board_items_for_issues(ctx.db, repo, refs)
    if not items:
        return {"status": "no_board_items"}

    if not _sync_enabled(ctx, token, owner, name, pr, log):
        return {"status": "disabled"}

    by_board: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        by_board[item["github_project_url"]].append(item)

    moved = 0
    for project_url, board_items in by_board.items():
        resolved = _resolve_status_option(ctx, token, project_url, option_name, log)
        if resolved is None:
            continue
        project_id, field_id, option_id = resolved
        for item in board_items:
            try:
                ctx.github.set_project_item_option(
                    token, project_id, item["project_item_id"], field_id, option_id
                )
                moved += 1
            except TransientError:
                raise  # idempotent job — RQ retries the whole update safely
            except Exception as exc:  # noqa: BLE001
                log.warning("board_status_set_failed", issue=item["number"], exc_info=True)
                writers.record_ops_event(
                    ctx.db, "board_status", "warning", "set_option_failed",
                    {"repo": repo, "pr": pr_number, "issue": item["number"],
                     "error": str(exc)[:300]},
                )
    log.info("board_status_updated", moved=moved, option=option_name)
    return {"status": "completed", "moved": moved}


def _sync_enabled(ctx, token: str, owner: str, name: str, pr: dict, log) -> bool:
    """Per-repo kill switch from .claude-review.yml at the PR head. Fail-open:
    a config hiccup must not silently freeze boards (mirror of the webhook's
    _change_notes_enabled semantics)."""
    try:
        raw = ctx.github.get_file_content(
            token, owner, name, ".claude-review.yml", pr["head"]["sha"]
        )
        if not raw:
            return True
        parsed = yaml.safe_load(raw) or {}
        return RepoConfig.model_validate(parsed).board_status_sync
    except Exception as exc:  # noqa: BLE001
        log.warning("board_status_config_failed", exc_info=True)
        writers.record_ops_event(
            ctx.db, "board_status", "warning", "config_fetch_failed",
            {"repo": f"{owner}/{name}", "error": str(exc)[:300]},
        )
        return True


def _resolve_status_option(
    ctx, token: str, project_url: str, option_name: str, log
) -> tuple[str, str, str] | None:
    """(project_id, status_field_id, option_id) — existing options only.

    A board without a Status single-select or without the wanted option is
    CONFIGURATION, not degradation: debug log + skip, no ops event (the
    operator chose their columns). GraphQL errors propagate to the caller's
    fail-soft/transient handling."""
    parsed = parse_github_project_url(project_url or "")
    if parsed is None:
        log.warning("board_status_project_url_invalid", url=project_url)
        writers.record_ops_event(
            ctx.db, "board_status", "warning", "project_url_invalid",
            {"url": (project_url or "")[:200]},
        )
        return None
    owner_type, owner, number = parsed
    try:
        project = ctx.github.get_project(token, owner_type, owner, number)
    except TransientError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("board_status_project_lookup_failed", exc_info=True)
        writers.record_ops_event(
            ctx.db, "board_status", "warning", "project_lookup_failed",
            {"url": project_url[:200], "error": str(exc)[:300]},
        )
        return None
    status_field = next(
        (f for f in project["fields"]
         if f["name"].lower() == "status" and f["dataType"] == "SINGLE_SELECT"),
        None,
    )
    if status_field is None:
        log.debug("board_status_no_status_field", url=project_url)
        return None
    option = next(
        (o for o in status_field.get("options") or []
         if o["name"].lower() == option_name.lower()),
        None,
    )
    if option is None:
        log.debug("board_status_option_missing", wanted=option_name, url=project_url)
        return None
    return project["id"], status_field["id"], option["id"]
