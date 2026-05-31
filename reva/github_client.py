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

import httpx
import jwt
import structlog

from reva._github_http import NotFound, map_github_status
from reva.errors import TransientError

logger = structlog.get_logger()

DEFAULT_BASE_URL = "https://api.github.com"
JWT_TTL_SECONDS = 9 * 60   # GitHub allows up to 10 minutes
JWT_IAT_SKEW = 60          # clock-skew tolerance
TOKEN_SAFETY_MARGIN = timedelta(minutes=5)
MAX_FILE_PAGES = 30        # safety net; size guard handles real cap
PAGE_SIZE = 100


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
                f"/repos/{owner}/{repo}/contents/{path}",
                params={"ref": ref},
                extra_headers={"Accept": "application/vnd.github.raw"},
                allow_404=True,
            )
        except NotFound:
            return None
        return response.text

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

    def find_pr_review_id(
        self, token: str, owner: str, repo: str, pr_number: int, marker: str
    ) -> int | None:
        """Return the id of our PR review whose body contains `marker`, else None.

        Used to recover from a crash between posting a review and persisting its
        id, so a retry reuses the existing review instead of duplicating it. The
        marker is the run footer (e.g. "Run #77"), unique per review run.
        """
        response = self._get(token, f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews")
        match = None
        for review in response.json():
            if marker in (review.get("body") or ""):
                match = review["id"]  # keep the last (newest) match
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

    def get_review_comments(
        self, token: str, owner: str, repo: str, pr_number: int, review_id: int
    ) -> list[dict]:
        """Return all inline comments posted in a PR review.

        Each item has at minimum: id, path, line, start_line (nullable).
        """
        response = self._get(
            token,
            f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews/{review_id}/comments",
        )
        return response.json()

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
        """Return {github_comment_database_id → thread_node_id} for unresolved threads."""
        query = """
        query GetPRThreads($owner: String!, $repo: String!, $prNumber: Int!) {
          repository(owner: $owner, name: $repo) {
            pullRequest(number: $prNumber) {
              reviewThreads(first: 100) {
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
        response = self._post(
            token,
            "/graphql",
            {"query": query, "variables": {"owner": owner, "repo": repo, "prNumber": pr_number}},
        )
        threads = (
            response.json()
            .get("data", {})
            .get("repository", {})
            .get("pullRequest", {})
            .get("reviewThreads", {})
            .get("nodes", [])
        )
        return {
            node["comments"]["nodes"][0]["databaseId"]: node["id"]
            for node in threads
            if not node.get("isResolved") and node.get("comments", {}).get("nodes")
        }

    def resolve_review_thread(self, token: str, thread_node_id: str) -> None:
        """Resolve a pull request review thread via GraphQL."""
        mutation = """
        mutation ResolveThread($threadId: ID!) {
          resolveReviewThread(input: {threadId: $threadId}) {
            thread { isResolved }
          }
        }
        """
        self._post(token, "/graphql", {"query": mutation, "variables": {"threadId": thread_node_id}})

    # --- shared HTTP --------------------------------------------------------

    def _get(
        self,
        token: str,
        path: str,
        params: dict | None = None,
        extra_headers: dict | None = None,
        allow_404: bool = False,
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
        if response.status_code >= 300:
            raise map_github_status(response, action=path)
        return response

    def _post(self, token: str, path: str, json_body: dict) -> httpx.Response:
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

        if response.status_code >= 300:
            raise map_github_status(response, action=path)
        return response

    def close(self) -> None:
        self._client.close()
