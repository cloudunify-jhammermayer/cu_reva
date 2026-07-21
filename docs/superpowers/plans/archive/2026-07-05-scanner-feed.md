# Scanner Feed Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Feed the reviewed repo's open GitHub security alerts (code-scanning, Dependabot, secret-scanning) into every review as fenced context, with a deterministic `critical` floor for findings matching secret alerts.

**Architecture:** Three thin readers on `GitHubClient` (one page each, fail-open per source) → pure `reva/scanner_feed.py` collector (normalize, filter to relevance, cap 20 by priority secrets > code-scanning > dependabot) → optional `scanner_alerts` skill param (the `manifest_audit` pattern) → post-hoc secret floor. CI-artifact ingestion was explicitly dropped (spec).

**Tech Stack:** Python 3.14, existing httpx `GitHubClient`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-05-scanner-feed-design.md`.

## Global Constraints

- Fail-open everywhere: a source that 403s/404s (no GHAS, missing App permission, feature off) is skipped and listed as unavailable; one ops event per run (`component="scanner_feed"`, severity `warning`); the review NEVER fails because of the feed.
- **Operator prerequisite (documented, not code):** GitHub App permission bump — code scanning + Dependabot alerts + secret scanning, all read. Until granted, the feature is a visible no-op (ops events).
- Cap 20 entries, priority secrets > code-scanning > dependabot; param omitted when empty (prompt-cache stability).
- **Prompt CHANGELOG:** the guidance addition trips the drift guard — coordinate with the triage plan (whichever lands second takes the next version).
- Final gate: `make test` + `ruff check reva worker/worker api/app scheduler/scheduler`.

---

### Task 1: GitHub alert readers

**Files:**
- Modify: `reva/github_client.py` (three methods next to `get_issue`, using the shared `_get` helper at ~line 704)
- Test: `worker/tests/test_github_scanner_readers.py`

**Interfaces:**
- Produces (each returns `None` when the source is unavailable — 403/404 —
  and raises Transient/Permanent only for real API failures, matching `_get`'s
  existing mapping):
  - `list_code_scanning_alerts(token, owner, repo) -> list[dict] | None`
  - `list_dependabot_alerts(token, owner, repo) -> list[dict] | None`
  - `list_secret_scanning_alerts(token, owner, repo) -> list[dict] | None`

- [x] **Step 1: Write the failing tests**

Create `worker/tests/test_github_scanner_readers.py` (construction: reuse the
fixture pattern in `worker/tests/test_github_client.py` if it builds a client
against a mock transport — `grep -n "MockTransport\|def client" worker/tests/test_github_client.py | head`;
otherwise the `__new__` pattern below works regardless of the constructor):

```python
"""Security-alert readers: one page, unavailable → None, fail-open mapping."""

from __future__ import annotations

import httpx
import pytest

from reva.github_client import GitHubClient


def _client(handler) -> GitHubClient:
    gh = GitHubClient.__new__(GitHubClient)
    gh.base_url = "https://api.github.com"
    gh._client = httpx.Client(transport=httpx.MockTransport(handler))
    return gh


def _alerts_handler(path_suffix, payload, status=200):
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(path_suffix)
        assert request.url.params["state"] == "open"
        assert request.headers["Authorization"] == "Bearer tok"
        return httpx.Response(status, json=payload)
    return handle


def test_code_scanning_alerts_happy():
    payload = [{"rule": {"id": "py/sql-injection", "severity": "error"},
                "most_recent_instance": {
                    "location": {"path": "custom_addons/x/models/a.py",
                                 "start_line": 40},
                    "message": {"text": "SQL injection"}}}]
    gh = _client(_alerts_handler("/code-scanning/alerts", payload))
    alerts = gh.list_code_scanning_alerts("tok", "acme", "widgets")
    assert alerts[0]["rule"]["id"] == "py/sql-injection"


def test_unavailable_is_none_not_error():
    for status in (403, 404):
        gh = _client(lambda req, s=status: httpx.Response(s, json={"message": "nope"}))
        assert gh.list_code_scanning_alerts("tok", "a", "r") is None
        assert gh.list_dependabot_alerts("tok", "a", "r") is None
        assert gh.list_secret_scanning_alerts("tok", "a", "r") is None


def test_dependabot_and_secret_paths():
    gh = _client(_alerts_handler("/dependabot/alerts", []))
    assert gh.list_dependabot_alerts("tok", "acme", "widgets") == []
    gh2 = _client(_alerts_handler("/secret-scanning/alerts", []))
    assert gh2.list_secret_scanning_alerts("tok", "acme", "widgets") == []


