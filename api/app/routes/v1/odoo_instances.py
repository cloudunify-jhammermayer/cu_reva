"""Admin (master-key) CRUD for Odoo instances.

Mints a per-instance inbound key (stored hashed, returned once), and encrypts
the outbound callback key at rest under REVA_SECRET_KEY.
"""

from __future__ import annotations

import hashlib
import secrets

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request

from app.dependencies import actor_from_request, get_db
from app.queries import odoo_instances as q
from app.schemas.odoo_instances import (
    OdooInstanceCost,
    OdooInstanceCreate,
    OdooInstanceCreated,
    OdooInstancePage,
    OdooInstanceSummary,
    OdooInstanceUpdate,
)
from reva import secrets_crypto
from reva.db import writers
from reva.db.engine import Database

router = APIRouter()
logger = structlog.get_logger()


def mint_inbound_key() -> tuple[str, str, str]:
    """Return (plaintext, sha256-hex, display-prefix) for a fresh inbound key."""
    plaintext = "reva_odoo_" + secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(plaintext.encode()).hexdigest()
    return plaintext, key_hash, plaintext[:16]


def _seal_outbound(plaintext: str) -> str:
    try:
        return secrets_crypto.encrypt(plaintext)
    except RuntimeError as exc:  # REVA_SECRET_KEY missing
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/odoo-instances", response_model=OdooInstancePage)
def list_instances(db: Database = Depends(get_db)) -> dict:
    items = q.list_odoo_instances(db)
    return {
        "items": [OdooInstanceSummary.model_validate(i) for i in items],
        "total": len(items),
    }


@router.post("/odoo-instances", status_code=201, response_model=OdooInstanceCreated)
def create_instance(
    body: OdooInstanceCreate, request: Request, db: Database = Depends(get_db)
) -> dict:
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="name is required")
    callback_api_key_enc = _seal_outbound(body.callback_api_key)
    plaintext, key_hash, key_prefix = mint_inbound_key()
    instance_id = writers.create_odoo_instance(
        db, name=name, key_hash=key_hash, key_prefix=key_prefix,
        callback_url=body.callback_url.strip(),
        callback_api_key_enc=callback_api_key_enc,
    )
    writers.record_admin_action(
        db, action="create_odoo_instance", actor=actor_from_request(request),
        target=name, detail={"instance_id": instance_id},
    )
    return {"id": instance_id, "name": name, "key_prefix": key_prefix, "api_key": plaintext}


@router.get("/odoo-instances/{instance_id}/cost", response_model=OdooInstanceCost)
def instance_cost(instance_id: int, db: Database = Depends(get_db)) -> dict:
    if writers.get_odoo_instance(db, instance_id) is None:
        raise HTTPException(status_code=404, detail="Odoo instance not found")
    return q.get_odoo_instance_cost(db, instance_id)


@router.post(
    "/odoo-instances/{instance_id}/rotate-key", response_model=OdooInstanceCreated
)
def rotate_key(
    instance_id: int, request: Request, db: Database = Depends(get_db)
) -> dict:
    plaintext, key_hash, key_prefix = mint_inbound_key()
    if not writers.rotate_odoo_instance_key(
        db, instance_id, key_hash=key_hash, key_prefix=key_prefix
    ):
        raise HTTPException(status_code=404, detail="Odoo instance not found")
    row = writers.get_odoo_instance(db, instance_id)
    writers.record_admin_action(
        db, action="rotate_odoo_instance_key", actor=actor_from_request(request),
        target=row["name"], detail={"instance_id": instance_id},
    )
    return {"id": instance_id, "name": row["name"], "key_prefix": key_prefix, "api_key": plaintext}


@router.patch("/odoo-instances/{instance_id}", status_code=200)
def update_instance(
    instance_id: int, body: OdooInstanceUpdate, request: Request,
    db: Database = Depends(get_db),
) -> dict:
    fields: dict[str, object] = {}
    if body.name is not None:
        fields["name"] = body.name.strip()
    if body.callback_url is not None:
        fields["callback_url"] = body.callback_url.strip()
    if body.callback_api_key is not None:
        fields["callback_api_key_enc"] = _seal_outbound(body.callback_api_key)
    if body.active is not None:
        fields["active"] = body.active
    if "daily_budget_usd" in body.model_fields_set:
        fields["daily_budget_usd"] = body.daily_budget_usd
    if "rate_limit_per_minute" in body.model_fields_set:
        fields["rate_limit_per_minute"] = body.rate_limit_per_minute
    if not fields:
        raise HTTPException(status_code=422, detail="no fields to update")
    if not writers.update_odoo_instance(db, instance_id, **fields):
        raise HTTPException(status_code=404, detail="Odoo instance not found")
    writers.record_admin_action(
        db, action="update_odoo_instance", actor=actor_from_request(request),
        target=str(instance_id), detail={k: v for k, v in fields.items() if k != "callback_api_key_enc"},
    )
    return {"id": instance_id, "updated": True}
