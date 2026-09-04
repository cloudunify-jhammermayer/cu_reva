"""Release-log lookup job (spec docs/superpowers/specs/archive/2026-09-04-release-log-requirements.md, R2).

No Claude call and no task material: find `docs/releases/<slug>.html` in the
repos mapped to the calling Odoo instance and hand Odoo the docs-site URL, the
fragment and the theme CSS, or a `failed` status with a German reason.

The row turns `completed` only after Odoo accepted the callback, so an RQ
retry (TransientError anywhere) repeats the cheap lookup; nothing is stored
to resend. `failed` is terminal: the reason is recorded, Odoo is told best
effort, and the job ends through the shared task contract.
"""

from __future__ import annotations

from typing import NoReturn

import structlog

from reva import config, release_log
from reva.db import writers
from reva.errors import PermanentError, TransientError
from reva.types import ReleaseNoteJobParams
from worker.repo_config import load_repo_config
from worker.runner import build_odoo_client, get_context

logger = structlog.get_logger()


def _mapped_repos(ctx, instance_name: str, log) -> tuple[list[dict], list[str], int]:
    """Enabled repos whose `.claude-review.yml` on the default branch declares
    `odoo_instance: <instance_name>`, ascending id, each with its installation
    token. One config fetch per enabled repo, the way reviews and audits read
    it. A repo whose config cannot be fetched or parsed is skipped with an ops
    event: a broken repo must not hide the release log sitting in another one.
    A GitHub outage propagates as TransientError so RQ retries the whole
    lookup. Returns (mapped, skipped full names, number of enabled repos) so a
    caller can tell "nothing mapped" apart from "every repo was unreadable"."""
    mapped: list[dict] = []
    skipped: list[str] = []
    enabled = writers.list_enabled_repositories(ctx.db)
    for repo in enabled:
        try:
            token = ctx.github.get_installation_token(repo["installation_id"])

            def _invalid(reason: str, repo=repo) -> None:
                log.warning("release_log_config_invalid", repo=repo["full_name"], error=reason)
                writers.record_ops_event(
                    ctx.db, "release_log", "warning", "config_parse_failed",
                    {"repo": repo["full_name"], "error": reason[:300]},
                )

            cfg = load_repo_config(
                ctx.github, token, repo["owner"], repo["name"], repo["default_branch"],
                on_invalid=_invalid,
            )
        except TransientError:
            raise
        except Exception as exc:  # noqa: BLE001 — degrade per repo, visibly
            log.warning("release_log_config_fetch_failed", repo=repo["full_name"], error=str(exc))
            writers.record_ops_event(
                ctx.db, "release_log", "warning", "config_fetch_failed",
                {"repo": repo["full_name"], "error": str(exc)[:300]},
            )
            skipped.append(repo["full_name"])
            continue
        if cfg.odoo_instance == instance_name:
            mapped.append({**repo, "token": token})
    return mapped, skipped, len(enabled)


def _find_release_log(
    ctx, params: ReleaseNoteJobParams, mapped: list[dict], log
) -> tuple[dict, str] | None:
    """(repo, html) for the first mapped repo (ascending id) holding
    docs/releases/<slug>.html; None when none has it. Several hits are an ops
    event so the duplicate gets cleaned up on the repo side."""
    path = release_log.release_log_path(params.slug)
    hits: list[tuple[dict, str]] = []
    for repo in mapped:
        try:
            content = ctx.github.get_file_content(
                repo["token"], repo["owner"], repo["name"], path, repo["default_branch"]
            )
        except TransientError:
            raise
        except Exception as exc:  # noqa: BLE001 — one unreadable repo must not hide another's page
            log.warning("release_log_page_fetch_failed", repo=repo["full_name"], error=str(exc))
            writers.record_ops_event(
                ctx.db, "release_log", "warning", "page_fetch_failed",
                {"repo": repo["full_name"], "path": path, "error": str(exc)[:300]},
            )
            continue
        if content:
            hits.append((repo, content))
    if not hits:
        return None
    if len(hits) > 1:
        repos = [r["full_name"] for r, _ in hits]
        log.warning("release_doc_ambiguous", repos=repos)
        writers.record_ops_event(
            ctx.db, "release_log", "info", "release_doc_ambiguous",
            {"note_id": params.note_id, "slug": params.slug, "repos": repos},
        )
    return hits[0]


def _fail(ctx, params: ReleaseNoteJobParams, error: str, log) -> NoReturn:
    """Record the failure, tell Odoo (best effort, never masks the reason) and
    end the job terminally."""
    log.warning("release_note_failed", error=error)
    writers.record_release_note_failed(ctx.db, params.note_id, error)
    try:
        odoo = build_odoo_client(ctx, params.odoo_instance_id)
        odoo.release_note(
            release_id=params.release_id, note_id=params.note_id, status="failed",
            url=None, html=None, css=None, error=error,
        )
        writers.record_release_note_callback_sent(ctx.db, params.note_id)
    except Exception:  # noqa: BLE001
        log.warning("release_note_failed_callback_error", exc_info=True)
        writers.record_ops_event(
            ctx.db, "odoo_callback", "error", "release_note_failed_callback_error",
            {"note_id": params.note_id, "release_id": params.release_id},
        )
    raise PermanentError(error)


