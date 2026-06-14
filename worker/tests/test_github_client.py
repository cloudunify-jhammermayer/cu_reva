"""Tests for GitHubClient.

Uses httpx.MockTransport for network isolation. JWT tests use a real
RSA key generated once per session via the `cryptography` package, so we
exercise the actual signing/verification path.
"""

from __future__ import annotations

import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from reva.errors import PermanentError, TransientError
from reva.github_client import GitHubClient


# --- fixtures ----------------------------------------------------------------


@pytest.fixture(scope="session")
def rsa_key_pair() -> tuple[str, str]:
    """Generate one RSA key for the whole test session (~50ms)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


def _make_client(handler, private_pem: str) -> GitHubClient:
    transport = httpx.MockTransport(handler)
    return GitHubClient(
        app_id=12345,
        private_key_pem=private_pem,
        client=httpx.Client(transport=transport),
    )


# --- JWT ---------------------------------------------------------------------


def test_jwt_has_correct_claims(rsa_key_pair):
    private_pem, public_pem = rsa_key_pair
    client = GitHubClient(app_id=12345, private_key_pem=private_pem)
    token_str = client._make_jwt()

    decoded = jwt.decode(token_str, public_pem, algorithms=["RS256"])
    assert decoded["iss"] == "12345"

    now = int(time.time())
    assert decoded["iat"] <= now
    assert decoded["exp"] > now
    assert decoded["exp"] - decoded["iat"] <= 11 * 60  # < GitHub's 10-min limit + skew


# --- Installation token exchange --------------------------------------------


def test_get_installation_token_exchanges_jwt(rsa_key_pair):
    private_pem, _ = rsa_key_pair
    captured: dict = {}

    def handler(req):
        captured["path"] = req.url.path
        captured["auth"] = req.headers.get("authorization", "")
        return httpx.Response(
            201,
            json={"token": "ghs_abc", "expires_at": "2099-12-31T23:59:59Z"},
        )

    client = _make_client(handler, private_pem)
    token = client.get_installation_token(100)

    assert token == "ghs_abc"
    assert captured["path"] == "/app/installations/100/access_tokens"
    assert captured["auth"].startswith("Bearer ")


def test_token_cache_hits_within_ttl(rsa_key_pair):
    private_pem, _ = rsa_key_pair
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(
            201,
            json={"token": "ghs_xyz", "expires_at": "2099-12-31T23:59:59Z"},
        )

    client = _make_client(handler, private_pem)
    client.get_installation_token(200)
    client.get_installation_token(200)
    assert calls["n"] == 1


def test_token_cache_remints_when_expired(rsa_key_pair):
    private_pem, _ = rsa_key_pair
    responses = iter(
        [
            # expires in the past relative to today (2026-05-16) → cache miss
            {"token": "ghs_first", "expires_at": "2026-05-15T00:00:00Z"},
            {"token": "ghs_second", "expires_at": "2099-12-31T23:59:59Z"},
        ]
    )

    def handler(req):
        return httpx.Response(201, json=next(responses))

    client = _make_client(handler, private_pem)
    assert client.get_installation_token(300) == "ghs_first"
    assert client.get_installation_token(300) == "ghs_second"


# --- Pull request reads -----------------------------------------------------


def test_get_file_content_url_encodes_path(rsa_key_pair):
    """CORR-18: a file path with spaces/# must be URL-encoded (slash preserved)
    so the request URL is valid rather than malformed."""
    private_pem, _ = rsa_key_pair
    captured: dict = {}

    def handler(req):
        captured["raw"] = req.url.raw_path.decode()
        return httpx.Response(200, text="file body")

    client = _make_client(handler, private_pem)
    client.get_file_content("tok", "acme", "widgets", "custom addons/foo#bar.py", "deadbeef")

    assert "/repos/acme/widgets/contents/custom%20addons/foo%23bar.py" in captured["raw"]
    assert " " not in captured["raw"]  # no raw space leaked into the URL


def test_get_pull_request_returns_json(rsa_key_pair):
    private_pem, _ = rsa_key_pair
    captured: dict = {}

    def handler(req):
        captured["path"] = req.url.path
        captured["auth"] = req.headers.get("authorization", "")
        return httpx.Response(200, json={"number": 42, "head": {"sha": "abc"}})

    client = _make_client(handler, private_pem)
    pr = client.get_pull_request("test_tok", "acme", "widgets", 42)

    assert pr["number"] == 42
    assert pr["head"]["sha"] == "abc"
    assert captured["path"] == "/repos/acme/widgets/pulls/42"
    assert captured["auth"] == "Bearer test_tok"


def test_get_pull_request_diff_sends_diff_accept_header(rsa_key_pair):
    private_pem, _ = rsa_key_pair
    captured: dict = {}

    def handler(req):
        captured["accept"] = req.headers.get("accept", "")
        return httpx.Response(200, text="diff --git a/x b/x\n+ hi\n")

    client = _make_client(handler, private_pem)
    diff = client.get_pull_request_diff("tok", "acme", "widgets", 1)

    assert diff.startswith("diff --git")
    assert "v3.diff" in captured["accept"]


# --- changed files pagination ----------------------------------------------


def test_get_changed_files_paginates_until_short_batch(rsa_key_pair):
    private_pem, _ = rsa_key_pair
    pages = {
        "1": [{"filename": f"f{i}.py"} for i in range(100)],
        "2": [{"filename": f"f{i}.py"} for i in range(100, 200)],
        "3": [{"filename": f"f{i}.py"} for i in range(200, 230)],
    }
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        page = req.url.params.get("page")
        return httpx.Response(200, json=pages[page])

    client = _make_client(handler, private_pem)
    files = client.get_changed_files("tok", "acme", "widgets", 1)

    assert calls["n"] == 3
    assert len(files) == 230


def test_get_changed_files_caps_at_30_pages(rsa_key_pair):
    private_pem, _ = rsa_key_pair
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(200, json=[{"filename": f"f{i}.py"} for i in range(100)])

    client = _make_client(handler, private_pem)
    files = client.get_changed_files("tok", "acme", "widgets", 1)

    assert calls["n"] == 30
    assert len(files) == 30 * 100


def test_get_changed_files_stops_on_empty_page(rsa_key_pair):
    private_pem, _ = rsa_key_pair
    pages = iter([[{"filename": "a.py"}], []])

    def handler(req):
        return httpx.Response(200, json=next(pages))

    client = _make_client(handler, private_pem)
    files = client.get_changed_files("tok", "acme", "widgets", 1)
    assert files == [{"filename": "a.py"}]


# --- file content -----------------------------------------------------------


def test_get_file_content_returns_raw_text(rsa_key_pair):
    private_pem, _ = rsa_key_pair
    captured: dict = {}

    def handler(req):
        captured["accept"] = req.headers.get("accept", "")
        captured["ref"] = req.url.params.get("ref")
        captured["path"] = req.url.path
        return httpx.Response(200, text="enabled: true\n")

    client = _make_client(handler, private_pem)
    body = client.get_file_content("tok", "acme", "widgets", ".claude-review.yml", "abc")

    assert body == "enabled: true\n"
    assert "raw" in captured["accept"]
    assert captured["ref"] == "abc"
    assert captured["path"].endswith("/contents/.claude-review.yml")


def test_get_file_content_returns_none_on_404(rsa_key_pair):
    private_pem, _ = rsa_key_pair
    client = _make_client(lambda req: httpx.Response(404), private_pem)
    body = client.get_file_content("tok", "acme", "widgets", "MISSING.md", "abc")
    assert body is None


# --- error mapping ----------------------------------------------------------


def test_403_with_zero_rate_limit_maps_to_transient(rsa_key_pair):
    private_pem, _ = rsa_key_pair
    reset_ts = str(int(time.time()) + 90)

    def handler(req):
        return httpx.Response(
            403,
            headers={
                "x-ratelimit-remaining": "0",
                "x-ratelimit-reset": reset_ts,
            },
            text="rate limited",
        )

    client = _make_client(handler, private_pem)
    with pytest.raises(TransientError) as exc_info:
        client.get_pull_request("tok", "acme", "widgets", 1)

    assert exc_info.value.retry_after is not None
    assert 60 < exc_info.value.retry_after <= 95


def test_403_without_rate_limit_maps_to_permanent(rsa_key_pair):
    private_pem, _ = rsa_key_pair
    client = _make_client(lambda req: httpx.Response(403, text="forbidden"), private_pem)
    with pytest.raises(PermanentError) as exc_info:
        client.get_pull_request("tok", "acme", "widgets", 1)
    assert "forbidden" in str(exc_info.value).lower()


def test_500_maps_to_transient(rsa_key_pair):
    private_pem, _ = rsa_key_pair
    client = _make_client(lambda req: httpx.Response(500, text="oops"), private_pem)
    with pytest.raises(TransientError):
        client.get_pull_request("tok", "acme", "widgets", 1)


def test_429_with_retry_after_maps_to_transient(rsa_key_pair):
    private_pem, _ = rsa_key_pair

    def handler(req):
        return httpx.Response(429, headers={"retry-after": "17"}, text="slow")

    client = _make_client(handler, private_pem)
    with pytest.raises(TransientError) as exc_info:
        client.get_pull_request("tok", "acme", "widgets", 1)
    assert exc_info.value.retry_after == 17


def test_401_maps_to_permanent(rsa_key_pair):
    private_pem, _ = rsa_key_pair
    client = _make_client(lambda req: httpx.Response(401, text="bad creds"), private_pem)
    with pytest.raises(PermanentError):
        client.get_pull_request("tok", "acme", "widgets", 1)


def test_404_on_non_file_endpoint_maps_to_permanent(rsa_key_pair):
    private_pem, _ = rsa_key_pair
    client = _make_client(lambda req: httpx.Response(404), private_pem)
    with pytest.raises(PermanentError):
        client.get_pull_request("tok", "acme", "widgets", 999)


def test_timeout_maps_to_transient(rsa_key_pair):
    private_pem, _ = rsa_key_pair

    def handler(req):
        raise httpx.ReadTimeout("read timeout", request=req)

    client = _make_client(handler, private_pem)
    with pytest.raises(TransientError):
        client.get_pull_request("tok", "acme", "widgets", 1)


def test_token_exchange_error_maps_to_permanent(rsa_key_pair):
    private_pem, _ = rsa_key_pair
    client = _make_client(lambda req: httpx.Response(401, text="bad jwt"), private_pem)
    with pytest.raises(PermanentError):
        client.get_installation_token(999)


# --- write methods ----------------------------------------------------------


def test_create_check_run_posts_payload(rsa_key_pair):
    private_pem, _ = rsa_key_pair
    captured: dict = {}

    def handler(req):
        import json
        captured["path"] = req.url.path
        captured["body"] = json.loads(req.content)
        return httpx.Response(201, json={"id": 9876, "status": "completed"})

    client = _make_client(handler, private_pem)
    cr_id = client.create_check_run(
        token="tok",
        owner="acme",
        repo="widgets",
        head_sha="abc123",
        name="REVA Review",
        status="completed",
        conclusion="failure",
        started_at="2026-05-16T10:00:00Z",
        completed_at="2026-05-16T10:02:14Z",
        output={"title": "1 critical", "summary": "x", "text": ""},
    )
    assert cr_id == 9876
    assert captured["path"] == "/repos/acme/widgets/check-runs"
    assert captured["body"]["head_sha"] == "abc123"
    assert captured["body"]["conclusion"] == "failure"
    assert captured["body"]["output"]["title"] == "1 critical"


def test_update_check_run_patches_existing(rsa_key_pair):
    private_pem, _ = rsa_key_pair
    captured: dict = {}

    def handler(req):
        import json
        captured["method"] = req.method
        captured["path"] = req.url.path
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={"id": 555, "status": "completed"})

    client = _make_client(handler, private_pem)
    cr_id = client.update_check_run(
        token="tok",
        owner="acme",
        repo="widgets",
        check_run_id=555,
        status="completed",
        conclusion="success",
        started_at="2026-06-03T10:00:00Z",
        completed_at="2026-06-03T10:02:14Z",
        output={"title": "no findings", "summary": "x", "text": ""},
    )
    assert cr_id == 555
    assert captured["method"] == "PATCH"
    assert captured["path"] == "/repos/acme/widgets/check-runs/555"
    assert "head_sha" not in captured["body"]  # PATCH targets the run by id
    assert captured["body"]["conclusion"] == "success"
    assert captured["body"]["output"]["title"] == "no findings"


def test_create_check_run_omits_conclusion_when_in_progress(rsa_key_pair):
    private_pem, _ = rsa_key_pair
    captured: dict = {}

    def handler(req):
        import json
        captured["body"] = json.loads(req.content)
        return httpx.Response(201, json={"id": 1})

    client = _make_client(handler, private_pem)
    client.create_check_run(
        token="tok", owner="a", repo="b", head_sha="s",
        name="REVA Review", status="in_progress",
        conclusion=None, started_at=None, completed_at=None,
        output={"title": "", "summary": "", "text": ""},
    )
    assert "conclusion" not in captured["body"]


def test_create_pr_review_posts_comments(rsa_key_pair):
    private_pem, _ = rsa_key_pair
    captured: dict = {}

    def handler(req):
        import json
        captured["path"] = req.url.path
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={"id": 12321})

    client = _make_client(handler, private_pem)
    review_id = client.create_pr_review(
        token="tok",
        owner="acme",
        repo="widgets",
        pr_number=42,
        commit_id="deadbeef",
        event="COMMENT",
        body="overall body",
        comments=[{"path": "x.py", "line": 10, "body": "inline"}],
    )
    assert review_id == 12321
    assert captured["path"] == "/repos/acme/widgets/pulls/42/reviews"
    assert captured["body"]["event"] == "COMMENT"
    assert captured["body"]["commit_id"] == "deadbeef"
    assert captured["body"]["comments"][0]["line"] == 10


def test_get_repo_installation_id_uses_app_jwt(rsa_key_pair):
    private_pem, _ = rsa_key_pair
    captured: dict = {}

    def handler(req):
        captured["path"] = req.url.path
        captured["auth"] = req.headers.get("authorization", "")
        return httpx.Response(200, json={"id": 7788, "app_id": 12345})

    client = _make_client(handler, private_pem)
    inst = client.get_repo_installation_id("acme", "widgets")
    assert inst == 7788
    assert captured["path"] == "/repos/acme/widgets/installation"
    # App-JWT auth (a signed JWT), not an installation token.
    assert captured["auth"].startswith("Bearer ")


def test_get_repo_returns_metadata(rsa_key_pair):
    private_pem, _ = rsa_key_pair

    def handler(req):
        assert req.url.path == "/repos/acme/widgets"
        return httpx.Response(200, json={
            "id": 555, "full_name": "acme/widgets", "name": "widgets",
            "owner": {"login": "acme"}, "default_branch": "main",
        })

    client = _make_client(handler, private_pem)
    meta = client.get_repo("tok", "acme", "widgets")
    assert meta["id"] == 555 and meta["default_branch"] == "main"


def test_create_issue_opens_issue(rsa_key_pair):
    private_pem, _ = rsa_key_pair
    captured: dict = {}

    def handler(req):
        import json
        captured["method"] = req.method
        captured["path"] = req.url.path
        captured["body"] = json.loads(req.content)
        return httpx.Response(
            201, json={"number": 77, "html_url": "https://github.com/acme/widgets/issues/77"}
        )

    client = _make_client(handler, private_pem)
    created = client.create_issue(
        token="tok", owner="acme", repo="widgets",
        title="[REVA audit] RCE", body="details\n<!-- revaaudit -->",
        labels=["reva-audit"],
    )
    # url is GitHub's canonical html_url, not reconstructed from owner/repo
    assert created == {"number": 77, "url": "https://github.com/acme/widgets/issues/77"}
    assert captured["method"] == "POST"
    assert captured["path"] == "/repos/acme/widgets/issues"
    assert captured["body"]["title"] == "[REVA audit] RCE"
    assert captured["body"]["labels"] == ["reva-audit"]


def test_ensure_label_creates_when_missing(rsa_key_pair):
    private_pem, _ = rsa_key_pair
    captured: dict = {}

    def handler(req):
        import json
        captured["method"] = req.method
        captured["path"] = req.url.path
        captured["body"] = json.loads(req.content)
        return httpx.Response(201, json={"id": 1, "name": "reva-audit"})

    client = _make_client(handler, private_pem)
    client.ensure_label("tok", "acme", "widgets", "reva-audit",
                        color="5319e7", description="REVA audit findings")
    assert captured["method"] == "POST"
    assert captured["path"] == "/repos/acme/widgets/labels"
    assert captured["body"]["name"] == "reva-audit"


def test_ensure_label_swallows_already_exists(rsa_key_pair):
    private_pem, _ = rsa_key_pair

    def handler(req):
        return httpx.Response(422, json={"message": "Validation Failed",
                                         "errors": [{"code": "already_exists"}]})

    client = _make_client(handler, private_pem)
    # Idempotent: a 422 "already exists" must not raise.
    client.ensure_label("tok", "acme", "widgets", "reva-audit")


def test_get_issue_labels_returns_names(rsa_key_pair):
    private_pem, _ = rsa_key_pair
    captured: dict = {}

    def handler(req):
        captured["method"] = req.method
        captured["path"] = req.url.path
        return httpx.Response(200, json=[{"name": "bug"}, {"name": "reva-risk-high"}])

    client = _make_client(handler, private_pem)
    labels = client.get_issue_labels("tok", "acme", "widgets", 42)
    assert labels == ["bug", "reva-risk-high"]
    assert captured["method"] == "GET"
    assert captured["path"] == "/repos/acme/widgets/issues/42/labels"


def test_add_labels_posts_labels_array(rsa_key_pair):
    private_pem, _ = rsa_key_pair
    captured: dict = {}

    def handler(req):
        import json
        captured["method"] = req.method
        captured["path"] = req.url.path
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json=[{"name": "reva-risk-low"}])

    client = _make_client(handler, private_pem)
    client.add_labels("tok", "acme", "widgets", 42, ["reva-risk-low"])
    assert captured["method"] == "POST"
    assert captured["path"] == "/repos/acme/widgets/issues/42/labels"
    assert captured["body"] == {"labels": ["reva-risk-low"]}


def test_remove_label_deletes_url_encoded_name(rsa_key_pair):
    private_pem, _ = rsa_key_pair
    captured: dict = {}

    def handler(req):
        captured["method"] = req.method
        captured["path"] = req.url.path
        return httpx.Response(200, json=[])

    client = _make_client(handler, private_pem)
    client.remove_label("tok", "acme", "widgets", 42, "reva-risk-high")
    assert captured["method"] == "DELETE"
    assert captured["path"] == "/repos/acme/widgets/issues/42/labels/reva-risk-high"


def test_remove_label_swallows_404(rsa_key_pair):
    private_pem, _ = rsa_key_pair

    def handler(req):
        return httpx.Response(404, json={"message": "Label does not exist"})

    client = _make_client(handler, private_pem)
    # Label not present -> no-op, must not raise.
    client.remove_label("tok", "acme", "widgets", 42, "reva-risk-low")


def test_issue_exists_with_marker_true_when_search_hits(rsa_key_pair):
    private_pem, _ = rsa_key_pair
    captured: dict = {}

    def handler(req):
        captured["path"] = req.url.path
        captured["q"] = req.url.params.get("q")
        return httpx.Response(200, json={"total_count": 1, "items": [{"number": 5}]})

    client = _make_client(handler, private_pem)
    assert client.issue_exists_with_marker("tok", "acme", "widgets", "revaaudit123") is True
    assert captured["path"] == "/search/issues"
    assert "repo:acme/widgets" in captured["q"]
    assert "state:open" in captured["q"]
    assert "revaaudit123" in captured["q"]


def test_issue_exists_with_marker_false_when_no_hits(rsa_key_pair):
    private_pem, _ = rsa_key_pair

    def handler(req):
        return httpx.Response(200, json={"total_count": 0, "items": []})

    client = _make_client(handler, private_pem)
    assert client.issue_exists_with_marker("tok", "acme", "widgets", "revaaudit123") is False


def test_create_issue_comment_posts_body(rsa_key_pair):
    private_pem, _ = rsa_key_pair
    captured: dict = {}

    def handler(req):
        import json
        captured["path"] = req.url.path
        captured["body"] = json.loads(req.content)
        return httpx.Response(201, json={"id": 555})

    client = _make_client(handler, private_pem)
    cid = client.create_issue_comment("tok", "acme", "widgets", 42, "## hi")
    assert cid == 555
    assert captured["path"] == "/repos/acme/widgets/issues/42/comments"
    assert captured["body"]["body"] == "## hi"


def test_find_pr_review_id_matches_marker(rsa_key_pair):
    private_pem, _ = rsa_key_pair
    captured: dict = {}

    def handler(req):
        captured["path"] = req.url.path
        return httpx.Response(200, json=[
            {"id": 1, "body": "someone else's review", "user": {"type": "User"}},
            {"id": 2, "body": "REVA summary\n*REVA · Run #77*", "user": {"type": "Bot"}},
        ])

    client = _make_client(handler, private_pem)
    rid = client.find_pr_review_id("tok", "acme", "widgets", 42, marker="Run #77")
    assert rid == 2
    assert captured["path"] == "/repos/acme/widgets/pulls/42/reviews"


def test_find_pr_review_id_returns_none_when_absent(rsa_key_pair):
    private_pem, _ = rsa_key_pair
    client = _make_client(
        lambda req: httpx.Response(200, json=[{"id": 1, "body": "other", "user": {"type": "Bot"}}]),
        private_pem,
    )
    assert client.find_pr_review_id("tok", "a", "b", 1, marker="Run #77") is None


def test_find_pr_review_id_ignores_non_bot_forged_marker(rsa_key_pair):
    """CORR-5: a non-bot commenter echoing the marker must NOT be matched."""
    private_pem, _ = rsa_key_pair
    client = _make_client(
        lambda req: httpx.Response(200, json=[
            {"id": 9, "body": "haha Run #77", "user": {"type": "User"}},  # forged by attacker
        ]),
        private_pem,
    )
    assert client.find_pr_review_id("tok", "a", "b", 1, marker="Run #77") is None


def test_find_pr_review_id_paginates(rsa_key_pair):
    """CORR-5: the marker may be on a later page once a PR has many reviews."""
    private_pem, _ = rsa_key_pair

    def handler(req):
        page = int(req.url.params.get("page", "1"))
        if page == 1:
            full = [{"id": i, "body": "no marker", "user": {"type": "Bot"}} for i in range(100)]
            return httpx.Response(200, json=full)
        return httpx.Response(200, json=[
            {"id": 777, "body": "REVA · Run #77", "user": {"type": "Bot"}},
        ])

    client = _make_client(handler, private_pem)
    assert client.find_pr_review_id("tok", "a", "b", 1, marker="Run #77") == 777


def test_find_check_run_id_returns_matching_run(rsa_key_pair):
    private_pem, _ = rsa_key_pair
    captured: dict = {}

    def handler(req):
        captured["path"] = req.url.path
        captured["check_name"] = req.url.params.get("check_name")
        return httpx.Response(200, json={
            "total_count": 1,
            "check_runs": [{"id": 4242, "name": "REVA Review"}],
        })

    client = _make_client(handler, private_pem)
    cr_id = client.find_check_run_id("tok", "acme", "widgets", "abc123", "REVA Review")
    assert cr_id == 4242
    assert captured["path"] == "/repos/acme/widgets/commits/abc123/check-runs"
    assert captured["check_name"] == "REVA Review"


def test_find_check_run_id_returns_none_when_absent(rsa_key_pair):
    private_pem, _ = rsa_key_pair
    client = _make_client(
        lambda req: httpx.Response(200, json={"total_count": 0, "check_runs": []}),
        private_pem,
    )
    assert client.find_check_run_id("tok", "a", "b", "sha", "REVA Review") is None


def test_post_422_maps_to_permanent(rsa_key_pair):
    private_pem, _ = rsa_key_pair
    client = _make_client(
        lambda req: httpx.Response(422, text="bad line"),
        private_pem,
    )
    with pytest.raises(PermanentError):
        client.create_pr_review(
            token="tok", owner="a", repo="b", pr_number=1,
            commit_id="s", event="COMMENT", body="x", comments=[],
        )


def test_post_500_maps_to_transient(rsa_key_pair):
    private_pem, _ = rsa_key_pair
    client = _make_client(lambda req: httpx.Response(500, text="boom"), private_pem)
    with pytest.raises(TransientError):
        client.create_issue_comment("tok", "a", "b", 1, "x")


def test_get_compare_diff_returns_diff_text(rsa_key_pair):
    private_pem, _ = rsa_key_pair

    def handler(request: httpx.Request) -> httpx.Response:
        assert "/compare/abc123...def456" in str(request.url)
        assert "diff" in request.headers.get("accept", "")
        return httpx.Response(200, text="diff --git a/foo.py b/foo.py\n+added")

    client = _make_client(handler, private_pem)
    result = client.get_compare_diff("tok", "acme", "widgets", "abc123", "def456")
    assert result.startswith("diff --git")


def test_get_compare_status_reads_json_status(rsa_key_pair):
    private_pem, _ = rsa_key_pair

    def handler(request: httpx.Request) -> httpx.Response:
        assert "/compare/abc123...def456" in str(request.url)
        # JSON read, NOT the v3.diff media type.
        assert "diff" not in request.headers.get("accept", "")
        return httpx.Response(200, json={"status": "diverged", "ahead_by": 2, "behind_by": 5})

    client = _make_client(handler, private_pem)
    assert client.get_compare_status("tok", "acme", "widgets", "abc123", "def456") == "diverged"


def test_get_review_threads_paginates(rsa_key_pair):
    """CORR-8: >100 threads must be followed across pages, not truncated."""
    private_pem, _ = rsa_key_pair
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        cursor = json.loads(request.content)["variables"]["cursor"]
        calls["n"] += 1
        if cursor is None:
            page = {"hasNextPage": True, "endCursor": "C1"}
            node = {"id": "T1", "isResolved": False, "comments": {"nodes": [{"databaseId": 1}]}}
        else:
            assert cursor == "C1"
            page = {"hasNextPage": False, "endCursor": None}
            node = {"id": "T2", "isResolved": False, "comments": {"nodes": [{"databaseId": 2}]}}
        return httpx.Response(200, json={"data": {"repository": {"pullRequest": {
            "reviewThreads": {"pageInfo": page, "nodes": [node]}}}}})

    client = _make_client(handler, private_pem)
    out = client.get_review_threads("tok", "acme", "widgets", 42)
    assert out == {1: "T1", 2: "T2"}
    assert calls["n"] == 2


def test_get_review_threads_returns_database_id_to_node_id_map(rsa_key_pair):
    private_pem, _ = rsa_key_pair

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/graphql"
        return httpx.Response(200, json={
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [
                                {
                                    "id": "THREAD_NODE_1",
                                    "isResolved": False,
                                    "comments": {"nodes": [{"databaseId": 12345}]},
                                },
                                {
                                    "id": "THREAD_NODE_2",
                                    "isResolved": True,
                                    "comments": {"nodes": [{"databaseId": 99999}]},
                                },
                            ]
                        }
                    }
                }
            }
        })

    client = _make_client(handler, private_pem)
    result = client.get_review_threads("tok", "acme", "widgets", 42)
    # Only unresolved threads returned
    assert result == {12345: "THREAD_NODE_1"}


def test_get_review_comments_uses_pr_endpoint_and_filters_by_review(rsa_key_pair):
    """Must use the PR-level comments endpoint (which reports `line`) and filter
    by pull_request_review_id — the /reviews/{id}/comments endpoint returns
    line=null, which breaks location matching (Aurium PR-60 regression)."""
    private_pem, _ = rsa_key_pair
    captured: dict = {}

    def handler(req):
        captured["path"] = req.url.path
        return httpx.Response(200, json=[
            {"id": 11, "path": "a.py", "line": 85, "pull_request_review_id": 100},
            {"id": 12, "path": "b.py", "line": 95, "pull_request_review_id": 100},
            {"id": 99, "path": "c.py", "line": 5, "pull_request_review_id": 200},
        ])

    client = _make_client(handler, private_pem)
    out = client.get_review_comments("tok", "acme", "widgets", 42, review_id=100)

    assert captured["path"] == "/repos/acme/widgets/pulls/42/comments"
    assert [c["id"] for c in out] == [11, 12]  # only review 100, with their lines
    assert out[0]["line"] == 85


def test_resolve_review_thread_posts_graphql_mutation(rsa_key_pair):
    private_pem, _ = rsa_key_pair
    called = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/graphql"
        body = request.read()
        called.append(body)
        return httpx.Response(200, json={
            "data": {"resolveReviewThread": {"thread": {"isResolved": True}}}
        })

    client = _make_client(handler, private_pem)
    client.resolve_review_thread("tok", "THREAD_NODE_1")
    assert len(called) == 1


def test_find_pr_review_id_ignores_reviews_before_since(rsa_key_pair):
    """A stale prior-era review (run_id reused / DB reset → marker collision) must
    NOT be recovered, or a re-review posts nothing. See PR-9 regression."""
    from datetime import datetime, timezone
    private_pem, _ = rsa_key_pair
    client = _make_client(
        lambda req: httpx.Response(200, json=[
            {"id": 4399817713, "body": "## REVA · Review\nRun #1",
             "user": {"type": "Bot"}, "submitted_at": "2026-06-01T08:58:50Z"},
        ]),
        private_pem,
    )
    since = datetime(2026, 6, 3, 7, 0, 0, tzinfo=timezone.utc)
    assert client.find_pr_review_id("tok", "a", "b", 1, marker="Run #1", since=since) is None


def test_find_pr_review_id_recovers_recent_review_with_since(rsa_key_pair):
    """A review from THIS run's era (just-posted, crash-recovery) is still recovered."""
    from datetime import datetime, timezone
    private_pem, _ = rsa_key_pair
    client = _make_client(
        lambda req: httpx.Response(200, json=[
            {"id": 555, "body": "Run #1", "user": {"type": "Bot"},
             "submitted_at": "2026-06-03T07:33:00Z"},
        ]),
        private_pem,
    )
    since = datetime(2026, 6, 3, 7, 0, 0, tzinfo=timezone.utc)
    assert client.find_pr_review_id("tok", "a", "b", 1, marker="Run #1", since=since) == 555


