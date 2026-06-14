from __future__ import annotations

from fastapi import APIRouter, Depends

from app.dependencies import get_db, get_redis
from app.queries import metrics as q
from app.schemas.metrics import (
    CostEntry,
    DashboardMetrics,
    DeveloperStat,
    FeedbackEntry,
    LearningStat,
    MuteEntry,
)
from reva.db.engine import Database

router = APIRouter()

_VALID_PERIODS = {"week", "month", "quarter"}


@router.get("/metrics/dashboard", response_model=DashboardMetrics)
def dashboard(
    db: Database = Depends(get_db),
    redis=Depends(get_redis),
) -> dict:
    return q.dashboard_metrics(db, redis)


@router.get("/metrics/developers", response_model=list[DeveloperStat])
def developers(period: str = "month", db: Database = Depends(get_db)) -> list[dict]:
    if period not in _VALID_PERIODS:
        period = "month"
    return [DeveloperStat.model_validate(r) for r in q.developer_stats(db, period)]


@router.get("/metrics/cost", response_model=list[CostEntry])
def cost(
    period: str = "month",
    repo: str | None = None,
    db: Database = Depends(get_db),
) -> list[dict]:
    if period not in _VALID_PERIODS:
        period = "month"
    return [CostEntry.model_validate(r) for r in q.cost_stats(db, period, repo)]


@router.get("/metrics/feedback", response_model=list[FeedbackEntry])
def feedback(db: Database = Depends(get_db)) -> list[dict]:
    return [FeedbackEntry.model_validate(r) for r in q.feedback_stats(db)]


@router.get("/metrics/learning", response_model=list[LearningStat])
def learning(db: Database = Depends(get_db)) -> list[dict]:
    return [LearningStat.model_validate(r) for r in q.learning_stats(db)]


@router.get("/metrics/mutes", response_model=list[MuteEntry])
def mutes(db: Database = Depends(get_db)) -> list[dict]:
    return [MuteEntry.model_validate(r) for r in q.active_mutes(db)]
