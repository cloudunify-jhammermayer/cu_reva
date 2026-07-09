"""GitHub App API client (read-only surface).

Satisfies the GitHubReader Protocol (reviewer.py / auditor.py). Two-step auth:
  1. Sign a short-lived JWT with the App's RSA private key.
  2. Exchange the JWT for an installation token scoped to one org.
Installation tokens are cached in-process keyed by installation_id.

Write methods (Check Runs, PR Reviews, inline comments, reactions) live on
a separate GitHubPoster surface in a later slice.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx
import jwt
import structlog

from reva._github_http import NotFound, map_github_status
from reva.errors import PermanentError, TransientError

logger = structlog.get_logger()

DEFAULT_BASE_URL = "https://api.github.com"
JWT_TTL_SECONDS = 9 * 60   # GitHub allows up to 10 minutes
JWT_IAT_SKEW = 60          # clock-skew tolerance
TOKEN_SAFETY_MARGIN = timedelta(minutes=5)
MAX_FILE_PAGES = 30        # safety net; size guard handles real cap
PAGE_SIZE = 100


def _graphql_data(response: httpx.Response, action: str) -> dict:
    """Extract `data` from a GraphQL response, raising on errors (M7).

    GraphQL failures come back as HTTP 200 with an `errors` array and a null
    `data` (or nulled sub-objects), which the naive `.get("data", {})` chain
    silently swallowed — turning a missing App permission into a silent no-op and
    crashing on `data: null`. Surface them: RATE_LIMITED is transient, everything
    else permanent (both are caught by the best-effort callers and logged)."""
    payload = response.json()
    errors = payload.get("errors")
    if errors:
        types = {e.get("type") for e in errors if isinstance(e, dict)}
        msg = "; ".join(e.get("message", "") for e in errors if isinstance(e, dict))[:200]
        if "RATE_LIMITED" in types:
            raise TransientError(f"GitHub GraphQL rate limited ({action}): {msg}")
        raise PermanentError(f"GitHub GraphQL error ({action}): {msg}")
    data = payload.get("data")
    if data is None:
        raise PermanentError(f"GitHub GraphQL returned no data ({action})")
    return data


def _submitted_after(review: dict, since: datetime) -> bool:
    """True if a PR review's submitted_at is at/after `since`. Conservatively
    False if missing/unparseable (so an ambiguous review isn't recovered)."""
    raw = review.get("submitted_at")
    if not raw:
        return False
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    return ts >= since


@dataclass
class _CachedToken:
    token: str
    expires_at: datetime  # safety margin already applied


class GitHubClient:
    def __init__(
        self,
        app_id: int,
        private_key_pem: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.app_id = app_id
        self.private_key_pem = private_key_pem
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=True)
        self._token_cache: dict[int, _CachedToken] = {}

    # --- JWT ----------------------------------------------------------------

    def _make_jwt(self) -> str:
        now = int(time.time())
        payload = {
            "iat": now - JWT_IAT_SKEW,
            "exp": now + JWT_TTL_SECONDS,
            "iss": str(self.app_id),
        }
        return jwt.encode(payload, self.private_key_pem, algorithm="RS256")

    # --- Installation tokens ------------------------------------------------

    def get_installation_token(self, installation_id: int) -> str:
        cached = self._token_cache.get(installation_id)
        if cached and datetime.now(timezone.utc) < cached.expires_at:
            return cached.token

        url = f"{self.base_url}/app/installations/{installation_id}/access_tokens"
        headers = {
            "Authorization": f"Bearer {self._make_jwt()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            response = self._client.post(url, headers=headers)
        except httpx.TimeoutException as exc:
            raise TransientError(f"GitHub timeout (token exchange): {exc}") from exc
        except httpx.TransportError as exc:
            raise TransientError(f"GitHub transport (token exchange): {exc}") from exc

        if response.status_code != 201:
            raise map_github_status(response, action="token exchange")

        data = response.json()
        expires_at = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
        self._token_cache[installation_id] = _CachedToken(
            token=data["token"],
            expires_at=expires_at - TOKEN_SAFETY_MARGIN,
        )
        return data["token"]

    # --- Reads --------------------------------------------------------------

    def get_repo_installation_id(self, owner: str, repo: str) -> int:
        """Installation id for a repo the app is installed on (app-JWT auth).

        Lets REVA register a repo on demand (for audits) without waiting for a
        webhook. 404 → the app isn't installed on that repo (PermanentError)."""
        response = self._get(self._make_jwt(), f"/repos/{owner}/{repo}/installation")
        return response.json()["id"]

    def get_repo(self, token: str, owner: str, repo: str) -> dict:
        """Repository metadata: id, full_name, default_branch, owner, ..."""
        return self._get(token, f"/repos/{owner}/{repo}").json()

    def get_pull_request(self, token: str, owner: str, repo: str, pr_number: int) -> dict:
        response = self._get(token, f"/repos/{owner}/{repo}/pulls/{pr_number}")
        return response.json()

    def get_pull_request_diff(self, token: str, owner: str, repo: str, pr_number: int) -> str:
        response = self._get(
            token,
            f"/repos/{owner}/{repo}/pulls/{pr_number}",
            extra_headers={"Accept": "application/vnd.github.v3.diff"},
        )
        return response.text

    def get_changed_files(self, token: str, owner: str, repo: str, pr_number: int) -> list[dict]:
        out: list[dict] = []
        for page in range(1, MAX_FILE_PAGES + 1):
            response = self._get(
                token,
                f"/repos/{owner}/{repo}/pulls/{pr_number}/files",
                params={"per_page": PAGE_SIZE, "page": page},
            )
            batch = response.json()
            if not batch:
                break
            out.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
        else:
            logger.warning(
                "github_changed_files_truncated",
                owner=owner,
                repo=repo,
                pr_number=pr_number,
                pages=MAX_FILE_PAGES,
            )
        return out

    def get_file_content(
        self, token: str, owner: str, repo: str, path: str, ref: str
    ) -> str | None:
        try:
            response = self._get(
                token,
                # URL-encode the file path (CORR-18) — keep `/` as the path
                # separator but encode spaces/#/?/etc. so a filename with special
                # chars produces a valid URL rather than a malformed request.
                f"/repos/{owner}/{repo}/contents/{quote(path, safe='/')}",
                params={"ref": ref},
                extra_headers={"Accept": "application/vnd.github.raw"},
                allow_404=True,
            )
        except NotFound:
            return None
        return response.text

    def get_tree(
        self, token: str, owner: str, repo: str, ref: str, recursive: bool = True
    ) -> dict:
        """Full git tree at `ref` (branch name or commit SHA).

        Returns the raw GitHub payload: ``{"tree": [...], "truncated": bool}``,
        where each entry is ``{"path", "type" ("blob"|"tree"), "sha", "size"?}``.
        Lets the docs surface enumerate a repo's files without cloning it.
        `ref` is a single path segment (default branch / SHA); slashed branch
        names aren't supported here — the docs endpoints only pass the default
        branch."""
        params = {"recursive": "1"} if recursive else None
        response = self._get(
            token, f"/repos/{owner}/{repo}/git/trees/{quote(ref, safe='')}", params=params
        )
        return response.json()

    def get_raw_file(
        self, token: str, owner: str, repo: str, path: str, ref: str
    ) -> bytes | None:
        """Raw bytes of a file at `ref` (for doc-embedded images/assets), or None
        on 404. Mirrors get_file_content's path-encoding and 404 handling but
        returns bytes rather than decoded text."""
        try:
            response = self._get(
                token,
                f"/repos/{owner}/{repo}/contents/{quote(path, safe='/')}",
                params={"ref": ref},
                extra_headers={"Accept": "application/vnd.github.raw"},
                allow_404=True,
            )
        except NotFound:
            return None
        return response.content

    def get_branches(self, token: str, owner: str, repo: str) -> list[dict]:
        """All branches as ``[{"name", "sha"}]`` (paginated). Backs the docs UI
        branch picker; the head SHA is what the tree endpoint needs, since the
        Git Trees API wants a tree-ish, not a (possibly slashed) branch name."""
        out: list[dict] = []
        for page in range(1, MAX_FILE_PAGES + 1):
            response = self._get(
                token,
                f"/repos/{owner}/{repo}/branches",
                params={"per_page": PAGE_SIZE, "page": page},
            )
            batch = response.json()
            if not batch:
                break
            out.extend({"name": b["name"], "sha": b["commit"]["sha"]} for b in batch)
            if len(batch) < PAGE_SIZE:
                break
        return out

    def get_issue(
        self, token: str, owner: str, repo: str, issue_number: int
    ) -> dict | None:
        """Return {title, body, node_id} for an issue, or None if it 404s
        (deleted / wrong number / cross-repo #N). Same Issues:read scope as
        create_issue, so no new GitHub App permission. Mirrors get_file_content's
        404 handling. node_id backfills pre-feature items into Projects v2."""
        try:
            response = self._get(
                token,
                f"/repos/{owner}/{repo}/issues/{issue_number}",
                allow_404=True,
            )
        except NotFound:
            return None
        data = response.json()
        return {"title": data.get("title") or "", "body": data.get("body") or "",
                "node_id": data.get("node_id")}

    # --- security alerts (scanner-feed spec) -------------------------------

    def _list_alerts(self, token: str, path: str) -> list[dict] | None:
        """One page of open alerts, or None when the source is unavailable."""
        try:
            response = self._get(
                token,
                path,
                params={"state": "open", "per_page": PAGE_SIZE},
                allow_404=True,
                allow_statuses=frozenset({403}),
            )
        except NotFound:
            return None
        if response.status_code == 403:
            # Distinguish rate-limit 403s (transient — retry) from
            # missing-feature/permission 403s (source unavailable). Without
            # this a rate-limited window mislabels all three sources as
            # unavailable in the ops events (review finding #8).
            if response.headers.get("x-ratelimit-remaining") == "0" or (
                "retry-after" in response.headers
            ):
                raise map_github_status(response, action=path)
            return None
        return response.json()

    def get_secret_alert_locations(
        self, token: str, owner: str, repo: str, alert_number: int
    ) -> list[dict] | None:
        """First page of one secret alert's locations, or None when unavailable.

        The list endpoint returns only `locations_url`; the file-anchored
        critical-severity floor needs the actual path/line (scanner-feed spec
        locked decision 4 — review finding #1).
        """
        try:
            response = self._get(
                token,
                f"/repos/{owner}/{repo}/secret-scanning/alerts/{alert_number}/locations",
                params={"per_page": PAGE_SIZE},
                allow_404=True,
                allow_statuses=frozenset({403}),
            )
        except NotFound:
            return None
        if response.status_code == 403:
            return None
        return response.json()

    def list_code_scanning_alerts(
        self, token: str, owner: str, repo: str
    ) -> list[dict] | None:
        return self._list_alerts(token, f"/repos/{owner}/{repo}/code-scanning/alerts")

    def list_dependabot_alerts(
        self, token: str, owner: str, repo: str
    ) -> list[dict] | None:
        return self._list_alerts(token, f"/repos/{owner}/{repo}/dependabot/alerts")

    def list_secret_scanning_alerts(
        self, token: str, owner: str, repo: str
    ) -> list[dict] | None:
        return self._list_alerts(token, f"/repos/{owner}/{repo}/secret-scanning/alerts")

    def get_compare_diff(
        self, token: str, owner: str, repo: str, base_sha: str, head_sha: str
    ) -> str:
        """Return the unified diff between two SHAs."""
        response = self._get(
            token,
            f"/repos/{owner}/{repo}/compare/{base_sha}...{head_sha}",
            extra_headers={"Accept": "application/vnd.github.v3.diff"},
        )
        return response.text

    def get_compare_status(
        self, token: str, owner: str, repo: str, base_sha: str, head_sha: str
    ) -> str:
        """Return GitHub's compare `status` for base...head — one of
        "ahead" | "behind" | "identical" | "diverged".

        "ahead"/"identical" mean base is an ancestor of head (a clean follow-up
        push), so the two-dot compare diff is a true delta. "diverged"/"behind"
        mean the branch was rebased/squashed/reset, so the delta base is invalid.
        Reads the compare endpoint as JSON (not the v3.diff media type)."""
        response = self._get(
            token,
            f"/repos/{owner}/{repo}/compare/{base_sha}...{head_sha}",
        )
        return response.json().get("status", "")

    def find_pr_review_id(
        self, token: str, owner: str, repo: str, pr_number: int, marker: str,
        since: datetime | None = None,
    ) -> int | None:
        """Return the id of our PR review whose body contains `marker`, else None.

        Used to recover from a crash between posting a review and persisting its
        id, so a retry reuses the existing review instead of duplicating it.

        CORR-5: only reviews authored by a Bot (our GitHub App) are considered,
        so a non-bot commenter can't post a review echoing the marker to hijack
        the recovered id. The listing is paginated (the marker may be on a later
        page once a PR accumulates reviews).

        `since`: only reviews submitted at/after this time are considered. The
        marker ("Run #N") is NOT unique across runs — the run-id sequence can
        repeat (DB reset) or a re-review reuses the row — so without this a
        re-review would recover a STALE prior review and skip posting its new
        findings (PR-9 regression). Scoping to this run's era keeps same-attempt
        crash recovery while ignoring old reviews.
        """
        match = None
        page = 1
        while True:
            reviews = self._get(
                token,
                f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
                params={"per_page": 100, "page": page},
            ).json()
            for review in reviews:
                if (review.get("user") or {}).get("type") != "Bot":
                    continue  # only our app's bot account can be us
                if since is not None and not _submitted_after(review, since):
                    continue  # stale prior-era review — not this run's
                if marker in (review.get("body") or ""):
                    match = review["id"]  # keep the last (newest) match
            if len(reviews) < 100:
                break
            page += 1
        return match

    def find_check_run_id(
        self, token: str, owner: str, repo: str, head_sha: str, name: str
    ) -> int | None:
        """Return the id of our Check Run named `name` on `head_sha`, else None.

        Recovers a Check Run posted by a prior attempt that crashed before its
        id was persisted, so a retry reuses it instead of posting a duplicate.
        """
        response = self._get(
            token,
            f"/repos/{owner}/{repo}/commits/{head_sha}/check-runs",
            params={"check_name": name},
        )
        runs = response.json().get("check_runs") or []
        return runs[0]["id"] if runs else None

    # --- Writes -------------------------------------------------------------

    def create_check_run(
        self,
        token: str,
        owner: str,
        repo: str,
        head_sha: str,
        name: str,
        status: str,
        conclusion: str | None,
        started_at: str | None,
        completed_at: str | None,
        output: dict,
    ) -> int:
        """Create a Check Run. Returns the new check_run id."""
        body: dict = {
            "name": name,
            "head_sha": head_sha,
            "status": status,
            "output": output,
        }
        if conclusion is not None:
            body["conclusion"] = conclusion
        if started_at is not None:
            body["started_at"] = started_at
        if completed_at is not None:
            body["completed_at"] = completed_at
        response = self._post(token, f"/repos/{owner}/{repo}/check-runs", body)
        return response.json()["id"]

    def update_check_run(
        self,
        token: str,
        owner: str,
        repo: str,
        check_run_id: int,
        status: str,
        conclusion: str | None,
        started_at: str | None,
        completed_at: str | None,
        output: dict,
    ) -> int:
        """Update an existing Check Run with a fresh result. Returns its id.

        Used on re-review/recovery so the existing Check Run on this SHA shows
        the current conclusion instead of a stale prior one.
        """
        body: dict = {"status": status, "output": output}
        if conclusion is not None:
            body["conclusion"] = conclusion
        if started_at is not None:
            body["started_at"] = started_at
        if completed_at is not None:
            body["completed_at"] = completed_at
        self._patch(token, f"/repos/{owner}/{repo}/check-runs/{check_run_id}", body)
        return check_run_id

    def create_pr_review(
        self,
        token: str,
        owner: str,
        repo: str,
        pr_number: int,
        commit_id: str,
        event: str,
        body: str,
        comments: list[dict],
    ) -> int:
        """Create a PR Review with optional inline comments. Returns the new review id."""
        payload = {
            "commit_id": commit_id,
            "event": event,
            "body": body,
            "comments": comments,
        }
        response = self._post(
            token, f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews", payload
        )
        return response.json()["id"]

    def create_issue_comment(
        self,
        token: str,
        owner: str,
        repo: str,
        pr_number: int,
        body: str,
    ) -> int:
        """Post a standalone comment on a PR. Returns the new comment id."""
        response = self._post(
            token,
            f"/repos/{owner}/{repo}/issues/{pr_number}/comments",
            {"body": body},
        )
        return response.json()["id"]

    def create_issue(
        self,
        token: str,
        owner: str,
        repo: str,
        title: str,
        body: str,
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
    ) -> dict:
        """Open a new issue. Returns {"number", "url", "id", "node_id"} — url is
        GitHub's canonical html_url, not reconstructed from the (possibly
        mis-cased) owner/repo the caller was given; node_id is the GraphQL id
        Projects v2 mutations key on."""
        payload: dict = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        if assignees:
            payload["assignees"] = assignees
        response = self._post(token, f"/repos/{owner}/{repo}/issues", payload)
        data = response.json()
        return {"number": data["number"], "url": data["html_url"], "id": data["id"],
                "node_id": data.get("node_id")}

    def add_sub_issue(
        self, token: str, owner: str, repo: str, parent_number: int, sub_issue_id: int
    ) -> None:
        """Attach an existing issue as a sub-issue of `parent_number`.

        The GitHub sub-issues API keys on the child's database `id` (the value
        create_issue returns), NOT its number. A 422 means it is ALREADY a
        sub-issue of this parent (the resume/requeue case) and is a no-op; any
        other failure — notably 403/404 if the App token cannot reach the
        sub-issues endpoint — must surface, not be masked as success."""
        self._post(
            token,
            f"/repos/{owner}/{repo}/issues/{parent_number}/sub_issues",
            {"sub_issue_id": sub_issue_id},
            allow_status=frozenset({422}),
        )

    def ensure_label(
        self,
        token: str,
        owner: str,
        repo: str,
        name: str,
        color: str = "5319e7",
        description: str = "",
    ) -> None:
        """Create a repo label if it doesn't exist (idempotent, best-effort).

        Labels are per-repo, so REVA creates its own rather than requiring it to
        be set up by hand. An existing label returns 422 'already_exists'; any
        failure is swallowed (labelling must never block issue creation)."""
        try:
            self._post(
                token,
                f"/repos/{owner}/{repo}/labels",
                {"name": name, "color": color, "description": description},
            )
        except (PermanentError, TransientError):
            pass

    def get_issue_labels(
        self, token: str, owner: str, repo: str, issue_number: int
    ) -> list[str]:
        """Label names currently on an issue/PR (single page).

        PRs and issues share the `/issues/{n}/labels` endpoint, so `issue_number`
        is the PR number for a pull request.
        """
        response = self._get(
            token, f"/repos/{owner}/{repo}/issues/{issue_number}/labels"
        )
        return [item["name"] for item in response.json()]

    def add_labels(
        self, token: str, owner: str, repo: str, issue_number: int, labels: list[str]
    ) -> None:
        """Add labels to an issue/PR. Additive — does not clear existing labels."""
        self._post(
            token,
            f"/repos/{owner}/{repo}/issues/{issue_number}/labels",
            {"labels": labels},
        )

    def remove_label(
        self, token: str, owner: str, repo: str, issue_number: int, name: str
    ) -> None:
        """Remove a single label from an issue/PR. A 404 (label not present) is a
        no-op. The label name is URL-encoded (it may contain ':' or spaces)."""
        self._delete(
            token,
            f"/repos/{owner}/{repo}/issues/{issue_number}/labels/{quote(name, safe='')}",
            allow_404=True,
        )

    def issue_exists_with_marker(
        self, token: str, owner: str, repo: str, marker: str
    ) -> bool:
        """True if an OPEN issue in the repo contains `marker` in its body.

        Used to dedup audit issues across re-runs — `marker` is a stable,
        plain-alphanumeric token embedded (as an HTML comment) in issue bodies.
        """
        response = self._get(
            token,
            "/search/issues",
            params={"q": f"repo:{owner}/{repo} type:issue state:open {marker}"},
        )
        return (response.json().get("total_count") or 0) > 0

    def find_issues_with_marker(
        self, token: str, owner: str, repo: str, marker: str
    ) -> list[dict]:
        """Issues — open AND closed — whose body contains `marker`, as
        [{"number", "title", "url", "state", "id", "node_id"}].

        Used to reconcile ticket issues across re-runs: a re-click or Odoo's
        10s-timeout race must re-link the existing issues, not duplicate them,
        and a closed (completed) issue still belongs to its ticket — so no
        state filter, unlike issue_exists_with_marker.
        """
        response = self._get(
            token,
            "/search/issues",
            params={"q": f"repo:{owner}/{repo} type:issue {marker}"},
        )
        return [
            {
                "number": item["number"],
                "title": item["title"],
                "url": item["html_url"],
                "state": item.get("state", "open"),
                "id": item["id"],
                "node_id": item.get("node_id"),
            }
            for item in response.json().get("items", [])
        ]

    def get_review_comments(
        self, token: str, owner: str, repo: str, pr_number: int, review_id: int
    ) -> list[dict]:
        """Return the inline comments belonging to PR review `review_id`.

        Uses the PR-level comments endpoint, NOT `/pulls/{pr}/reviews/{id}/comments`:
        the review-scoped endpoint reports `line: null` for comments created with
        the review, which breaks location-based matching (the comment-id backfill
        then attaches nothing and delta re-reviews can't resolve threads — Aurium
        PR-60 regression). The PR-level endpoint reports the resolved `line`, and
        each comment carries `pull_request_review_id` to filter on.

        Each item has at minimum: id, path, line, start_line (nullable).
        """
        out: list[dict] = []
        page = 1
        while True:
            response = self._get(
                token,
                f"/repos/{owner}/{repo}/pulls/{pr_number}/comments",
                params={"per_page": 100, "page": page},
            )
            batch = response.json()
            out.extend(c for c in batch if c.get("pull_request_review_id") == review_id)
            if len(batch) < 100:
                break
            page += 1
        return out

    def reply_to_review_comment(
        self,
        token: str,
        owner: str,
        repo: str,
        pr_number: int,
        comment_id: int,
        body: str,
    ) -> int:
        """Post a reply in an existing review comment thread. Returns the new comment id."""
        response = self._post(
            token,
            f"/repos/{owner}/{repo}/pulls/{pr_number}/comments/{comment_id}/replies",
            {"body": body},
        )
        return response.json()["id"]

    def get_review_threads(
        self, token: str, owner: str, repo: str, pr_number: int
    ) -> dict[int, str]:
        """Return {github_comment_database_id → thread_node_id} for unresolved threads.

        Paginated (CORR-8): a busy PR can have >100 review threads, and resolution
        must consider all of them, not just the first page."""
        query = """
        query GetPRThreads($owner: String!, $repo: String!, $prNumber: Int!, $cursor: String) {
          repository(owner: $owner, name: $repo) {
            pullRequest(number: $prNumber) {
              reviewThreads(first: 100, after: $cursor) {
                pageInfo { hasNextPage endCursor }
                nodes {
                  id
                  isResolved
                  comments(first: 1) {
                    nodes { databaseId }
                  }
                }
              }
            }
          }
        }
        """
        out: dict[int, str] = {}
        cursor: str | None = None
        while True:
            response = self._post(
                token,
                "/graphql",
                {"query": query, "variables": {
                    "owner": owner, "repo": repo, "prNumber": pr_number, "cursor": cursor,
                }},
            )
            data = _graphql_data(response, "get_review_threads")
            # Null-safe: a nulled repository/pullRequest (e.g. permission gap)
            # yields {} → no threads, loop ends — never an AttributeError.
            review_threads = (
                ((data.get("repository") or {}).get("pullRequest") or {}).get("reviewThreads")
                or {}
            )
            for node in review_threads.get("nodes", []):
                if not node.get("isResolved") and node.get("comments", {}).get("nodes"):
                    out[node["comments"]["nodes"][0]["databaseId"]] = node["id"]
            page = review_threads.get("pageInfo", {})
            if not page.get("hasNextPage") or not page.get("endCursor"):
                break
            cursor = page["endCursor"]
        return out

    def resolve_review_thread(self, token: str, thread_node_id: str) -> None:
        """Resolve a pull request review thread via GraphQL."""
        mutation = """
        mutation ResolveThread($threadId: ID!) {
          resolveReviewThread(input: {threadId: $threadId}) {
            thread { isResolved }
          }
        }
        """
        response = self._post(
            token, "/graphql", {"query": mutation, "variables": {"threadId": thread_node_id}}
        )
        # M7: surface a GraphQL error instead of reporting success unconditionally
        # (a failed resolve must not be marked resolved_by_fix by the caller).
        _graphql_data(response, "resolve_review_thread")

    # --- GitHub Projects v2 (GraphQL-only) ----------------------------------

    # Shared field selection: plain fields expose id/name/dataType; single-
    # selects additionally expose their options (id + name) for option lookup.
    _PROJECT_FIELD_FRAGMENT = """
              ... on ProjectV2FieldCommon { id name dataType }
              ... on ProjectV2SingleSelectField { id name dataType options { id name } }"""

    def get_project(self, token: str, owner_type: str, owner: str, number: int) -> dict:
        """Resolve a Projects v2 board URL to {"id", "fields"} (first 50 fields,
        each {"id", "name", "dataType"[, "options"]}).

        Projects v2 has no REST API. A null projectV2 means the number is wrong
        OR the App installation lacks the org-level Projects permission — the
        caller (ticket_issue_runner) degrades fail-soft either way."""
        entity = "organization" if owner_type == "orgs" else "user"
        query = f"""
        query($login: String!, $number: Int!) {{
          {entity}(login: $login) {{
            projectV2(number: $number) {{
              id
              fields(first: 50) {{ nodes {{{self._PROJECT_FIELD_FRAGMENT}
              }} }}
            }}
          }}
        }}"""
        response = self._post(
            token, "/graphql",
            {"query": query, "variables": {"login": owner, "number": number}},
        )
        data = _graphql_data(response, "get_project")
        project = (data.get(entity) or {}).get("projectV2")
        if project is None:
            raise PermanentError(
                f"project {owner_type}/{owner}/projects/{number} not found "
                "(or the GitHub App lacks the org Projects permission)"
            )
        return {
            "id": project["id"],
            "fields": [f for f in (project.get("fields") or {}).get("nodes", []) if f],
        }

    def create_project_field(
        self,
        token: str,
        project_id: str,
        name: str,
        data_type: str,
        options: list[dict] | None = None,
    ) -> dict:
        """Create a project field (DATE, or SINGLE_SELECT with options as
        [{"name", "color", "description"}]) and return its field dict —
        same shape as get_project's fields, incl. created option ids."""
        mutation = f"""
        mutation($projectId: ID!, $name: String!, $dataType: ProjectV2CustomFieldType!,
                 $options: [ProjectV2SingleSelectFieldOptionInput!]) {{
          createProjectV2Field(input: {{
            projectId: $projectId, name: $name, dataType: $dataType,
            singleSelectOptions: $options
          }}) {{
            projectV2Field {{{self._PROJECT_FIELD_FRAGMENT}
            }}
          }}
        }}"""
        response = self._post(
            token, "/graphql",
            {"query": mutation, "variables": {
                "projectId": project_id, "name": name,
                "dataType": data_type, "options": options,
            }},
        )
        data = _graphql_data(response, "create_project_field")
        return data["createProjectV2Field"]["projectV2Field"]

    def add_issue_to_project(self, token: str, project_id: str, content_node_id: str) -> str:
        """Add an issue (by GraphQL node id) to a project; returns the project
        item id. Idempotent by API contract — re-adding returns the existing
        item's id."""
        mutation = """
        mutation($projectId: ID!, $contentId: ID!) {
          addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
            item { id }
          }
        }"""
        response = self._post(
            token, "/graphql",
            {"query": mutation, "variables": {
                "projectId": project_id, "contentId": content_node_id,
            }},
        )
        data = _graphql_data(response, "add_issue_to_project")
        return data["addProjectV2ItemById"]["item"]["id"]

    def _set_project_item_value(
        self, token: str, project_id: str, item_id: str, field_id: str, value: dict
    ) -> None:
        mutation = """
        mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $value: ProjectV2FieldValue!) {
          updateProjectV2ItemFieldValue(input: {
            projectId: $projectId, itemId: $itemId, fieldId: $fieldId, value: $value
          }) {
            projectV2Item { id }
          }
        }"""
        response = self._post(
            token, "/graphql",
            {"query": mutation, "variables": {
                "projectId": project_id, "itemId": item_id,
                "fieldId": field_id, "value": value,
            }},
        )
        # M7: surface a GraphQL error instead of reporting success unconditionally.
        _graphql_data(response, "update_project_item_field")

    def set_project_item_date(
        self, token: str, project_id: str, item_id: str, field_id: str, date_value: str
    ) -> None:
        """Set a DATE field on a project item (date_value: YYYY-MM-DD)."""
        self._set_project_item_value(
            token, project_id, item_id, field_id, {"date": date_value})

    def set_project_item_option(
        self, token: str, project_id: str, item_id: str, field_id: str, option_id: str
    ) -> None:
        """Set a SINGLE_SELECT field on a project item to `option_id`."""
        self._set_project_item_value(
            token, project_id, item_id, field_id, {"singleSelectOptionId": option_id})

    def set_project_item_number(
        self, token: str, project_id: str, item_id: str, field_id: str, number: float
    ) -> None:
        """Set a NUMBER field on a project item to `number`."""
        self._set_project_item_value(
            token, project_id, item_id, field_id, {"number": number})

    # --- shared HTTP --------------------------------------------------------

    def _get(
        self,
        token: str,
        path: str,
        params: dict | None = None,
        extra_headers: dict | None = None,
        allow_404: bool = False,
        allow_statuses: frozenset[int] = frozenset(),
    ) -> httpx.Response:
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if extra_headers:
            headers.update(extra_headers)
        try:
            response = self._client.get(url, headers=headers, params=params)
        except httpx.TimeoutException as exc:
            raise TransientError(f"GitHub timeout: {exc}") from exc
        except httpx.TransportError as exc:
            raise TransientError(f"GitHub transport error: {exc}") from exc

        if response.status_code == 404 and allow_404:
            raise NotFound()
        if response.status_code in allow_statuses:
            return response
        if response.status_code >= 300:
            raise map_github_status(response, action=path)
        return response

    def _post(
        self,
        token: str,
        path: str,
        json_body: dict,
        allow_status: frozenset[int] = frozenset(),
    ) -> httpx.Response:
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        }
        try:
            response = self._client.post(url, headers=headers, json=json_body)
        except httpx.TimeoutException as exc:
            raise TransientError(f"GitHub timeout: {exc}") from exc
        except httpx.TransportError as exc:
            raise TransientError(f"GitHub transport error: {exc}") from exc

        if response.status_code in allow_status:
            return response
        if response.status_code >= 300:
            raise map_github_status(response, action=path)
        return response

    def _patch(self, token: str, path: str, json_body: dict) -> httpx.Response:
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        }
        try:
            response = self._client.patch(url, headers=headers, json=json_body)
        except httpx.TimeoutException as exc:
            raise TransientError(f"GitHub timeout: {exc}") from exc
        except httpx.TransportError as exc:
            raise TransientError(f"GitHub transport error: {exc}") from exc

        if response.status_code >= 300:
            raise map_github_status(response, action=path)
        return response

    def _delete(
        self, token: str, path: str, allow_404: bool = False
    ) -> httpx.Response | None:
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            response = self._client.delete(url, headers=headers)
        except httpx.TimeoutException as exc:
            raise TransientError(f"GitHub timeout: {exc}") from exc
        except httpx.TransportError as exc:
            raise TransientError(f"GitHub transport error: {exc}") from exc

        # NB: _delete returns None on a 404 with allow_404 (a missing resource is
        # a true no-op for DELETE), unlike _get which raises NotFound — different
        # contract for the same flag name, intentional. Callers discard the return.
        if response.status_code == 404 and allow_404:
            return None
        if response.status_code >= 300:
            raise map_github_status(response, action=path)
        return response

    def close(self) -> None:
        self._client.close()
