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

from reva.url_safety import assert_safe_url

from reva.errors import PermanentError, TransientError

logger = structlog.get_logger()

_TIMEOUT = 15.0


class OdooCallbackClient:
    def __init__(self, callback_url: str, api_key: str) -> None:
        # Empty callback_url = Odoo write-back disabled (see worker Settings).
        # Construct without validating so the worker boots without Odoo configured;
        # any method call then raises PermanentError instead of silently no-opping.
        self._enabled = bool(callback_url.strip())
        if not self._enabled:
            self._base_url = ""
            self._callback_url = ""
            self._api_key = api_key
            return
        # callback_url is the write-field endpoint; derive base for sibling endpoints
        if callback_url.endswith("/write-field"):
            self._base_url = callback_url[: -len("/write-field")]
        else:
            self._base_url = callback_url.rstrip("/")
        # Fail fast on a malformed/dangerous callback URL. Internal/RFC1918 hosts
        # are allowed (Odoo is often internal); only bad schemes + metadata are
        # rejected. See reva.url_safety.
        assert_safe_url(self._base_url + "/write-field")
        self._callback_url = self._base_url + "/write-field"
        self._api_key = api_key

    def _post(self, path: str, payload: dict) -> None:
        """POST to a REVA callback endpoint; raises TransientError or PermanentError."""
        if not self._enabled:
            raise PermanentError(
                "Odoo callback is disabled (ODOO_CALLBACK_URL is unset)"
            )
        url = self._base_url + path
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        try:
            resp = httpx.post(url, json=payload, headers=headers, timeout=_TIMEOUT)
        except httpx.TimeoutException as exc:
            raise TransientError(f"Odoo {path} timed out: {exc}") from exc
        except httpx.TransportError as exc:
            raise TransientError(f"Odoo {path} transport error: {exc}") from exc
        if resp.status_code in (200, 204):
            return
        body = resp.text[:300]
        if 400 <= resp.status_code < 500:
            raise PermanentError(f"Odoo {path} {resp.status_code} (permanent): {body}")
        raise TransientError(f"Odoo {path} {resp.status_code} (transient): {body}")

    def reset_status(self, ticket_id: int, model_name: str) -> None:
        """Set reva_status = pending in Odoo before re-running analysis."""
        self._post("/reset-status", {"ticket_id": ticket_id, "model_name": model_name})

    def write_field(
        self,
        ticket_id: int,
        model_name: str,
        field_name: str,
        html: str,
    ) -> None:
        """POST the analysis HTML to the Odoo callback endpoint."""
        self._post("/write-field", {
            "ticket_id": ticket_id,
            "model_name": model_name,
            "field_name": field_name,
            "html": html,
        })
        logger.bind(ticket_id=ticket_id, model_name=model_name).info("odoo_callback_ok")

    def issues_created(
        self,
        ticket_id: int,
        model_name: str,
        request_id: int,
        status: str,
        issues: list[dict],
        error: str | None = None,
    ) -> None:
        """POST the created GitHub issues (or a failure) to the Odoo callback.

        Contract 2 of the github-issues handoff: status is exactly "created"
        or "failed"; issues items are {"number", "title", "url"}; request_id
        must echo the id REVA returned from POST /api/v1/create-issues. Odoo
        responds 409 (permanent) when the record is no longer pending or the
        request_id is stale — the expected outcome of its 10s-timeout race.
        """
        self._post("/issues-created", {
            "ticket_id": ticket_id,
            "model_name": model_name,
            "request_id": request_id,
            "status": status,
            "issues": issues,
            "error": error,
        })
        logger.bind(ticket_id=ticket_id, model_name=model_name).info(
            "odoo_issues_created_ok"
        )

    def issue_state(
        self,
        ticket_id: int,
        model_name: str,
        number: int,
        state: str,
        issues: list[dict],
    ) -> None:
        """POST a per-issue state change (GitHub issue closed/reopened) to Odoo.

        `number`/`state` identify the change; `issues` is the FULL current
        snapshot [{"number", "title", "url", "state"}] so Odoo re-renders the
        links idempotently (done issues get marked). 409 = the record's links
        are not in the 'created' state — permanent, do not retry.
        """
        self._post("/issue-state", {
            "ticket_id": ticket_id,
            "model_name": model_name,
            "number": number,
            "state": state,
            "issues": issues,
        })
        logger.bind(ticket_id=ticket_id, model_name=model_name, number=number).info(
            "odoo_issue_state_ok"
        )
