"""Collector: normalization, relevance filters, cap priority, formatting."""

from __future__ import annotations

from dataclasses import dataclass

from reva.scanner_feed import ScannerEntry, collect, format_param

_CODE_ALERT = {
    "rule": {"id": "py/sql-injection", "severity": "error", "description": "SQL"},
    "most_recent_instance": {
        "location": {"path": "custom_addons/x/models/a.py", "start_line": 40},
        "message": {"text": "user input reaches cr.execute"},
    },
}
_DEP_ALERT = {
    "security_advisory": {"summary": "requests DoS", "severity": "high"},
    "dependency": {
        "package": {"name": "requests"},
        "manifest_path": "worker/requirements.txt",
    },
}
_SECRET_ALERT = {
    "secret_type_display_name": "GitHub Personal Access Token",
    "number": 3,
}


@dataclass
class FakeGH:
    code: list | None = None
    dependabot: list | None = None
    secret: list | None = None
    # alert number -> locations list; "raise" simulates a lookup failure.
    secret_locations: dict | None = None
    location_calls: int = 0

    def list_code_scanning_alerts(self, token, owner, repo):
        return self.code

    def list_dependabot_alerts(self, token, owner, repo):
        return self.dependabot

    def list_secret_scanning_alerts(self, token, owner, repo):
        return self.secret

    def get_secret_alert_locations(self, token, owner, repo, alert_number):
        self.location_calls += 1
        value = (self.secret_locations or {}).get(alert_number)
        if value == "raise":
            raise RuntimeError("locations fetch failed")
        return value


def test_code_alert_filtered_to_changed_files():
    gh = FakeGH(code=[_CODE_ALERT], dependabot=[], secret=[])
    feed = collect(
        gh,
        "t",
        "o",
        "r",
        changed_files=["custom_addons/x/models/a.py"],
    )
    assert any(entry.tool == "code-scanning" and entry.line == 40 for entry in feed.entries)

    feed2 = collect(gh, "t", "o", "r", changed_files=["other.py"])
    assert not any(entry.tool == "code-scanning" for entry in feed2.entries)


def test_dependabot_only_when_manifest_touched():
    gh = FakeGH(code=[], dependabot=[_DEP_ALERT], secret=[])
    feed = collect(gh, "t", "o", "r", changed_files=["worker/requirements.txt"])
    assert any(entry.tool == "dependabot" for entry in feed.entries)

    feed2 = collect(gh, "t", "o", "r", changed_files=["models/a.py"])
    assert not any(entry.tool == "dependabot" for entry in feed2.entries)


def test_secret_alerts_always_included():
    gh = FakeGH(code=[], dependabot=[], secret=[_SECRET_ALERT])
    feed = collect(gh, "t", "o", "r", changed_files=["anything.py"])
    assert any(entry.tool == "secret-scanning" for entry in feed.entries)


def test_unavailable_sources_listed():
    gh = FakeGH(code=None, dependabot=[], secret=None)
    feed = collect(gh, "t", "o", "r", changed_files=[])
    assert set(feed.unavailable) == {"code-scanning", "secret-scanning"}


def test_cap_priority_secrets_first():
    secret_alerts = [
        {
            **_SECRET_ALERT,
            "number": i,
            "secret_type_display_name": f"Token {i}",
        }
        for i in range(5)
    ]
    code_alerts = [
        {
            **_CODE_ALERT,
            "rule": {"id": f"rule-{i}", "severity": "error"},
        }
        for i in range(30)
    ]
    gh = FakeGH(code=code_alerts, dependabot=[], secret=secret_alerts)

    feed = collect(gh, "t", "o", "r", changed_files=["custom_addons/x/models/a.py"])

    assert len(feed.entries) == 20
    assert all(entry.tool == "secret-scanning" for entry in feed.entries[:5])
    assert feed.omitted == 15


def test_format_param_shape_and_omission_contract():
    entry = ScannerEntry(
        tool="code-scanning",
        rule="py/sql-injection",
        severity="error",
        file="a.py",
        line=40,
        description="SQL injection",
    )
    feed = type("F", (), {"entries": [entry], "unavailable": [], "omitted": 0})()

    text = format_param(feed)

    assert "code-scanning | py/sql-injection | error | a.py:40" in text
    assert "hints" in text.lower()


# --- secret location enrichment (review finding #1) ------------------------------

_COMMIT_LOCATION = {
    "type": "commit",
    "details": {"path": "custom_addons/x/models/a.py", "start_line": 12},
}


def test_secret_alert_enriched_with_file_location():
    """The list endpoint has no locations — the per-alert fetch anchors the
    entry to a file so the critical-severity floor can fire."""
    gh = FakeGH(code=[], dependabot=[], secret=[_SECRET_ALERT],
                secret_locations={3: [_COMMIT_LOCATION]})
    feed = collect(gh, "t", "o", "r", changed_files=[])
    entry = next(e for e in feed.entries if e.tool == "secret-scanning")
    assert entry.file == "custom_addons/x/models/a.py"
    assert entry.line == 12


def test_secret_location_failure_degrades_to_repo_wide():
    gh = FakeGH(code=[], dependabot=[], secret=[_SECRET_ALERT],
                secret_locations={3: "raise"})
    feed = collect(gh, "t", "o", "r", changed_files=[])
    entry = next(e for e in feed.entries if e.tool == "secret-scanning")
    assert entry.file == "-" and entry.line is None


def test_non_commit_locations_stay_repo_wide():
    gh = FakeGH(code=[], dependabot=[], secret=[_SECRET_ALERT],
                secret_locations={3: [{"type": "issue_comment", "details": {}}]})
    feed = collect(gh, "t", "o", "r", changed_files=[])
    entry = next(e for e in feed.entries if e.tool == "secret-scanning")
    assert entry.file == "-"


def test_secret_location_lookups_capped():
    alerts = [dict(_SECRET_ALERT, number=n) for n in range(1, 10)]
    gh = FakeGH(code=[], dependabot=[], secret=alerts, secret_locations={})
    collect(gh, "t", "o", "r", changed_files=[])
    assert gh.location_calls == 5