def test_find_issues_with_marker_returns_items_any_state(rsa_key_pair):
    """Reconcile needs the matching issues themselves (number/title/url), open
    AND closed — a completed issue still belongs to its ticket and must be
    re-linked, not re-created."""
    private_pem, _ = rsa_key_pair
    captured: dict = {}

    def handler(req):
        captured["path"] = req.url.path
        captured["q"] = req.url.params.get("q")
        return httpx.Response(200, json={
            "total_count": 2,
            "items": [
                {"number": 5, "title": "Open one", "state": "open",
                 "html_url": "https://github.com/acme/widgets/issues/5"},
                {"number": 3, "title": "Closed one", "state": "closed",
                 "html_url": "https://github.com/acme/widgets/issues/3"},
            ],
        })

    client = _make_client(handler, private_pem)
    issues = client.find_issues_with_marker("tok", "acme", "widgets", "revaticketabc")

    assert captured["path"] == "/search/issues"
    assert "repo:acme/widgets" in captured["q"]
    assert "revaticketabc" in captured["q"]
    assert "state:open" not in captured["q"]
    assert issues == [
        {"number": 5, "title": "Open one",
         "url": "https://github.com/acme/widgets/issues/5", "state": "open"},
        {"number": 3, "title": "Closed one",
         "url": "https://github.com/acme/widgets/issues/3", "state": "closed"},
    ]


def test_find_issues_with_marker_empty(rsa_key_pair):
    private_pem, _ = rsa_key_pair

    def handler(req):
        return httpx.Response(200, json={"total_count": 0, "items": []})

    client = _make_client(handler, private_pem)
    assert client.find_issues_with_marker("tok", "acme", "widgets", "revaticketabc") == []
