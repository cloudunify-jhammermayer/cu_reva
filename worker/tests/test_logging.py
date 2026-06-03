"""Tests for the shared logging config (INFR-4)."""

from __future__ import annotations

import json

import structlog

import reva.logging as rl


def _reconfigure(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    rl._configured = False
    rl.configure_logging()


def test_configure_logging_emits_json_with_level_and_timestamp(capsys, monkeypatch):
    _reconfigure(monkeypatch, REVA_LOG_FORMAT="json", REVA_LOG_LEVEL="INFO")
    structlog.get_logger().info("hello_event", foo="bar")

    out = capsys.readouterr().out
    line = next(line for line in out.splitlines() if "hello_event" in line)
    rec = json.loads(line)  # must be valid JSON
    assert rec["event"] == "hello_event"
    assert rec["foo"] == "bar"
    assert rec["level"] == "info"
    assert "timestamp" in rec


def test_console_format_is_not_json(capsys, monkeypatch):
    _reconfigure(monkeypatch, REVA_LOG_FORMAT="console", REVA_LOG_LEVEL="INFO")
    structlog.get_logger().info("console_event")
    out = capsys.readouterr().out
    assert "console_event" in out
    line = next(line for line in out.splitlines() if "console_event" in line)
    with __import__("pytest").raises(json.JSONDecodeError):
        json.loads(line)


def test_idempotent(monkeypatch):
    _reconfigure(monkeypatch, REVA_LOG_FORMAT="json")
    import logging
    before = list(logging.getLogger().handlers)
    rl.configure_logging()  # second call is a no-op
    assert logging.getLogger().handlers == before
