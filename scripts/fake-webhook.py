#!/usr/bin/env python3
"""Send a fake pull_request webhook to a local REVA instance.

Usage:
    python scripts/fake-webhook.py [--url URL] [--secret SECRET] [--action ACTION]

The payload is signed with HMAC-SHA256 exactly as GitHub does it.
The worker will receive and enqueue the job; GitHub API calls will fail
unless you supply a real installation_id and repo that match your GitHub App.

For end-to-end testing use ngrok instead (see README).
"""

import argparse
import hashlib
import hmac
import json
import os
import time
import urllib.request

DEFAULT_URL = "http://localhost:8080/webhook/github"
DEFAULT_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "localtest123")


def make_payload(action: str, pr_number: int, installation_id: int, repo: str) -> dict:
    owner, name = repo.split("/", 1)
    sha = "abc123def456abc123def456abc123def456abc123"
    return {
        "action": action,
        "number": pr_number,
        "pull_request": {
            "number": pr_number,
            "title": f"test: fake PR #{pr_number}",
            "state": "open",
            "draft": False,
            "user": {"login": "test-user"},
            "head": {"sha": sha, "ref": f"feat/test-{pr_number}"},
            "base": {"ref": "main", "sha": "0000000000000000000000000000000000000000"},
            "html_url": f"https://github.com/{repo}/pull/{pr_number}",
        },
        "repository": {
            "id": 99999,
            "name": name,
            "full_name": repo,
            "private": True,
            "owner": {"login": owner},
            "default_branch": "main",
        },
        "installation": {"id": installation_id},
        "sender": {"login": "test-user"},
    }


def sign(body: bytes, secret: str) -> str:
    mac = hmac.new(secret.encode(), body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


def send(url: str, payload: dict, secret: str, event: str = "pull_request") -> None:
    body = json.dumps(payload).encode()
    sig = sign(body, secret)
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": f"fake-{int(time.time())}",
            "X-Hub-Signature-256": sig,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body_resp = resp.read().decode()
            print(f"HTTP {resp.status}  {body_resp}")
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}  {e.read().decode()}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Send a fake GitHub PR webhook")
    parser.add_argument("--url", default=DEFAULT_URL, help="Webhook endpoint URL")
    parser.add_argument("--secret", default=DEFAULT_SECRET, help="Webhook secret")
    parser.add_argument("--action", default="opened",
                        choices=["opened", "synchronize", "reopened"],
                        help="PR action (default: opened)")
    parser.add_argument("--pr", type=int, default=1, help="PR number (default: 1)")
    parser.add_argument("--installation-id", type=int, default=0,
                        help="GitHub App installation ID (0 = worker will fail at GitHub API)")
    parser.add_argument("--repo", default="acme/reva-test",
                        help="repo full name (default: acme/reva-test)")
    args = parser.parse_args()

    payload = make_payload(args.action, args.pr, args.installation_id, args.repo)
    print(f"Sending pull_request.{args.action} for {args.repo}#{args.pr} → {args.url}")
    send(args.url, payload, args.secret)


if __name__ == "__main__":
    main()
