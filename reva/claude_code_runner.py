"""Headless Claude Code CLI runner.

Wraps `claude --print` as a subprocess. Manages a local repo cache at
repo_cache_dir/{owner}/{name}/ — clones once, fetches on subsequent calls.

Structured review output is written by Claude to a temp JSON file (the
output path is appended to the task string). REVA reads the file after the
process exits and deletes it regardless of outcome.
"""

from __future__ import annotations

import base64
import fcntl
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import structlog

from reva.errors import PermanentError, TransientError
from reva.types import ClaudeResponse

logger = structlog.get_logger()

# owner/name become path segments under the repo cache; constrain them to safe
# GitHub-style identifiers (must start alphanumeric; no separators or "..") so a
# malformed/forged repo identity can never escape the cache dir.
_SAFE_REPO_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _validate_repo_component(value: str, kind: str) -> None:
    if not _SAFE_REPO_COMPONENT.match(value or "") or ".." in value:
        raise PermanentError(f"unsafe repo {kind}: {value!r}")

DEFAULT_MODEL = "claude-sonnet-4-6"
DEEP_MODEL = "claude-opus-4-7"
_CLAUDE_BIN = "claude"

# Repo-aware skills that benefit from a pre-indexed code graph (full/deep reviews
# and audits reason across files). The diff/delta paths are cost-sensitive and
# don't traverse the repo, so they stay off CodeGraph. See the engine-layer spec.
_CODEGRAPH_SKILLS = frozenset({"reva-full-review", "reva-repo-audit"})
_CODEGRAPH_INDEX_TIMEOUT = 180  # seconds; bound the index step like a git op
# Stdio MCP server config handed to the Claude CLI via --mcp-config. The server
# runs in the CLI's cwd (the clone) and finds .codegraph/codegraph.db there.
_CODEGRAPH_MCP_CONFIG = {
    "mcpServers": {
        "codegraph": {"type": "stdio", "command": "codegraph", "args": ["serve", "--mcp"]}
    }
}
SUBPROCESS_TIMEOUT = 1500  # seconds; large PRs can take 10–15 minutes

# Headroom for git clone/fetch + GitHub posting that bracket the subprocess
# inside one RQ job. The RQ job_timeout MUST exceed SUBPROCESS_TIMEOUT, or the
# work-horse is SIGKILLed mid-review (wasting spend, losing the result). Every
# enqueue of a review/audit job derives its timeout from REVIEW_JOB_TIMEOUT so
# the two can never drift apart again.
JOB_TIMEOUT_BUFFER = 300
REVIEW_JOB_TIMEOUT = SUBPROCESS_TIMEOUT + JOB_TIMEOUT_BUFFER  # 1800s

# Bound every git op (clone/fetch/checkout/reset). Held under the per-repo
# flock, so an unbounded git that hangs on the network would stall every job
# for that repo until a container restart.
_GIT_TIMEOUT = 300  # seconds

# Only these host env vars are forwarded to the Claude CLI subprocess. The
# worker's secrets (DATABASE_URL, REDIS_URL, GITHUB_*, ODOO_*) must NOT leak into
# its environment. ANTHROPIC_API_KEY and HOME are injected explicitly below.
# The *_PROXY vars are forwarded so the subprocess's Anthropic traffic routes
# through the egress allowlist proxy when one is configured (A2 egress lock).
_ENV_ALLOWLIST = (
    "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
)