def run_release_note(job_params: dict) -> dict:
    """RQ task entry point for the release-log lookup."""
    ctx = get_context()
    params = ReleaseNoteJobParams.model_validate(job_params)
    log = logger.bind(
        note_id=params.note_id,
        release_id=params.release_id,
        odoo_instance_id=params.odoo_instance_id,
        slug=params.slug,
    )
    log.info("release_note_start")

    row = writers.get_release_note(ctx.db, params.note_id)
    if row is None:
        raise PermanentError(f"release note {params.note_id} not found")
    if row["status"] == "completed":
        log.info("release_note_resume_completed")
        return {"status": "completed", "note_id": params.note_id}
    if row["status"] == "failed":
        raise PermanentError(row["error"] or "release note already failed")

    instance = writers.get_odoo_instance(ctx.db, params.odoo_instance_id)
    if instance is None:
        raise PermanentError(f"odoo_instance {params.odoo_instance_id} not found")

    try:
        path = release_log.release_log_path(params.slug)
        mapped, skipped, total = _mapped_repos(ctx, instance["name"], log)
        hit = _find_release_log(ctx, params, mapped, log) if mapped else None
    except TransientError:
        log.warning("release_note_transient_error", exc_info=True)
        raise
    except PermanentError as exc:
        log.error("release_note_permanent_error", error=str(exc))
        _fail(ctx, params, f"GitHub-Zugriff fehlgeschlagen: {exc}", log)
    except Exception as exc:  # noqa: BLE001
        log.exception("release_note_unexpected_error")
        _fail(ctx, params, f"Unerwarteter Fehler: {exc}", log)

    if not mapped and total and len(skipped) == total:
        _fail(
            ctx, params,
            "GitHub-Zugriff fehlgeschlagen für alle Repositories: " + ", ".join(skipped),
            log,
        )
    if not mapped:
        _fail(
            ctx, params,
            f"Kein Repository mit `odoo_instance: {instance['name']}` in .claude-review.yml",
            log,
        )
    if hit is None:
        _fail(
            ctx, params,
            f"Kein Release-Log '{path}' in " + ", ".join(r["full_name"] for r in mapped),
            log,
        )
    repo, html = hit

    url = release_log.docs_site_page_url(repo["id"], path)
    if not config.DOCS_SITE_URL:
        log.warning("docs_site_url_unset", url=url)
        writers.record_ops_event(
            ctx.db, "release_log", "warning", "docs_site_url_unset",
            {"note_id": params.note_id, "url": url},
        )

    try:
        odoo = build_odoo_client(ctx, params.odoo_instance_id)
    except Exception as exc:  # noqa: BLE001 — bad callback URL / undecryptable key
        log.exception("release_note_odoo_client_error")
        _fail(ctx, params, f"Odoo-Client nicht konfigurierbar: {exc}", log)

    try:
        odoo.release_note(
            release_id=params.release_id,
            note_id=params.note_id,
            status="completed",
            url=url,
            html=html,
            css=release_log.theme_css(),
            error=None,
        )
    except PermanentError as exc:
        # 401/404/409: Odoo will not take this delivery (stale note_id, release
        # no longer pending). Terminal; the reason stays readable in the TUI.
        # But a 409 here can also mean the row was superseded by a re-submit
        # while this job was in flight (see submit_release_note's stale-pending
        # path) — that is the by-design outcome, not a failure to report.
        current = writers.get_release_note(ctx.db, params.note_id)
        if current is not None and current["status"] != "pending":
            log.info("release_note_superseded_delivery", error=str(exc))
            writers.record_ops_event(
                ctx.db, "release_log", "info", "release_note_superseded_delivery",
                {"note_id": params.note_id, "release_id": params.release_id},
            )
            raise
        log.warning("release_note_callback_permanent", exc_info=True)
        writers.record_release_note_failed(
            ctx.db, params.note_id, f"odoo callback rejected: {exc}"
        )
        writers.record_ops_event(
            ctx.db, "odoo_callback", "error", "release_note_callback_failed",
            {"note_id": params.note_id, "release_id": params.release_id},
        )
        raise
    except TransientError:
        log.warning("release_note_callback_error", exc_info=True)
        writers.record_ops_event(
            ctx.db, "odoo_callback", "error", "release_note_callback_failed",
            {"note_id": params.note_id, "release_id": params.release_id},
        )
        raise

    writers.record_release_note_completed(
        ctx.db, params.note_id, source_repo_id=repo["id"], source_path=path, url=url
    )
    log.info("release_note_done", repo=repo["full_name"])
    return {"status": "completed", "note_id": params.note_id}