def test_server_error_still_raises_transient():
    from reva.errors import TransientError

    gh = _client(lambda req: httpx.Response(502, text="bad gateway"))
    with pytest.raises(TransientError):
        gh.list_code_scanning_alerts("tok", "a", "r")
```

(If `_get`'s ≥300 handling maps 502 differently, mirror the client's actual
error mapping in that last test — the contract is: 403/404 → `None`,
5xx → the client's transient error, other 4xx → its permanent error.)

- [x] **Step 2: Run to verify failure, then implement**

Add to `reva/github_client.py` (after `get_issue`):

```python
    # --- security alerts (scanner-feed spec) --------------------------------

    def _list_alerts(self, token: str, path: str) -> list[dict] | None:
        """One page of open alerts, or None when the feature/permission is
        absent (403/404) — the scanner feed is fail-open by design."""
        try:
            response = self._get(token, path, params={"state": "open",
                                                      "per_page": 100},
                                 allow_404=True)
        except NotFound:
            return None
        if response.status_code == 403:
            return None
        return response.json()

    def list_code_scanning_alerts(self, token: str, owner: str, repo: str) -> list[dict] | None:
        return self._list_alerts(token, f"/repos/{owner}/{repo}/code-scanning/alerts")

    def list_dependabot_alerts(self, token: str, owner: str, repo: str) -> list[dict] | None:
        return self._list_alerts(token, f"/repos/{owner}/{repo}/dependabot/alerts")

    def list_secret_scanning_alerts(self, token: str, owner: str, repo: str) -> list[dict] | None:
        return self._list_alerts(token, f"/repos/{owner}/{repo}/secret-scanning/alerts")
```

**Adapter note:** `_get` raises on `>= 300` (line ~729) — check whether a 403
reaches the `response.status_code == 403` line or raises first. If `_get`
raises its permanent error for 403 before returning, catch that error type in
`_list_alerts` instead (`except PermanentError: return None` is too broad —
inspect `_get`'s raise site and catch precisely, e.g. match on status by
passing a new `allow_statuses=(403,)` parameter to `_get`, mirroring
`allow_404`). The tests define the contract.

- [x] **Step 3: Run to verify pass, commit**

```bash
cd worker && .venv/bin/python -m pytest tests/test_github_scanner_readers.py tests/test_github_client.py -q
git add reva/github_client.py worker/tests/test_github_scanner_readers.py
git commit -m "feat(github): open security-alert readers (fail-open)"
```

---

### Task 2: Collector + formatter (`reva/scanner_feed.py`)

**Files:**
- Create: `reva/scanner_feed.py`
- Test: `worker/tests/test_scanner_feed.py`

**Interfaces:**
- Produces: `ScannerEntry(tool, rule, severity, file, line, description)`;
  `ScannerFeed(entries: list[ScannerEntry], unavailable: list[str])`;
  `collect(github, token, owner, repo, changed_files: list[str]) -> ScannerFeed`;
  `format_param(feed) -> str`; `MANIFEST_PATTERNS` (dependency-file matcher).

- [x] **Step 1: Write the failing tests**

Create `worker/tests/test_scanner_feed.py`:

```python
"""Collector: normalization, relevance filters, cap priority, formatting."""

from __future__ import annotations

from dataclasses import dataclass

from reva.scanner_feed import ScannerEntry, collect, format_param

_CODE_ALERT = {"rule": {"id": "py/sql-injection", "severity": "error",
                        "description": "SQL injection"},
               "most_recent_instance": {
                   "location": {"path": "custom_addons/x/models/a.py",
                                "start_line": 40},
                   "message": {"text": "user input reaches cr.execute"}}}
_DEP_ALERT = {"security_advisory": {"summary": "requests DoS",
                                    "severity": "high"},
              "dependency": {"package": {"name": "requests"},
                             "manifest_path": "worker/requirements.txt"}}
_SECRET_ALERT = {"secret_type_display_name": "GitHub Personal Access Token",
                 "locations_url": "…", "number": 3}


@dataclass
class FakeGH:
    code: list | None = None
    dep: list | None = None
    secret: list | None = None

    def list_code_scanning_alerts(self, t, o, r):
        return self.code

    def list_dependabot_alerts(self, t, o, r):
        return self.dep

    def list_secret_scanning_alerts(self, t, o, r):
        return self.secret


