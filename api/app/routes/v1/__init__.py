"""Aggregate all /api/v1 sub-routers, split by auth gate.

- master gate (require_api_key): every admin/read/management route, incl. the
  ticket list handlers and the odoo-instances CRUD.
- instance gate (require_odoo_instance): ONLY the Odoo create routes.
- shared gate (require_master_or_odoo_instance): the per-run ticket GET/requeue
  routes — Odoo's self-heal polls them with its instance key (scoped to its own
  rows), ops/TUI with the master key (unscoped).
- any-key (own check): GET /health — the credentialed connection test.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.dependencies import require_api_key, require_odoo_instance
from app.ratelimit import rate_limit
from app.routes.v1 import (
    admin,
    audits,
    failures,
    findings,
    health,
    metrics,
    odoo_instances,
    ops_events,
    pending,
    personas,
    release_notes,
    repos,
    reviews,
    support_requests,
    ticket_actuals,
    ticket_analyses,
    ticket_issues,
    ticket_journeys,
    timesheet_reviews,
    value_reports,
)

router = APIRouter()

_master = APIRouter(dependencies=[Depends(require_api_key), Depends(rate_limit)])
_master.include_router(reviews.router)
_master.include_router(findings.router)
_master.include_router(repos.router)
_master.include_router(failures.router)
_master.include_router(metrics.router)
_master.include_router(pending.router)
_master.include_router(admin.router)
_master.include_router(ticket_analyses.router)
_master.include_router(ticket_issues.router)
_master.include_router(ticket_journeys.router)
_master.include_router(timesheet_reviews.router)
_master.include_router(release_notes.router)
_master.include_router(value_reports.router)
_master.include_router(audits.router)
_master.include_router(odoo_instances.router)
_master.include_router(ops_events.router)
_master.include_router(support_requests.router)
_master.include_router(personas.router)

_instance = APIRouter(dependencies=[Depends(require_odoo_instance), Depends(rate_limit)])
_instance.include_router(ticket_actuals.create_router)
_instance.include_router(ticket_analyses.create_router)
_instance.include_router(ticket_issues.create_router)
_instance.include_router(support_requests.create_router)
_instance.include_router(timesheet_reviews.create_router)
_instance.include_router(release_notes.create_router)

# Connection test: accepts master OR instance key, so it sits outside both
# gates and does its own credential check (see routes/v1/health.py).
_any = APIRouter(dependencies=[Depends(rate_limit)])
_any.include_router(health.router)

# Auth lives in each handler's require_master_or_odoo_instance dependency (the
# handlers need the resolved instance for row scoping); only rate limit here.
_shared = APIRouter(dependencies=[Depends(rate_limit)])
_shared.include_router(ticket_analyses.shared_router)
_shared.include_router(ticket_issues.shared_router)
_shared.include_router(support_requests.shared_router)

router.include_router(_master)
router.include_router(_instance)
router.include_router(_shared)
router.include_router(_any)
