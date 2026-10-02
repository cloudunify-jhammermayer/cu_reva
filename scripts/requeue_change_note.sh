#!/usr/bin/env bash
# Manually-triggered: enqueue the merge change-note job for a merged PR whose
# merge webhook did not enqueue it (e.g. merged before its ticket reference was
# recognised). Run on the prod host:
#
#   cd ~/cu_reva && ./scripts/requeue_change_note.sh <owner/repo> <pr-number>
#
# Reads the PR from GitHub and enqueues the same job the webhook would have.
# The repo's `change_notes` switch is not consulted. Safe to re-run: the job
# reuses its change_notes rows and a delivered note is not sent again.

set -euo pipefail
cd "$(dirname "$0")/.."

[ $# -eq 2 ] || { echo "usage: $0 <owner/repo> <pr-number>"; exit 1; }

docker compose -f docker-compose.prod.yml exec -T -e REPO="$1" -e PR="$2" api python - <<'PY'
import os
import sys

from redis import Redis
from rq import Queue, Retry

from app.settings import Settings
from reva.github_client import GitHubClient

repo = os.environ["REPO"].lower()
owner, name = repo.split("/", 1)
pr_number = int(os.environ["PR"])

settings = Settings.from_env()
github = GitHubClient(app_id=settings.github_app_id, private_key_pem=settings.github_private_key)
installation_id = github.get_repo_installation_id(owner, name)
pr = github.get_pull_request(github.get_installation_token(installation_id), owner, name, pr_number)
if not pr.get("merged"):
    sys.exit(f"{repo}#{pr_number} is not merged, nothing enqueued")

job = Queue(settings.queue_name, connection=Redis.from_url(settings.redis_url)).enqueue(
    "worker.change_note_tasks.run_change_note",
    {
        "repo_full_name": repo,
        "pr_number": pr_number,
        "pr_title": pr.get("title") or "",
        "pr_body": pr.get("body") or "",
        "pr_url": pr.get("html_url") or "",
        "head_ref": (pr.get("head") or {}).get("ref") or "",
        "installation_id": installation_id,
    },
    retry=Retry(max=3, interval=[30, 120, 300]),
)
print(f"enqueued {job.id} for {repo}#{pr_number}: {pr.get('title')}")
PY