def test_code_alert_filtered_to_changed_files():
    gh = FakeGH(code=[_CODE_ALERT], dep=[], secret=[])
    feed = collect(gh, "t", "o", "r",
                   changed_files=["custom_addons/x/models/a.py"])
    assert any(e.tool == "code-scanning" and e.line == 40 for e in feed.entries)
    feed2 = collect(gh, "t", "o", "r", changed_files=["other.py"])
    assert not any(e.tool == "code-scanning" for e in feed2.entries)


def test_dependabot_only_when_manifest_touched():
    gh = FakeGH(code=[], dep=[_DEP_ALERT], secret=[])
    feed = collect(gh, "t", "o", "r", changed_files=["worker/requirements.txt"])
    assert any(e.tool == "dependabot" for e in feed.entries)
    feed2 = collect(gh, "t", "o", "r", changed_files=["models/a.py"])
    assert not any(e.tool == "dependabot" for e in feed2.entries)


def test_secret_alerts_always_included():
    gh = FakeGH(code=[], dep=[], secret=[_SECRET_ALERT])
    feed = collect(gh, "t", "o", "r", changed_files=["anything.py"])
    assert any(e.tool == "secret-scanning" for e in feed.entries)


def test_unavailable_sources_listed():
    gh = FakeGH(code=None, dep=[], secret=None)
    feed = collect(gh, "t", "o", "r", changed_files=[])
    assert set(feed.unavailable) == {"code-scanning", "secret-scanning"}


def test_cap_priority_secrets_first():
    gh = FakeGH(code=[_CODE_ALERT] * 30, dep=[], secret=[_SECRET_ALERT] * 5)
    feed = collect(gh, "t", "o", "r",
                   changed_files=["custom_addons/x/models/a.py"])
    assert len(feed.entries) == 20
    assert all(e.tool == "secret-scanning" for e in feed.entries[:5])


def test_format_param_shape_and_omission_contract():
    entry = ScannerEntry(tool="code-scanning", rule="py/sql-injection",
                         severity="error", file="a.py", line=40,
                         description="SQL injection")
    text = format_param(type("F", (), {"entries": [entry], "unavailable": [],
                                       "omitted": 0})())
    assert "code-scanning | py/sql-injection | error | a.py:40" in text
    assert "hints" in text.lower()  # verdict-don't-copy framing present
