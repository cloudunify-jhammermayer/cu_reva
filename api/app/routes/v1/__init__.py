"""Aggregate all /api/v1 sub-routers into a single router."""

from __future__ import annotations

from fastapi import APIRouter

from app.routes.v1 import admin, failures, findings, metrics, pending, repos, reviews

router = APIRouter()
router.include_router(reviews.router)
router.include_router(findings.router)
router.include_router(repos.router)
router.include_router(failures.router)
router.include_router(metrics.router)
router.include_router(pending.router)
router.include_router(admin.router)
