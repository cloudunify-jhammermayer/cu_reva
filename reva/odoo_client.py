"""Odoo callback client.

Posts analysis results to the custom FastAPI endpoint that CloudUnify
builds on the Odoo side. REVA defines the request contract; the Odoo
endpoint is expected to match it.

Contract:
    POST {callback_url}
    Authorization: Bearer {api_key}
    Content-Type: application/json

    {
        "ticket_id": 123,
        "model_name": "helpdesk.ticket",
        "field_name": "description",
        "html": "<h2>...</h2>"
    }

    Response 200: {"ok": true}

Error mapping:
    4xx  → PermanentError  (bad request / auth — do not retry)
    5xx  → TransientError  (server error — RQ retries)
    network → TransientError
"""

from __future__ import annotations

import structlog

import httpx

from reva.errors import PermanentError, TransientError

logger = structlog.get_logger()

_TIMEOUT = 15.0


class OdooCallbackClient:
    def __init__(self, callback_url: str, api_key: str) -> None:
        self._callback_url = callback_url
        self._api_key = api_key

    def write_field(
        self,
        ticket_id: int,
        model_name: str,
        field_name: str,
        html: str,
    ) -> None:
        """POST the analysis HTML to the Odoo callback endpoint."""
        payload = {
            "ticket_id": ticket_id,
            "model_name": model_name,
            "field_name": field_name,
            "html": html,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        log = logger.bind(ticket_id=ticket_id, model_name=model_name, field_name=field_name)

        try:
            resp = httpx.post(
                self._callback_url,
                json=payload,
                headers=headers,
                timeout=_TIMEOUT,
            )
        except httpx.TimeoutException as exc:
            raise TransientError(f"Odoo callback timed out: {exc}") from exc
        except httpx.TransportError as exc:
            raise TransientError(f"Odoo callback transport error: {exc}") from exc

        if resp.status_code == 200:
            log.info("odoo_callback_ok")
            return

        body = resp.text[:300]
        if 400 <= resp.status_code < 500:
            raise PermanentError(
                f"Odoo callback {resp.status_code} (permanent): {body}"
            )
        raise TransientError(
            f"Odoo callback {resp.status_code} (transient): {body}"
        )
