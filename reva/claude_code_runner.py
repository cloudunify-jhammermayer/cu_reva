"""Headless Claude Code CLI runner.

Wraps `claude --print` as a subprocess. Manages a local repo cache at
repo_cache_dir/{owner}/{name}/ — clones once, fetches on subsequent calls.

Structured review output is written by Claude to a temp JSON file (the
output path is appended to the task string). REVA reads the file after the
process exits and deletes it regardless of outcome.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from reva.errors import PermanentError, TransientError
from reva.types import ClaudeResponse

DEFAULT_MODEL = "claude-sonnet-4-6"
DEEP_MODEL = "claude-opus-4-7"
_CLAUDE_BIN = "claude"


class ClaudeCodeRunner:
    def __init__(
        self,
        repo_cache_dir: str,
        api_key: str,
        skills_dir: str,
        default_model: str = DEFAULT_MODEL,
        deep_model: str = DEEP_MODEL,
    ) -> None:
        self.repo_cache_dir = repo_cache_dir
        self.api_key = api_key
        self.skills_dir = skills_dir
        self.default_model = default_model
        self.deep_model = deep_model

    # ------------------------------------------------------------------ public

    def ensure_repo(
        self,
        owner: str,
        name: str,
        head_sha: str | None,
        token: str,
    ) -> str:
        """Clone or fetch the repo; checkout head_sha (or reset to FETCH_HEAD if None).

        Returns the absolute path to the working tree.

        Raises:
            TransientError: git clone/fetch failure (network, auth expiry).
            PermanentError: git checkout failure (SHA not found in repo).
        """
        repo_path = os.path.join(self.repo_cache_dir, owner, name)
        clone_url = f"https://x-access-token:{token}@github.com/{owner}/{name}"

        if not os.path.isdir(repo_path):
            os.makedirs(os.path.dirname(repo_path), exist_ok=True)
            self._run_git_transient(["clone", clone_url, repo_path])
        else:
            self._run_git_transient(["-C", repo_path, "remote", "set-url", "origin", clone_url])
            self._run_git_transient(["-C", repo_path, "fetch", "origin"])

        if head_sha:
            self._run_git_permanent(["-C", repo_path, "checkout", head_sha])
        else:
            self._run_git_permanent(["-C", repo_path, "reset", "--hard", "FETCH_HEAD"])

        return repo_path

    def review(
        self,
        repo_path: str,
        skill: str,
        params: dict,
        model: str | None = None,
    ) -> ClaudeResponse:
        raise NotImplementedError("review() is implemented in Task 4")

    # ----------------------------------------------------------------- helpers

    def _create_output_path(self) -> str:
        fd, path = tempfile.mkstemp(suffix=".json", prefix="reva_review_")
        os.close(fd)
        return path

    def _read_skill(self, skill: str) -> str:
        path = os.path.join(self.skills_dir, f"{skill}.md")
        try:
            with open(path) as f:
                return f.read()
        except FileNotFoundError as exc:
            raise PermanentError(f"Skill file not found: {path}") from exc

    @staticmethod
    def _git_subcommand(args: list[str]) -> str:
        """Extract the git subcommand from an args list, skipping -C <path> pairs."""
        i = 0
        while i < len(args):
            if args[i] == "-C":
                i += 2  # skip flag and its value
            elif args[i].startswith("-"):
                i += 1
            else:
                return args[i]
        return args[0]

    def _run_git_transient(self, args: list[str]) -> None:
        """Run a git command; raises TransientError on failure."""
        result = subprocess.run(
            ["git"] + args, capture_output=True, text=True
        )
        if result.returncode != 0:
            cmd = self._git_subcommand(args)
            raise TransientError(f"git {cmd} failed: {result.stderr[:200]}")

    def _run_git_permanent(self, args: list[str]) -> None:
        """Run a git command; raises PermanentError on failure."""
        result = subprocess.run(
            ["git"] + args, capture_output=True, text=True
        )
        if result.returncode != 0:
            cmd = self._git_subcommand(args)
            raise PermanentError(f"git {cmd} failed: {result.stderr[:200]}")


# ---------------------------------------------------------------------- module


def _exit_to_error(returncode: int, stderr: str) -> Exception:
    snippet = stderr[:200]
    if returncode == 1:
        return PermanentError(f"claude exited 1: {snippet}")
    return TransientError(f"claude exited {returncode}: {snippet}")