class ClaudeCodeRunner:
    def __init__(
        self,
        repo_cache_dir: str,
        api_key: str,
        skills_dir: str,
        prompts_dir: str | None = None,
        default_model: str = DEFAULT_MODEL,
        deep_model: str = DEEP_MODEL,
        codegraph_enabled: bool = False,
        codegraph_index_timeout: int = _CODEGRAPH_INDEX_TIMEOUT,
    ) -> None:
        self.repo_cache_dir = repo_cache_dir
        self.api_key = api_key
        self.skills_dir = skills_dir
        # Shared governance + domain rules prepended to every review skill. None
        # disables the preamble (used in tests).
        self.prompts_dir = prompts_dir
        self.default_model = default_model
        self.deep_model = deep_model
        # When enabled, repo-aware reviews get a pre-indexed CodeGraph exposed via
        # MCP (cheaper, more cross-file-aware). Default off; pinned/validated first.
        self.codegraph_enabled = codegraph_enabled
        self.codegraph_index_timeout = codegraph_index_timeout

    # ------------------------------------------------------------------ public

    @contextmanager
    def repo_lock(self, owner: str, name: str):
        """Exclusive per-repo lock spanning ensure_repo + review.

        The working tree at repo_cache_dir/{owner}/{name} is shared across jobs.
        Without this, two concurrent jobs (e.g. two PRs, or a PR + an audit)
        would `git checkout` over each other and review the wrong SHA. Held as
        a flock on a sibling lock file so it works across worker processes
        sharing the repo-cache volume. Different repos never block each other.
        """
        _validate_repo_component(owner, "owner")
        _validate_repo_component(name, "name")
        lock_dir = os.path.join(self.repo_cache_dir, owner)
        os.makedirs(lock_dir, exist_ok=True)
        lock_path = os.path.join(lock_dir, f".{name}.lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

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
        _validate_repo_component(owner, "owner")
        _validate_repo_component(name, "name")
        repo_path = os.path.join(self.repo_cache_dir, owner, name)
        # Authenticate via a transient http.extraHeader instead of embedding the
        # token in the remote URL, so it is never written to <repo>/.git/config
        # (which the Claude CLI subprocess can Read). The stored remote stays
        # token-less; the token lives only in this process's argv during the op.
        clean_url = f"https://github.com/{owner}/{name}"
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        auth_args = ["-c", f"http.extraHeader=Authorization: Basic {basic}"]

        if not os.path.isdir(repo_path):
            os.makedirs(os.path.dirname(repo_path), exist_ok=True)
            self._run_git_transient(auth_args + ["clone", clean_url, repo_path])
        else:
            # Ensure any token-bearing URL from older clones is scrubbed.
            self._run_git_transient(["-C", repo_path, "remote", "set-url", "origin", clean_url])
            self._run_git_transient(auth_args + ["-C", repo_path, "fetch", "origin"])

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
        """Run `claude --print` in repo_path using a skill template.

        Reads prompts/skills/{skill}.md, appends task parameters and a temp
        output path, runs the CLI, reads the JSON written by Claude, and
        returns a ClaudeResponse with tool_use_input set to that JSON.

        Raises:
            PermanentError: non-zero exit code 1, or Claude wrote no valid JSON.
            TransientError: non-zero exit code other than 1 (killed, OOM, etc.).
        """
        self._scrub_clone(repo_path)
        output_path = self._create_output_path(repo_path)
        preamble = self._build_preamble()
        skill_content = self._read_skill(skill)
        body = f"{preamble}\n\n{skill_content}" if preamble else skill_content
        # XML-delimit each value so user-controlled content (pr_body, diff, etc.)
        # cannot be confused with task instructions by the model.
        param_lines = "\n".join(f"<{k}>\n{v}\n</{k}>" for k, v in params.items())
        task = (
            f"{body}\n\n"
            f"## Task Parameters\n\n"
            f"{param_lines}\n\n"
            f"output_path: {output_path}"
        )
        env = {k: os.environ[k] for k in _ENV_ALLOWLIST if k in os.environ}
        env["ANTHROPIC_API_KEY"] = self.api_key
        env["HOME"] = "/home/worker"
        # Repo-aware reviews get the CodeGraph MCP server when enabled. Returns
        # None (and we run a plain review) if disabled, not repo-aware, or the
        # index/setup failed — the accelerator is never allowed to block a review.
        mcp_config_path = None
        if self.codegraph_enabled and skill in _CODEGRAPH_SKILLS:
            mcp_config_path = self._codegraph_prepare(repo_path)
        # The allowlist IS the security boundary — note there is NO
        # --dangerously-skip-permissions (which would bypass it). In --print mode
        # any tool not pre-allowed is denied (nothing to prompt), so an injected
        # instruction in the diff/repo can only Read/Grep/Glob and Write — never
        # Bash or the network. Write is unscoped because this CLI ignores a
        # Write(<path>) rule in --print mode; instead Claude Code's workspace
        # boundary confines writes to the cwd (the clone), where the output file
        # is created. The clone is ephemeral and never pushed, so that is a safe
        # sandbox. The mcp__codegraph__* tools are read-only graph queries against
        # a local stdio subprocess — no new write/exec/network capability.
        allowed_tools = "Read,Grep,Glob,Write"
        mcp_args: list[str] = []
        if mcp_config_path:
            mcp_args = ["--mcp-config", mcp_config_path]
            allowed_tools = "Read,Grep,Glob,Write,mcp__codegraph__*"
        try:
            proc = subprocess.run(
                [
                    _CLAUDE_BIN, "--print",
                    "--output-format", "json",
                    "--model", model or self.default_model,
                    # SECU-1 defense-in-depth beside _scrub_clone: ignore the
                    # clone's project setting sources (blocks .claude/settings.json
                    # hooks, which execute as the worker user) and honour only
                    # REVA's own --mcp-config (blocks the clone's .mcp.json).
                    "--setting-sources", "user",
                    "--strict-mcp-config",
                    *mcp_args,
                    "--allowedTools", allowed_tools,
                ],
                input=task,
                cwd=repo_path,
                env=env,
                capture_output=True,
                text=True,
                timeout=SUBPROCESS_TIMEOUT,
            )
            if proc.returncode != 0:
                raise _exit_to_error(proc.returncode, proc.stderr or proc.stdout)

            try:
                with open(output_path) as f:
                    tool_use_input = json.load(f)
            except FileNotFoundError as exc:
                raise PermanentError(
                    f"Claude did not create output file at {output_path}"
                ) from exc
            except json.JSONDecodeError as exc:
                raise PermanentError(
                    f"Claude wrote invalid JSON to {output_path}: {exc}"
                ) from exc

            usage: dict = {}
            total_cost_usd = 0.0
            try:
                result_json = json.loads(proc.stdout)
                usage = result_json.get("usage") or {}
                total_cost_usd = float(result_json.get("total_cost_usd") or 0.0)
            except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
                pass

            return ClaudeResponse(
                model=model or self.default_model,
                stop_reason="tool_use",
                tool_use_input=tool_use_input,
                input_tokens=int(usage.get("input_tokens", 0)),
                output_tokens=int(usage.get("output_tokens", 0)),
                cache_read_tokens=int(usage.get("cache_read_input_tokens", 0)),
                cache_creation_tokens=int(usage.get("cache_creation_input_tokens", 0)),
                total_cost_usd=total_cost_usd,
            )
        finally:
            Path(output_path).unlink(missing_ok=True)
            if mcp_config_path:
                Path(mcp_config_path).unlink(missing_ok=True)

    # ----------------------------------------------------------------- helpers

    def _codegraph_prepare(self, repo_path: str) -> str | None:
        """Index the clone with CodeGraph and write an MCP config for the CLI.

        `init` builds the graph the first time; `sync` refreshes an existing
        `.codegraph/` index incrementally. Bounded by codegraph_index_timeout
        (held under repo_lock, like a git op). Any failure — missing binary,
        non-zero exit, timeout — logs a warning and returns None so the review
        runs without CodeGraph rather than failing.
        """
        subcommand = "sync" if os.path.isdir(os.path.join(repo_path, ".codegraph")) else "init"
        try:
            result = subprocess.run(
                ["codegraph", subcommand, repo_path],
                capture_output=True,
                text=True,
                timeout=self.codegraph_index_timeout,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("codegraph_index_skipped", repo=repo_path, error=str(exc))
            return None
        if result.returncode != 0:
            logger.warning(
                "codegraph_index_failed", repo=repo_path, stderr=(result.stderr or "")[:200]
            )
            return None
        # Written inside the clone (cwd) like the output file — ephemeral, removed
        # after the run. The CLI passes it via --mcp-config.
        fd, path = tempfile.mkstemp(suffix=".json", prefix=".reva_mcp_", dir=repo_path)
        with os.fdopen(fd, "w") as f:
            json.dump(_CODEGRAPH_MCP_CONFIG, f)
        return path

    # Config files the Claude CLI auto-loads from cwd. The clone is fully
    # attacker-controlled, so a PR can ship these to gain code execution as the
    # worker user (.mcp.json MCP servers + .claude/ settings.json hooks). REVA
    # never relies on repo-supplied versions of these — its own prompt set and
    # the GitHub-API-loaded .claude-review.yml are the only sanctioned config —
    # so they are deleted from the clone before every CLI invocation. REVA's own
    # artifacts (.codegraph index, .reva_* temp files, source) are not listed.
    _SCRUB_NAMES = (".mcp.json", ".claude", ".claude.json", "CLAUDE.md", "AGENTS.md")

    def _scrub_clone(self, repo_path: str) -> None:
        """Delete repo-supplied Claude CLI config from the clone (SECU-1).

        Runs every review because the checkout re-materialises the attacker's
        files at the PR head SHA. Best-effort per entry: a deletion failure is
        logged but never blocks the review (the CLI flags are the backstop).
        """
        for name in self._SCRUB_NAMES:
            target = os.path.join(repo_path, name)
            try:
                if os.path.isdir(target) and not os.path.islink(target):
                    shutil.rmtree(target)
                elif os.path.lexists(target):
                    os.unlink(target)
            except OSError as exc:
                logger.warning("scrub_clone_failed", path=target, error=str(exc))

    def _create_output_path(self, dir_: str) -> str:
        # Created INSIDE the cloned repo (the CLI's cwd). Claude Code confines
        # writes to its working directory, so without --dangerously-skip-permissions
        # the output must live under cwd. The clone is ephemeral and never pushed,
        # so a REVA-owned temp file here is harmless; it's removed after the run.
        fd, path = tempfile.mkstemp(suffix=".json", prefix=".reva_review_", dir=dir_)
        os.close(fd)
        return path

    # Shared governance (path-agnostic) + Odoo rules, prepended to every skill.
    _PREAMBLE_FILES = ("review_guidance.md", "odoo19.md")

    def _build_preamble(self) -> str:
        """Concatenate the shared review-guidance + Odoo-rules files.

        Best-effort: a missing file degrades the review (logged) but never
        breaks it, so reviews keep working if the prompt set is incomplete.
        """
        if not self.prompts_dir:
            return ""
        parts: list[str] = []
        for fname in self._PREAMBLE_FILES:
            try:
                with open(os.path.join(self.prompts_dir, fname)) as f:
                    parts.append(f.read())
            except FileNotFoundError:
                pass
        return "\n\n".join(parts)

    def _read_skill(self, skill: str) -> str:
        path = os.path.join(self.skills_dir, f"{skill}.md")
        try:
            with open(path) as f:
                return f.read()
        except FileNotFoundError as exc:
            raise PermanentError(f"Skill file not found: {path}") from exc

    @staticmethod
    def _git_subcommand(args: list[str]) -> str:
        """Extract the git subcommand, skipping `-C <path>` / `-c <key=val>` pairs.

        `-c` is skipped as a pair so a secret-bearing value (e.g. the auth
        extraHeader) is never returned and surfaced in an error message/log.
        """
        i = 0
        while i < len(args):
            if args[i] in ("-C", "-c"):
                i += 2
            elif args[i].startswith("-"):
                i += 1
            else:
                return args[i]
        return args[0]

    def _run_git(self, args: list[str], error_class: type[Exception]) -> None:
        cmd = self._git_subcommand(args)
        try:
            result = subprocess.run(
                ["git"] + args,
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            # A timeout is always transient (network/load), regardless of the
            # caller's error_class — retrying is the right move, and it must not
            # be allowed to hang forever under the per-repo lock.
            raise TransientError(
                f"git {cmd} timed out after {_GIT_TIMEOUT}s"
            ) from exc
        if result.returncode != 0:
            raise error_class(f"git {cmd} failed: {result.stderr[:200]}")

    def evict_stale_repos(self, ttl_days: int) -> None:
        """Remove repo directories not accessed within ttl_days."""
        cache = Path(self.repo_cache_dir)
        if not cache.exists():
            return
        cutoff = time.time() - ttl_days * 86400
        for owner_dir in cache.iterdir():
            if not owner_dir.is_dir():
                continue
            for repo_dir in owner_dir.iterdir():
                if repo_dir.is_dir() and repo_dir.stat().st_mtime < cutoff:
                    # Hold the per-repo lock so we never rmtree a tree a
                    # concurrent worker is mid-review on.
                    with self.repo_lock(owner_dir.name, repo_dir.name):
                        shutil.rmtree(repo_dir)

    def _run_git_transient(self, args: list[str]) -> None:
        self._run_git(args, TransientError)

    def _run_git_permanent(self, args: list[str]) -> None:
        self._run_git(args, PermanentError)


# ---------------------------------------------------------------------- module


def _exit_to_error(returncode: int, stderr: str) -> Exception:
    snippet = stderr[:200]
    if returncode == 1:
        return PermanentError(f"claude exited 1: {snippet}")
    return TransientError(f"claude exited {returncode}: {snippet}")
