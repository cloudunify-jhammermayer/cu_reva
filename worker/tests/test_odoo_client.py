"""Tests for OdooCallbackClient.write_field.

Patches reva.odoo_client.httpx.post via monkeypatch — no live network calls.
"""

from __future__ import annotations

import json

import httpx
import pytest

from reva.errors import PermanentError, TransientError
from reva.odoo_client import OdooCallbackClient

_URL = "https://odoo.example.com/api/reva/write-field"
_KEY = "test-api-key-secret"


def _client() -> OdooCallbackClient:
    return OdooCallbackClient(callback_url=_URL, api_key=_KEY)


def _kwargs() -> dict:
    return {
        "ticket_id": 123,
        "model_name": "helpdesk.ticket",
        "field_name": "description",
        "html": "<h2>Summary</h2><p>test</p>",
    }


def _mock_post(status: int = 200, body: str = '{"ok":true}', exc=None):
    """Return a callable that replaces httpx.post."""
    def post(url, **kwargs):
        if exc is not None:
            raise exc
        return httpx.Response(status, text=body)
    return post


def test_empty_callback_url_constructs_disabled():
    # Empty URL = Odoo callback disabled (settings contract). Constructing must
    # NOT validate or raise — the worker boots without Odoo configured.
    OdooCallbackClient(callback_url="", api_key="")


def test_disabled_client_write_field_raises_permanent():
    client = OdooCallbackClient(callback_url="", api_key="")
    with pytest.raises(PermanentError):
        client.write_field(**_kwargs())


def test_disabled_client_reset_status_raises_permanent():
    client = OdooCallbackClient(callback_url="", api_key="")
    with pytest.raises(PermanentError):
        client.reset_status(ticket_id=123, model_name="helpdesk.ticket")


def test_rejects_cloud_metadata_callback_url():
    with pytest.raises(ValueError):
        OdooCallbackClient(callback_url="http://169.254.169.254/write-field", api_key=_KEY)


def test_allows_internal_callback_url():
    # Odoo is commonly on an internal network — must be permitted.
    OdooCallbackClient(callback_url="http://10.0.0.9:8069/api/reva/write-field", api_key=_KEY)


def test_write_field_success(monkeypatch):
    monkeypatch.setattr("reva.odoo_client.httpx.post", _mock_post(200))
    _client().write_field(**_kwargs())  # no exception


def test_write_field_4xx_is_permanent(monkeypatch):
    for status in (400, 401, 403, 404, 422):
        monkeypatch.setattr("reva.odoo_client.httpx.post", _mock_post(status, "bad request"))
        with pytest.raises(PermanentError):
            _client().write_field(**_kwargs())


def test_write_field_5xx_is_transient(monkeypatch):
    monkeypatch.setattr("reva.odoo_client.httpx.post", _mock_post(500, "server error"))
    with pytest.raises(TransientError):
        _client().write_field(**_kwargs())


def test_write_field_timeout_is_transient(monkeypatch):
    exc = httpx.TimeoutException("timed out")
    monkeypatch.setattr("reva.odoo_client.httpx.post", _mock_post(exc=exc))
    with pytest.raises(TransientError):
        _client().write_field(**_kwargs())


def test_write_field_transport_error_is_transient(monkeypatch):
    exc = httpx.TransportError("connection refused")
    monkeypatch.setattr("reva.odoo_client.httpx.post", _mock_post(exc=exc))
    with pytest.raises(TransientError):
        _client().write_field(**_kwargs())


def test_write_field_sends_bearer_token(monkeypatch):
    captured: dict = {}

    def post(url, *, headers, **kwargs):
        captured["auth"] = headers.get("Authorization", "")
        return httpx.Response(200, text='{"ok":true}')

    monkeypatch.setattr("reva.odoo_client.httpx.post", post)
    _client().write_field(**_kwargs())
    assert captured["auth"] == f"Bearer {_KEY}"


def test_write_field_payload_shape(monkeypatch):
    captured: dict = {}

    def post(url, *, json, **kwargs):
        captured["body"] = json
        return httpx.Response(200, text='{"ok":true}')

    monkeypatch.setattr("reva.odoo_client.httpx.post", post)
    kw = _kwargs()
    _client().write_field(**kw)
    assert captured["body"]["ticket_id"] == kw["ticket_id"]
    assert captured["body"]["model_name"] == kw["model_name"]
    assert captured["body"]["field_name"] == kw["field_name"]
    assert captured["body"]["html"] == kw["html"]