```

- [x] **Step 2: Run to verify failure, then implement `reva/scanner_feed.py`**

```python
"""GitHub security alerts as review context (scanner-feed spec).

Pure collector: normalizes the three alert payload shapes, filters to
relevance, caps by priority (secrets > code-scanning > dependabot). The
reviewer attaches format_param()'s output as the optional `scanner_alerts`
skill param — hints the model must verdict, never findings to copy.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field

_CAP = 20
MANIFEST_PATTERNS = ("*requirements*.txt", "*pyproject.toml", "*package.json",
                     "*package-lock.json", "*__manifest__.py", "*Pipfile*",
                     "*poetry.lock")


@dataclass(frozen=True)
class ScannerEntry:
    tool: str          # secret-scanning | code-scanning | dependabot
    rule: str
    severity: str
    file: str          # "-" when not file-anchored
    line: int | None
    description: str


@dataclass
class ScannerFeed:
    entries: list[ScannerEntry] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    omitted: int = 0


def _manifest_touched(changed_files: list[str]) -> bool:
    return any(fnmatch.fnmatch(f, p) for f in changed_files for p in MANIFEST_PATTERNS)


def _norm_code(alerts: list[dict], changed: set[str]) -> list[ScannerEntry]:
    out = []
    for a in alerts:
        loc = (a.get("most_recent_instance") or {}).get("location") or {}
        path = loc.get("path")
        if not path or path not in changed:
            continue  # code alerts are only actionable on the PR's files
        rule = a.get("rule") or {}
        msg = ((a.get("most_recent_instance") or {}).get("message") or {})
        out.append(ScannerEntry(
            tool="code-scanning", rule=str(rule.get("id", "?")),
            severity=str(rule.get("severity", "unknown")),
            file=path, line=loc.get("start_line"),
            description=(msg.get("text") or rule.get("description") or "")[:200],
        ))
    return out


def _norm_dependabot(alerts: list[dict]) -> list[ScannerEntry]:
    out = []
    for a in alerts:
        adv = a.get("security_advisory") or {}
        dep = a.get("dependency") or {}
        pkg = (dep.get("package") or {}).get("name", "?")
        out.append(ScannerEntry(
            tool="dependabot", rule=pkg,
            severity=str(adv.get("severity", "unknown")),
            file=str(dep.get("manifest_path") or "-"), line=None,
            description=str(adv.get("summary", ""))[:200],
        ))
    return out


def _norm_secret(alerts: list[dict]) -> list[ScannerEntry]:
    return [ScannerEntry(
        tool="secret-scanning",
        rule=str(a.get("secret_type_display_name")
                 or a.get("secret_type") or "secret"),
        severity="critical", file="-", line=None,
        description=f"open secret-scanning alert #{a.get('number', '?')}",
    ) for a in alerts]


def collect(github, token: str, owner: str, repo: str,
            changed_files: list[str]) -> ScannerFeed:
    """Fetch + normalize + filter + cap. Reader None => source unavailable."""
    feed = ScannerFeed()
    changed = set(changed_files)

    secret = github.list_secret_scanning_alerts(token, owner, repo)
    if secret is None:
        feed.unavailable.append("secret-scanning")
    code = github.list_code_scanning_alerts(token, owner, repo)
    if code is None:
        feed.unavailable.append("code-scanning")
    dep = github.list_dependabot_alerts(token, owner, repo)
    if dep is None:
        feed.unavailable.append("dependabot")

    prioritized = _norm_secret(secret or [])
    prioritized += _norm_code(code or [], changed)
    if _manifest_touched(changed_files):
        prioritized += _norm_dependabot(dep or [])

    # Dedup on (tool, rule, file, line), keep first (priority order).
    seen: set[tuple] = set()
    unique = []
    for e in prioritized:
        key = (e.tool, e.rule, e.file, e.line)
        if key not in seen:
            seen.add(key)
            unique.append(e)
    feed.entries = unique[:_CAP]
    feed.omitted = max(0, len(unique) - _CAP)
    return feed


def format_param(feed: ScannerFeed) -> str:
    lines = [
        "Open GitHub security alerts for this repository. These are HINTS to "
        "verdict during your review, not findings to copy: confirm each in the "
        "diff or the code before reporting, and cite the customer's file.",
    ]
    for e in feed.entries:
        loc = f"{e.file}:{e.line}" if e.line else e.file
        lines.append(f"- {e.tool} | {e.rule} | {e.severity} | {loc} | {e.description}")
    if feed.omitted:
        lines.append(f"({feed.omitted} more alerts omitted)")
    return "\n".join(lines)
```

- [x] **Step 3: Run to verify pass, commit**

```bash
cd worker && .venv/bin/python -m pytest tests/test_scanner_feed.py -q
git add reva/scanner_feed.py worker/tests/test_scanner_feed.py
git commit -m "feat(scanner): alert collector + fenced-param formatter"
```

---

### Task 3: Reviewer wiring + secret floor + kill switch

**Files:**
- Modify: `worker/worker/reviewer.py` (param attachment next to `manifest_audit` ~line 521; post-hoc floor next to `_calibrate_odoo_severity`), `reva/types.py` (`RepoConfig.scanner_feed`)
- Test: `worker/tests/test_reviewer_scanner.py`

**Interfaces:**
- Consumes: Task 1 readers via `self.github`, Task 2 collector, the Reviewer ops-event seam.
- Produces: optional `skill_params["scanner_alerts"]`; `_floor_secret_findings(findings, feed)`; `RepoConfig.scanner_feed: bool = True`.

- [x] **Step 1: Write the failing tests**

Create `worker/tests/test_reviewer_scanner.py` (reuse `test_reviewer.py`'s
fixture pattern; monkeypatch `worker.worker.reviewer.scanner_collect` — the
name the wiring imports). Assertions, one test each:

```python
# 1. feed with entries      → skill_params["scanner_alerts"] present, fenced
#                             by the runner like every param (assert the fake
#                             runner received the param)
# 2. empty feed             → param absent (prompt-cache stability)
# 3. repo_config.scanner_feed=False → collector never called
# 4. collector raises       → review proceeds, param absent, ops event
#                             ("scanner_feed", "warning", "collect_failed")
# 5. unavailable sources    → ops event ("scanner_feed", "warning",
#                             "sources_unavailable", detail lists them) —
#                             once per run, review proceeds
# 6. secret floor: a finding whose file matches a secret-scanning entry's
#    file (or any finding when the secret entry is repo-wide "-"?? NO —
#    floor only applies to file-anchored matches; repo-wide secret alerts
#    floor nothing) is raised to severity "critical"
```

Write them concretely against the fixture; for #6 call the new
`_floor_secret_findings` directly with a `Finding(severity="minor",
category="security", file="a.py", …)` and a feed containing a
`secret-scanning` entry with `file="a.py"` → severity becomes `"critical"`;
an entry with `file="-"` floors nothing.

- [x] **Step 2: Implement**

`reva/types.py::RepoConfig`:

```python
    # Kill switch for the GitHub security-alert feed (scanner-feed spec).
    scanner_feed: bool = True
```

`worker/worker/reviewer.py` — import
`from reva.scanner_feed import collect as scanner_collect, format_param`;
after the `manifest_audit` block:

```python
        # Scanner feed (spec 2026-07-05): open GitHub security alerts as
        # fenced hints. Fail-open: any collection problem degrades to "no
        # param" + an ops event; the review never fails because of it.
        scanner_feed_result = None
        if repo_config.scanner_feed:
            try:
                scanner_feed_result = scanner_collect(
                    self.github, token, owner, name, changed_files
                )
                if scanner_feed_result.unavailable:
                    self._record_ops_event(
                        "scanner_feed", "warning", "sources_unavailable",
                        {"repo": f"{owner}/{name}",
                         "sources": scanner_feed_result.unavailable},
                    )
                if scanner_feed_result.entries:
                    skill_params["scanner_alerts"] = format_param(scanner_feed_result)
                    log.info("scanner_alerts_attached",
                             alerts=len(scanner_feed_result.entries))
            except Exception:
                log.warning("scanner_feed_failed", exc_info=True)
                self._record_ops_event("scanner_feed", "warning",
                                       "collect_failed",
                                       {"repo": f"{owner}/{name}"})
```

Post-hoc floor — module-level function + call it where findings are
post-processed (next to `_drop_muted_findings` / `_calibrate_odoo_severity`
in the result pipeline):

```python
def _floor_secret_findings(findings, feed) -> None:
    """A finding on a file with an open secret-scanning alert is critical —
    deterministic floor, the _calibrate_odoo_severity pattern."""
    if feed is None:
        return
    secret_files = {e.file for e in feed.entries
                    if e.tool == "secret-scanning" and e.file != "-"}
    for f in findings:
        if f.file in secret_files and f.severity != "critical":
            f.severity = "critical"
```

(`Finding.severity` mutability: if the model is frozen, rebuild the finding
with `model_copy(update={"severity": "critical"})` and replace it in the
list — check `grep -n "model_config" reva/types.py` near `Finding`.)

- [x] **Step 3: Run to verify pass, commit**

```bash
cd worker && .venv/bin/python -m pytest tests/test_reviewer_scanner.py tests/test_reviewer.py -q
git add reva/types.py worker/worker/reviewer.py worker/tests/test_reviewer_scanner.py
git commit -m "feat(scanner): fenced alert param + secret severity floor"
```

---

### Task 4: Guidance, CHANGELOG, docs, final verification

**Files:**
- Modify: `prompts/review_guidance.md`, `prompts/CHANGELOG.md` (+ `test_get_version`), `docs/setup-production.md` (App-permission step)

- [x] **Step 1: Guidance section** (append to `prompts/review_guidance.md`):

```markdown
## Security-alert hints (scanner feed)

Some reviews carry a `scanner_alerts` task parameter: open GitHub security
alerts (secret-scanning, code-scanning, Dependabot) for this repository.
They are hints, not findings — verdict each one that intersects your review:
confirm it in the diff or the code, then report it as an ordinary finding
citing the customer's file, naming the alert in the body. Ignore alerts you
cannot confirm or that don't intersect the change. Never invent alerts, and
never treat the parameter's absence as evidence the repo is clean.
```

- [x] **Step 2: CHANGELOG bump** (next version after the triage plan's, if
that landed first) + update the `test_get_version` assertion.

- [x] **Step 3: Operator docs** — add to `docs/setup-production.md`'s
checklist:

```markdown
1. **Scanner feed permissions.** Grant the GitHub App read access to
   Code scanning alerts, Dependabot alerts, and Secret scanning alerts
   (App settings → Permissions), then accept the permission update on the
   installation. Until then the scanner feed is a visible no-op
   (`scanner_feed / sources_unavailable` ops events).
```

- [x] **Step 4: Full gate + commit + report**

```bash
make test
worker/.venv/bin/ruff check reva worker/worker api/app scheduler/scheduler
git add prompts/ docs/setup-production.md worker/tests/
git commit -m "feat(prompts+docs): scanner-alert guidance + operator permission step"
```

Report honestly: no live GHAS repo was exercised — the staging gate (spec)
is one repo with alerts enabled, confirming the fenced param appears and a
planted secret alert floors its finding to critical.
