"""Reviewer scanner-feed wiring and secret severity floor."""

from __future__ import annotations

from reva.scanner_feed import ScannerEntry, ScannerFeed
from reva.types import Finding
from tests.test_reviewer import (
    FakeGitHub,
    FakeRunner,
    _claude_response_with_findings,
    _make_reviewer,
    _params,
)
from worker.reviewer import _floor_secret_findings


def _empty_response():
    return _claude_response_with_findings([])


def test_feed_entries_attach_scanner_alerts_param(monkeypatch):
    feed = ScannerFeed(entries=[
        ScannerEntry(
            tool="code-scanning",
            rule="py/sql-injection",
            severity="error",
            file="custom_addons/app.py",
            line=12,
            description="SQL reaches cursor",
        )
    ])
    monkeypatch.setattr("worker.reviewer.scanner_collect", lambda *args: feed)
    runner = FakeRunner(response=_empty_response())
    reviewer, _, _, runner, _ = _make_reviewer(runner=runner)

    result = reviewer.execute(_params())

    assert result.status == "completed"
    assert "scanner_alerts" in runner.last_params
    assert "code-scanning | py/sql-injection | error | custom_addons/app.py:12" in (
        runner.last_params["scanner_alerts"]
    )


def test_empty_feed_omits_param(monkeypatch):
    monkeypatch.setattr("worker.reviewer.scanner_collect", lambda *args: ScannerFeed())
    runner = FakeRunner(response=_empty_response())
    reviewer, _, _, runner, _ = _make_reviewer(runner=runner)

    reviewer.execute(_params())

    assert "scanner_alerts" not in runner.last_params


def test_repo_config_can_disable_scanner_feed(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "worker.reviewer.scanner_collect",
        lambda *args: calls.append(args) or ScannerFeed(),
    )
    github = FakeGitHub(file_contents={".claude-review.yml": "scanner_feed: false\n"})
    runner = FakeRunner(response=_empty_response())
    reviewer, _, _, runner, _ = _make_reviewer(github=github, runner=runner)

    reviewer.execute(_params())

    assert calls == []
    assert "scanner_alerts" not in runner.last_params


def test_collector_error_fails_open_and_records_ops_event(monkeypatch):
    ops_events = []

    def fail_collect(*args):
        raise RuntimeError("github down")

    monkeypatch.setattr("worker.reviewer.scanner_collect", fail_collect)
    runner = FakeRunner(response=_empty_response())
    reviewer, _, _, runner, _ = _make_reviewer(
        runner=runner,
        ops_recorder=lambda c, s, e, d: ops_events.append((c, s, e, d)),
    )

    result = reviewer.execute(_params())

    assert result.status == "completed"
    assert "scanner_alerts" not in runner.last_params
    assert ops_events == [
        ("scanner_feed", "warning", "collect_failed", {"repo": "acme/widgets"})
    ]


def test_unavailable_sources_record_one_ops_event(monkeypatch):
    ops_events = []
    feed = ScannerFeed(unavailable=["code-scanning", "secret-scanning"])
    monkeypatch.setattr("worker.reviewer.scanner_collect", lambda *args: feed)
    runner = FakeRunner(response=_empty_response())
    reviewer, _, _, _, _ = _make_reviewer(
        runner=runner,
        ops_recorder=lambda c, s, e, d: ops_events.append((c, s, e, d)),
    )

    reviewer.execute(_params())

    assert ops_events == [
        (
            "scanner_feed",
            "warning",
            "sources_unavailable",
            {
                "repo": "acme/widgets",
                "sources": ["code-scanning", "secret-scanning"],
            },
        )
    ]


def test_secret_floor_only_file_anchored_matches():
    finding = Finding(
        severity="minor",
        category="security",
        file="a.py",
        line_start=1,
        line_end=1,
        title="Leaked token",
        body="A token is present.",
        confidence=0.9,
        is_odoo_specific=False,
    )
    repo_wide = ScannerFeed(entries=[
        ScannerEntry(
            tool="secret-scanning",
            rule="Token",
            severity="critical",
            file="-",
            line=None,
            description="open secret",
        )
    ])
    _floor_secret_findings([finding], repo_wide)
    assert finding.severity == "minor"

    anchored = ScannerFeed(entries=[
        ScannerEntry(
            tool="secret-scanning",
            rule="Token",
            severity="critical",
            file="a.py",
            line=1,
            description="open secret",
        )
    ])
    _floor_secret_findings([finding], anchored)
    assert finding.severity == "critical"
