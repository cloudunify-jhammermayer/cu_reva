"""Real-git integration tests for ClaudeCodeRunner.two_tree_diff (spec 2026-07-24).

SQLite/mock unit tests (worker/tests/test_claude_code_runner.py) fake or use a
tiny real repo for the two happy/cold-cache cases; this file additionally
exercises the merge-base "base_moved" gate and a genuinely evicted-object
scenario against real `git` end to end, on real local repos under tmp_path.

Unlike worker/tests/test_pg_integration.py (gated on REVA_TEST_POSTGRES_URL —
a real external Postgres service), the only dependency here is the `git`
binary itself, which every worker image already requires for
ensure_repo()/review(). No Docker, no network: `origin` is a local bare repo.

Run directly:
  cd worker && .venv/bin/python -m pytest tests/integration/test_two_tree_delta.py -v
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from reva.claude_code_runner import ClaudeCodeRunner

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(cwd, *args):
    subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True, text=True)


def _rev_parse(cwd, ref="HEAD") -> str:
    return subprocess.run(
        ["git", "-C", cwd, "rev-parse", ref], capture_output=True, text=True
    ).stdout.strip()


def _init_origin_and_work(tmp_path):
    """A bare `origin.git` plus a `work` clone with one base commit on main."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", str(origin), str(work)], check=True, capture_output=True)
    w = str(work)
    _git(w, "config", "user.email", "t@t")
    _git(w, "config", "user.name", "t")
    os.makedirs(work / "custom_addons" / "m", exist_ok=True)
    (work / "custom_addons" / "m" / "a.py").write_text("x = 1\n")
    _git(w, "add", "-A")
    _git(w, "commit", "-m", "base")
    _git(w, "push", "origin", "HEAD:main")
    return origin, work


def _clone_cache(tmp_path, origin) -> str:
    cache = tmp_path / "cache" / "o" / "r"
    os.makedirs(cache.parent, exist_ok=True)
    subprocess.run(["git", "clone", str(origin), str(cache)], check=True, capture_output=True)
    return str(cache)


def test_two_tree_diff_amend_same_base(tmp_path):
    """Pure amend/reword (merge-base with the target unchanged) -> a real
    local two-tree diff, not a full-review fallback."""
    origin, work = _init_origin_and_work(tmp_path)
    w = str(work)

    (work / "custom_addons" / "m" / "b.py").write_text("y = 1\n")
    _git(w, "add", "-A")
    _git(w, "commit", "-m", "feat")
    prior = _rev_parse(w)
    _git(w, "update-ref", "refs/pull/7/head", prior)
    _git(w, "push", "origin", "refs/pull/7/head")

    runner = ClaudeCodeRunner(repo_cache_dir=str(tmp_path / "cache"), api_key="k", skills_dir="s")
    cache = _clone_cache(tmp_path, origin)
    # Prime the cache with `prior` while the PR ref still points at it — exactly
    # what the FIRST review's own two_tree_diff/ensure_repo call would have
    # done. This is what keeps `prior`'s object resident once the ref moves on.
    subprocess.run(
        ["git", "-C", cache, "fetch", "origin", "+refs/pull/7/head:refs/pull/7/head"],
        check=True, capture_output=True,
    )

    (work / "custom_addons" / "m" / "b.py").write_text("y = 2\n")
    _git(w, "add", "-A")
    _git(w, "commit", "--amend", "-m", "feat")
    new = _rev_parse(w)
    _git(w, "update-ref", "refs/pull/7/head", new)
    subprocess.run(["git", "-C", w, "push", "--force", "origin", "refs/pull/7/head"],
                   check=True, capture_output=True)

    diff, reason = runner.two_tree_diff("tok", "o", "r", "main", prior, new, 7)

    assert reason == "ok"
    assert diff is not None
    assert "y = 2" in diff and "b.py" in diff
    assert "a.py" not in diff  # unchanged file absent from a two-tree diff


