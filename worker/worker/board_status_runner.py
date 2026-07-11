"""GitHub Projects board Status sync + Odoo work-status callbacks — the
linked-PR signals native workflows can't express (board-status spec 2026-07-10,
work-status spec 2026-07-11).

Two independent legs fire on the same two triggers (pr_active from the api
webhook, review_done from the reviewer):

  - Board leg: moves cards to "In Progress"/"In review" (existing options only —
    never creates fields/options, never touches Todo/Done; native workflows own
    added->Todo and closed/merged->Done). Gated on RepoConfig.board_status_sync.
  - Odoo leg: sends per-issue work-status hints (in_progress/in_review) for every
    REVA-created issue linked by the PR, board or no board. Gated on
    RepoConfig.work_status.

Both switches read from ONE .claude-review.yml fetch. Fail-soft by design: the
board and the callbacks are a bonus; every degradation logs AND records an ops
event (component="board_status" for the board leg, "odoo_callback" for the
Odoo leg)."""

from __future__ import annotations

from collections import defaultdict

import structlog
import yaml

from reva.db import writers
from reva.errors import PermanentError, TransientError
from reva.github_urls import parse_github_project_url
from reva.ticket_links import parse_closing_refs, resolve_pr_tickets
from reva.types import RepoConfig
from worker.runner import build_odoo_client, get_context

logger = structlog.get_logger()

_OPTION_BY_TRIGGER = {"pr_active": "In Progress", "review_done": "In review"}
# Odoo work-status leg (spec 2026-07-11): the same two triggers map to Odoo's
# per-issue display hints, independent of the board leg.
_WORK_STATUS_BY_TRIGGER = {"pr_active": "in_progress", "review_done": "in_review"}


def run_board_status_update(job_params: dict) -> dict:
    ctx = get_context()
    repo = job_params["repo_full_name"].lower()
    pr_number = job_params["pr_number"]
    trigger = job_params["trigger"]
    option_name = _OPTION_BY_TRIGGER.get(trigger)
    work_status = _WORK_STATUS_BY_TRIGGER.get(trigger)
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

    if pr.get("merged") or pr.get("state") == "closed":
        # A merge/close landed before/mid-signal: don't drag a Done card back
        # to "In Progress"/"In review", nor emit a work status for a dead PR
        # (guard extended to pr_active per the 2026-07-10 review). Normal
        # lifecycle, not degradation — no ops event.
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
    if not refs:
        # No linked issues → neither leg has anything to do; skip the config
        # fetch entirely.
        return {"status": "no_refs"}

    # ONE config fetch serves both kill switches (spec 2026-07-11).
    board_enabled, work_enabled = _repo_flags(ctx, token, owner, name, pr, log)

    # --- Odoo work-status leg (board-independent) ---
    if work_enabled and work_status is not None:
        _update_work_status(ctx, repo, refs, work_status, log)

    # --- Board leg ---
    if not board_enabled:
        log.debug("board_status_disabled")
        return {"status": "disabled"}

    items = writers.get_board_items_for_issues(ctx.db, repo, refs)
    if not items:
        return {"status": "no_board_items"}

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


def _repo_flags(ctx, token: str, owner: str, name: str, pr: dict, log) -> tuple[bool, bool]:
    """Both per-repo kill switches from ONE .claude-review.yml fetch at the PR
    head: (board_status_sync, work_status). Fail-open: a config hiccup must not
    silently freeze the board or the Odoo callbacks (mirror of the webhook's
    _change_notes_enabled semantics)."""
    try:
        raw = ctx.github.get_file_content(
            token, owner, name, ".claude-review.yml", pr["head"]["sha"]
        )
        if not raw:
            return True, True
        cfg = RepoConfig.model_validate(yaml.safe_load(raw) or {})
        return cfg.board_status_sync, cfg.work_status
    except Exception as exc:  # noqa: BLE001
        log.warning("board_status_config_failed", exc_info=True)
        writers.record_ops_event(
            ctx.db, "board_status", "warning", "config_fetch_failed",
            {"repo": f"{owner}/{name}", "error": str(exc)[:300]},
        )
        return True, True


def _update_work_status(ctx, repo: str, refs: list[int], work_status: str, log) -> None:
    """Send per-issue work-status hints to Odoo for the REVA-created issues this
    PR links, one callback per resolved ticket. Board-independent: resolves
    tickets the way change_note_runner does (resolve_pr_tickets), never via
    get_board_items_for_issues, so board-less tickets are included. Only the
    issues linked by THIS PR are sent (intersection with the ticket's union) —
    Odoo upserts by number against existing records."""
    for ref in resolve_pr_tickets(ctx.db, repo, refs):
        union = writers.get_ticket_issue_union(
            ctx.db, ref.odoo_instance_id, ref.ticket_id, ref.model_name
        )
        union_numbers = {item["number"] for item in union}
        matched = sorted(n for n in refs if n in union_numbers)
        if not matched:
            continue
        try:
            odoo = build_odoo_client(ctx, ref.odoo_instance_id)
            odoo.issue_work_status(
                ticket_id=ref.ticket_id,
                model_name=ref.model_name,
                issues=[{"number": n, "work_status": work_status} for n in matched],
            )
        except TransientError:
            raise  # idempotent no-op upsert in Odoo — RQ retries the whole job
        except PermanentError:
            log.warning("work_status_rejected", ticket_id=ref.ticket_id, exc_info=True)
            writers.record_ops_event(
                ctx.db, "odoo_callback", "warning", "work_status_rejected",
                {"ticket_id": ref.ticket_id},
            )


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
