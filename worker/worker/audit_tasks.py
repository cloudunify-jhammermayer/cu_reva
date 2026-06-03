"""RQ task entry point for repo audit jobs."""

from __future__ import annotations

import hashlib
from urllib.parse import quote

import structlog

from reva.db import writers
from reva.db.repo_lookup import get_repo_meta
from reva.errors import TransientError
from reva.review_formatter import SEVERITY_EMOJI
from reva.types import AuditJobParams, Finding
from worker.runner import budget_exceeded, get_context

logger = structlog.get_logger()

# Severities that warrant a GitHub issue. Lower ones are still persisted to the
# audit_findings table, just not surfaced as issues (would be noise).
_ISSUE_SEVERITIES = ("major", "critical")
# Label applied to every audit issue. REVA creates it per-repo if missing, so
# you can filter `label:reva-audit` without setting anything up by hand.
_AUDIT_ISSUE_LABEL = "reva-audit"


def _audit_finding_marker(owner: str, repo: str, f: Finding) -> str:
    """Stable, plain-alphanumeric token identifying a finding across audit runs.

    Embedded in the issue body so a re-run can detect (via search) that an issue
    already exists and skip it. Keyed on repo + file + title — the parts that
    identify "the same problem" run to run."""
    key = f"{owner}/{repo}\x00{f.file or ''}\x00{f.title}"
    return "revaaudit" + hashlib.sha1(key.encode()).hexdigest()[:16]


def _audit_issue_location(f: Finding, owner: str, repo: str, branch: str) -> str:
    """A clickable location: links the file (at the default branch) with a line
    anchor when we have a path; 'repository-wide' otherwise."""
    if not f.file:
        return "repository-wide"
    label = f"{f.file}:{f.line_start}" if f.line_start else f.file
    url = f"https://github.com/{owner}/{repo}/blob/{branch}/{quote(f.file, safe='/')}"
    if f.line_start:
        url += f"#L{f.line_start}"
        if f.line_end and f.line_end != f.line_start:
            url += f"-L{f.line_end}"
    return f"[`{label}`]({url})"


def _format_audit_issue_body(
    f: Finding, marker: str, owner: str, repo: str, branch: str, audit_id: int
) -> str:
    """Render an audit finding as a readable issue body, with a hidden dedup marker."""
    emoji = SEVERITY_EMOJI.get(f.severity, "")
    conf = f"{round(f.confidence * 100)}%" if f.confidence is not None else "—"

    lines = [
        f"> {emoji} **{f.severity.capitalize()}** · `{f.category}` · confidence {conf}",
        "",
        f"**📍 Location:** {_audit_issue_location(f, owner, repo, branch)}",
        "",
        "### Description",
        "",
        f.body,
    ]
    if f.suggestion:
        lines += ["", "### Suggested fix", "", f.suggestion]
    lines += [
        "",
        "---",
        f"<sub>🤖 Opened by the REVA repo audit (run #{audit_id}). "
        "Findings below `major` are tracked by REVA but not opened as issues.</sub>",
        f"<!-- {marker} -->",
    ]
    return "\n".join(lines)


def _open_audit_issues(ctx, params: AuditJobParams, audit_id: int, findings: list[Finding],
                       finding_ids: list[int], log) -> None:
    """Open a GitHub issue per major/critical finding (deduped). Best-effort:
    the audit already succeeded and findings are persisted, so a GitHub failure
    is logged per finding and never fails the job."""
    targets = [
        (fid, f) for fid, f in zip(finding_ids, findings) if f.severity in _ISSUE_SEVERITIES
    ]
    if not targets:
        return
    meta = get_repo_meta(ctx.db, params.repository_id)
    owner, name = meta["owner"], meta["name"]
    branch = meta["default_branch"] or "HEAD"
    token = ctx.github.get_installation_token(params.installation_id)
    ctx.github.ensure_label(
        token, owner, name, _AUDIT_ISSUE_LABEL, description="REVA repo-audit findings"
    )
    for fid, f in targets:
        marker = _audit_finding_marker(owner, name, f)
        try:
            if ctx.github.issue_exists_with_marker(token, owner, name, marker):
                log.info("audit_issue_skipped_duplicate", finding_id=fid, title=f.title)
                continue
            number = ctx.github.create_issue(
                token, owner, name,
                title=f"[REVA audit] {f.title}",
                body=_format_audit_issue_body(f, marker, owner, name, branch, audit_id),
                labels=[_AUDIT_ISSUE_LABEL],
            )
            writers.set_audit_finding_issue_number(ctx.db, fid, number)
            log.info("audit_issue_created", finding_id=fid, issue=number)
        except Exception as exc:  # best-effort; the finding is already persisted
            log.warning("audit_issue_failed", finding_id=fid, error=str(exc))


def run_audit(job_params: dict) -> dict:
    """RQ task: run a full repo audit and persist the result."""
    from reva.db.models import AuditRun
    from sqlalchemy import insert, update

    ctx = get_context()
    params = AuditJobParams.model_validate(job_params)
    log = logger.bind(repository_id=params.repository_id)
    log.info("audit_job_start")

    # SECU-4: the audit is the most expensive Claude path — respect the rolling
    # cap. Decline a NEW audit when over budget (no row created); in-flight
    # audits are never interrupted.
    spent = budget_exceeded(ctx)
    if spent is not None:
        log.warning("audit_over_budget", spent_usd=round(spent, 2),
                    budget_usd=ctx.daily_budget_usd)
        return {"audit_id": None, "status": "declined", "reason": "over_budget"}

    with ctx.db.session() as s:
        result_row = s.execute(
            insert(AuditRun).values(
                repository_id=params.repository_id,
                status="started",
                requested_by=params.requested_by,
            ).returning(AuditRun.id)
        )
        audit_id = result_row.scalar_one()
        s.commit()

    try:
        result = ctx.auditor.execute(params)
    except Exception as exc:
        # Audits are not RQ-retried, so any failure (transient or permanent) is
        # terminal — mark the row failed instead of leaving it stuck in 'started'
        # forever (CORR-12). The raw message stays internal (admin-only path).
        with ctx.db.session() as s:
            s.execute(
                update(AuditRun)
                .where(AuditRun.id == audit_id)
                .values(status="failed", error_message=str(exc)[:500])
            )
            s.commit()
        log.error("audit_job_failed", error=str(exc),
                  transient=isinstance(exc, TransientError))
        raise

    # Persist every finding (audits used to discard all but the count), then
    # open GitHub issues for the major/critical ones.
    finding_ids = writers.insert_audit_findings(ctx.db, audit_id, result.findings)
    _open_audit_issues(ctx, params, audit_id, result.findings, finding_ids, log)

    with ctx.db.session() as s:
        s.execute(
            update(AuditRun)
            .where(AuditRun.id == audit_id)
            .values(
                status="completed",
                summary=result.summary,
                model=result.model,
                started_at=result.started_at,
                completed_at=result.completed_at,
                duration_ms=result.duration_ms,
                finding_count=len(result.findings),
            )
        )
        s.commit()

    # CORR-11/SECU-4: record audit spend in the unified ledger so the cap counts it.
    writers.record_claude_spend(ctx.db, "audit", result.estimated_cost_usd)

    log.info("audit_job_done", findings=len(result.findings),
             cost_usd=result.estimated_cost_usd)
    return {"audit_id": audit_id, "status": "completed", "findings": len(result.findings)}