def test_two_tree_diff_rebase_onto_newer_base_reports_base_moved(tmp_path):
    """A legitimate rebase onto an advanced target branch changes the PR's
    merge-base — this must NOT be treated as a same-PR delta."""
    origin, work = _init_origin_and_work(tmp_path)
    w = str(work)

    _git(w, "checkout", "-b", "feature")
    (work / "custom_addons" / "m" / "b.py").write_text("y = 1\n")
    _git(w, "add", "-A")
    _git(w, "commit", "-m", "feat")
    prior = _rev_parse(w)
    _git(w, "update-ref", "refs/pull/7/head", prior)
    _git(w, "push", "origin", "refs/pull/7/head")

    runner = ClaudeCodeRunner(repo_cache_dir=str(tmp_path / "cache"), api_key="k", skills_dir="s")
    cache = _clone_cache(tmp_path, origin)
    subprocess.run(
        ["git", "-C", cache, "fetch", "origin", "+refs/pull/7/head:refs/pull/7/head"],
        check=True, capture_output=True,
    )

    # Advance main independently (other work lands on the target branch while
    # the PR is open) and keep the cache's view of it in sync, exactly as a
    # normal ensure_repo() `git fetch origin` would between reviews.
    _git(w, "checkout", "main")
    (work / "custom_addons" / "m" / "c.py").write_text("z = 1\n")
    _git(w, "add", "-A")
    _git(w, "commit", "-m", "unrelated main work")
    _git(w, "push", "origin", "HEAD:main")
    subprocess.run(["git", "-C", cache, "fetch", "origin"], check=True, capture_output=True)

    # Rebase the feature commit onto the now-advanced main and force-push.
    _git(w, "checkout", "feature")
    _git(w, "rebase", "main")
    new = _rev_parse(w)
    _git(w, "update-ref", "refs/pull/7/head", new)
    subprocess.run(["git", "-C", w, "push", "--force", "origin", "refs/pull/7/head"],
                   check=True, capture_output=True)

    diff, reason = runner.two_tree_diff("tok", "o", "r", "main", prior, new, 7)

    assert diff is None
    assert reason == "base_moved"


def test_two_tree_diff_evicted_prior_object_reports_missing(tmp_path):
    """A prior_sha whose object was never fetched into (or was pruned from) the
    cache clone -> object_missing, degrading to a full review; never raises."""
    origin, work = _init_origin_and_work(tmp_path)
    w = str(work)

    # A commit that is NEVER pushed/fetched anywhere the cache clone can see —
    # stands in for a force-pushed-away SHA GitHub no longer serves, or one
    # `git gc`-pruned from a long-lived cache clone.
    (work / "custom_addons" / "m" / "b.py").write_text("y = 1\n")
    _git(w, "add", "-A")
    _git(w, "commit", "-m", "feat")
    prior = _rev_parse(w)

    (work / "custom_addons" / "m" / "b.py").write_text("y = 2\n")
    _git(w, "add", "-A")
    _git(w, "commit", "--amend", "-m", "feat")
    new = _rev_parse(w)
    _git(w, "update-ref", "refs/pull/7/head", new)
    _git(w, "push", "origin", "refs/pull/7/head")

    runner = ClaudeCodeRunner(repo_cache_dir=str(tmp_path / "cache"), api_key="k", skills_dir="s")
    _clone_cache(tmp_path, origin)  # fresh clone: `prior` was never resident

    diff, reason = runner.two_tree_diff("tok", "o", "r", "main", prior, new, 7)

    assert diff is None
    assert reason == "object_missing"


def test_two_tree_diff_cold_cache_never_raises(tmp_path):
    """No cache clone at all (first-ever job for this repo, or an evicted repo
    directory) -> cold_cache, never a raised exception."""
    runner = ClaudeCodeRunner(repo_cache_dir=str(tmp_path / "cache"), api_key="k", skills_dir="s")

    diff, reason = runner.two_tree_diff("tok", "o", "r", "main", "dead" * 10, "beef" * 10, 1)

    assert diff is None
    assert reason == "cold_cache"
