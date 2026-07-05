"""GitHub security alerts as review context."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field

_CAP = 20
MANIFEST_PATTERNS = (
    "*requirements*.txt",
    "*pyproject.toml",
    "*package.json",
    "*package-lock.json",
    "*__manifest__.py",
    "*Pipfile*",
    "*poetry.lock",
)


@dataclass(frozen=True)
class ScannerEntry:
    tool: str
    rule: str
    severity: str
    file: str
    line: int | None
    description: str


@dataclass
class ScannerFeed:
    entries: list[ScannerEntry] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    omitted: int = 0


def _manifest_touched(changed_files: list[str]) -> bool:
    return any(
        fnmatch.fnmatch(path, pattern)
        for path in changed_files
        for pattern in MANIFEST_PATTERNS
    )


def _norm_code(alerts: list[dict], changed: set[str]) -> list[ScannerEntry]:
    out: list[ScannerEntry] = []
    for alert in alerts:
        instance = alert.get("most_recent_instance") or {}
        loc = instance.get("location") or {}
        path = loc.get("path")
        if not path or path not in changed:
            continue
        rule = alert.get("rule") or {}
        msg = instance.get("message") or {}
        out.append(ScannerEntry(
            tool="code-scanning",
            rule=str(rule.get("id", "?")),
            severity=str(rule.get("severity", "unknown")),
            file=path,
            line=loc.get("start_line"),
            description=str(msg.get("text") or rule.get("description") or "")[:200],
        ))
    return out


def _norm_dependabot(alerts: list[dict]) -> list[ScannerEntry]:
    out: list[ScannerEntry] = []
    for alert in alerts:
        advisory = alert.get("security_advisory") or {}
        dependency = alert.get("dependency") or {}
        package = dependency.get("package") or {}
        out.append(ScannerEntry(
            tool="dependabot",
            rule=str(package.get("name") or "?"),
            severity=str(advisory.get("severity", "unknown")),
            file=str(dependency.get("manifest_path") or "-"),
            line=None,
            description=str(advisory.get("summary") or "")[:200],
        ))
    return out


# Extra per-alert API calls to resolve secret locations are bounded — the
# floor only needs file anchors for the few (capped) alerts that make the feed.
_SECRET_LOCATION_LOOKUPS = 5


def _first_commit_location(locations: list[dict]) -> tuple[str, int | None]:
    """(path, start_line) of the first commit-type location, else ("-", None)."""
    for location in locations:
        if location.get("type") == "commit":
            details = location.get("details") or {}
            path = details.get("path")
            if path:
                return str(path), details.get("start_line")
    return "-", None


def _norm_secret(
    alerts: list[dict],
    locations_by_number: dict[int, list[dict]] | None = None,
) -> list[ScannerEntry]:
    """Normalize secret alerts. The LIST endpoint returns only locations_url,
    so file anchors come from the separately-fetched locations_by_number map
    (review finding #1 — without it the critical-severity floor never fires)."""
    lookup = locations_by_number or {}
    out: list[ScannerEntry] = []
    for alert in alerts:
        file, line = _first_commit_location(lookup.get(alert.get("number"), []))
        out.append(ScannerEntry(
            tool="secret-scanning",
            rule=str(
                alert.get("secret_type_display_name")
                or alert.get("secret_type")
                or "secret"
            ),
            severity="critical",
            file=file,
            line=line,
            description=f"open secret-scanning alert #{alert.get('number', '?')}",
        ))
    return out


def collect(
    github,
    token: str,
    owner: str,
    repo: str,
    changed_files: list[str],
) -> ScannerFeed:
    """Fetch, normalize, filter, and cap scanner alerts."""
    feed = ScannerFeed()
    changed = set(changed_files)

    secret = github.list_secret_scanning_alerts(token, owner, repo)
    if secret is None:
        feed.unavailable.append("secret-scanning")
    code = github.list_code_scanning_alerts(token, owner, repo)
    if code is None:
        feed.unavailable.append("code-scanning")
    dependabot = github.list_dependabot_alerts(token, owner, repo)
    if dependabot is None:
        feed.unavailable.append("dependabot")

    # Best-effort location enrichment for the first few secret alerts — this
    # is what anchors them to files so the severity floor can fire. Any
    # failure degrades to a repo-wide ("-") entry, never fails the feed.
    locations_by_number: dict[int, list[dict]] = {}
    for alert in (secret or [])[:_SECRET_LOCATION_LOOKUPS]:
        number = alert.get("number")
        if number is None:
            continue
        try:
            locations = github.get_secret_alert_locations(token, owner, repo, number)
        except Exception:
            locations = None
        if locations:
            locations_by_number[number] = locations

    prioritized = _norm_secret(secret or [], locations_by_number)
    prioritized += _norm_code(code or [], changed)
    if _manifest_touched(changed_files):
        prioritized += _norm_dependabot(dependabot or [])

    seen: set[tuple[str, str, str, int | None]] = set()
    unique: list[ScannerEntry] = []
    for entry in prioritized:
        key = (entry.tool, entry.rule, entry.file, entry.line)
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)

    feed.entries = unique[:_CAP]
    feed.omitted = max(0, len(unique) - _CAP)
    return feed


def format_param(feed: ScannerFeed) -> str:
    lines = [
        "Open GitHub security alerts for this repository. These are hints to "
        "verdict during your review, not findings to copy: confirm each in the "
        "diff or the code before reporting, and cite the customer's file.",
    ]
    for entry in feed.entries:
        loc = f"{entry.file}:{entry.line}" if entry.line else entry.file
        lines.append(
            f"- {entry.tool} | {entry.rule} | {entry.severity} | "
            f"{loc} | {entry.description}"
        )
    if feed.omitted:
        lines.append(f"({feed.omitted} more alerts omitted)")
    return "\n".join(lines)
