# 06 — Review Worker

## Overview

The review worker is the core of the system. It consumes jobs from Redis (RQ), orchestrates the entire review process, and writes results to both PostgreSQL and GitHub.

## Worker Lifecycle per Job

```
1. Claim job from RQ
2. Update review_job status → started
3. Create GitHub installation token
4. Fetch PR metadata from GitHub API
5. Check if head_sha is still current (stale check)
6. Fetch diff from GitHub API
7. Check diff size (>1000 lines → decline)
8. Load .claude-review.yml from repo (via GitHub API)
9. Load CLAUDE.md from repo (via GitHub API)
10. Build Claude prompt
11. Call Claude Messages API
12. Parse structured JSON response
13. Validate findings against schema
14. Create review_run record in Postgres
15. Create review_findings records in Postgres
16. Check head_sha freshness again (pre-post check)
17. Create GitHub Check Run
18. Post PR Review with inline comments
19. Update review_run with GitHub IDs
20. Update review_job status → completed
21. Send Google Chat notification if critical findings or failure
```

## Worker Structure

```
worker/
├── Dockerfile
├── requirements.txt
└── worker/
    ├── __init__.py
    ├── main.py             # RQ worker entry point
    ├── tasks.py            # run_review task function
    ├── reviewer.py         # Core review orchestration
    ├── claude_client.py    # Claude Messages API wrapper
    ├── github_client.py    # GitHub API wrapper (tokens, PRs, reviews)
    ├── diff_parser.py      # Parse unified diff, count lines
    ├── finding_normalizer.py  # Validate and normalize Claude output
    └── config_loader.py    # Load .claude-review.yml and CLAUDE.md
```

## Dependencies (requirements.txt)

```
rq>=1.16.0
redis>=5.0.0
httpx>=0.27.0
sqlalchemy>=2.0.30
psycopg2-binary>=2.9.9
PyJWT>=2.8.0
cryptography>=42.0.0
pydantic>=2.7.0
pyyaml>=6.0.1
structlog>=24.1.0
```

## Claude Client (claude_client.py)

Uses the Claude Messages API directly via httpx. No SDK dependency.

### Structured output via tool_use

REVA does **not** ask Claude to emit raw JSON. Instead, the worker defines a
`submit_review` tool whose `input_schema` is derived from the `ReviewResult`
pydantic model in `worker/types.py`. We force Claude to call this tool
(`tool_choice = {"type":"tool","name":"submit_review"}`) and read the
validated tool input directly. This eliminates regex-based JSON extraction
and the "Claude wrapped the response in markdown fences" failure mode.

See `worker/review_tool.py` for the tool definition.

### Prompt caching

The system prompt is sent as a **list of content blocks** so we can attach
`cache_control: {"type": "ephemeral"}` to each cacheable chunk independently:

- `system.md`  — cached (changes per prompt version).
- `odoo19.md`  — cached (only included for Odoo repos).
- `CLAUDE.md`  — cached per-repo (changes when the file changes in the repo).

The user message (PR title/body, changed files, diff) is **not** cached because
it changes every review. Cache hits cut input cost ~90% on repeated reviews
of the same repo.

### Risk level

REVA does **not** trust Claude's `risk_level` blindly. After capping findings
to the top 15 by severity × confidence, the worker recomputes `risk_level`
deterministically from the surviving set per `pr-review-requirements.md` §4:

| Surviving findings | risk_level |
|---|---|
| any `critical` | `critical` |
| any `major` (no critical) | `high` |
| `>= 3 minor` (no major/critical) | `medium` |
| else | `low` |

This keeps the value consistent even when capping drops findings.

### Retries

Retries are owned by **RQ**, not the client. The client raises:
- `TransientError` (with optional `retry_after`) on 429 + 5xx + network errors
- `PermanentError` on 4xx (other than 429) and malformed responses

`tasks.run_review` is enqueued with `rq.Retry(max=3, interval=[30, 120, 300])`.

```python
import httpx
import json
import structlog

logger = structlog.get_logger()

class ClaudeClient:
    BASE_URL = "https://api.anthropic.com/v1/messages"
    DEFAULT_MODEL = "claude-sonnet-4-6"
    DEEP_MODEL = "claude-opus-4-7"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.client = httpx.Client(timeout=180.0)  # 3 min timeout

    def review(
        self,
        system_prompt: str,
        review_prompt: str,
        model: str | None = None,
        max_tokens: int = 8192,
    ) -> dict:
        """Call Claude Messages API and return parsed response."""
        model = model or self.DEFAULT_MODEL

        response = self.client.post(
            self.BASE_URL,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": max_tokens,
                "system": system_prompt,
                "messages": [
                    {"role": "user", "content": review_prompt},
                ],
            },
        )

        if response.status_code == 429:
            raise TransientError("Claude rate limited", retry_after=response.headers.get("retry-after"))
        if response.status_code >= 500:
            raise TransientError(f"Claude server error: {response.status_code}")
        if response.status_code != 200:
            raise PermanentError(f"Claude API error: {response.status_code} {response.text}")

        data = response.json()

        # Extract text content
        text_blocks = [b["text"] for b in data["content"] if b["type"] == "text"]
        full_text = "\n".join(text_blocks)

        # Extract usage
        usage = data.get("usage", {})

        return {
            "text": full_text,
            "model": data.get("model"),
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "stop_reason": data.get("stop_reason"),
        }

    def close(self):
        self.client.close()
```

## Prompt Assembly

The worker assembles the prompt from multiple sources:

```python
class PromptBuilder:
    def __init__(self, prompts_dir: str = "/app/prompts"):
        self.prompts_dir = prompts_dir

    def build_system_prompt(self, repo_config: dict, claude_md: str | None) -> str:
        """Assemble the system prompt from template + repo context."""
        system = self._read("system.md")

        # Add Odoo 19 context if repo config indicates Odoo
        if repo_config.get("odoo", False) or repo_config.get("framework") == "odoo":
            system += "\n\n" + self._read("odoo19.md")

        # Add CLAUDE.md from repo
        if claude_md:
            system += f"\n\n## Repository-Specific Instructions\n\n{claude_md}"

        return system

    def build_review_prompt(
        self,
        mode: str,
        pr_title: str,
        pr_body: str,
        diff: str,
        changed_files: list[str],
        base_branch: str,
        head_branch: str,
    ) -> str:
        """Assemble the review prompt with PR context."""
        template = self._read(f"{mode}_review.md")

        return template.format(
            pr_title=pr_title,
            pr_body=pr_body or "(no description)",
            diff=diff,
            changed_files="\n".join(f"- {f}" for f in changed_files),
            base_branch=base_branch,
            head_branch=head_branch,
        )

    def get_version(self) -> str:
        """Read prompt version from CHANGELOG.md first line."""
        changelog = self._read("CHANGELOG.md")
        first_line = changelog.strip().split("\n")[0]
        # Expected: "## v1.0 — Initial release"
        return first_line.split("—")[0].replace("##", "").strip()

    def _read(self, filename: str) -> str:
        path = os.path.join(self.prompts_dir, filename)
        with open(path) as f:
            return f.read()
```

## Review Orchestration (reviewer.py)

```python
class Reviewer:
    def __init__(self, claude: ClaudeClient, github: GitHubClient, db, prompt_builder: PromptBuilder):
        self.claude = claude
        self.github = github
        self.db = db
        self.prompts = prompt_builder

    def execute(self, job_params: dict) -> ReviewResult:
        repo_id = job_params["repository_id"]
        pr_id = job_params["pull_request_id"]
        head_sha = job_params["head_sha"]
        installation_id = job_params["installation_id"]
        review_mode = job_params["review_mode"]
        trigger_event = job_params["trigger_event"]

        # Get installation token
        token = self.github.get_installation_token(installation_id)

        # Fetch PR data
        repo = self.db.get_repository(repo_id)
        pr = self.db.get_pull_request(pr_id)
        pr_detail = self.github.get_pull_request(token, repo.owner, repo.name, pr.pr_number)

        # Stale check: is head_sha still current?
        current_sha = pr_detail["head"]["sha"]
        if current_sha != head_sha:
            logger.info("stale_sha", expected=head_sha[:8], current=current_sha[:8])
            return ReviewResult(status="stale", reason="Head SHA changed before review started")

        # Fetch diff
        diff = self.github.get_pull_request_diff(token, repo.owner, repo.name, pr.pr_number)
        changed_files = self.github.get_changed_files(token, repo.owner, repo.name, pr.pr_number)

        # Check diff size
        diff_lines = count_diff_lines(diff)
        if diff_lines > self.prompts.max_diff_lines:
            return ReviewResult(
                status="declined",
                reason=f"Diff too large ({diff_lines} lines > {self.prompts.max_diff_lines} max). "
                       f"Please split this PR into smaller, focused changes.",
            )

        # Load repo config
        repo_config = self.github.get_file_content(
            token, repo.owner, repo.name, ".claude-review.yml", head_sha
        )
        repo_config = yaml.safe_load(repo_config) if repo_config else {}

        # Load CLAUDE.md
        claude_md = self.github.get_file_content(
            token, repo.owner, repo.name, "CLAUDE.md", head_sha
        )

        # Select model
        model = self.claude.DEEP_MODEL if review_mode == "deep" else self.claude.DEFAULT_MODEL

        # Build prompts
        system_prompt = self.prompts.build_system_prompt(repo_config, claude_md)
        review_prompt = self.prompts.build_review_prompt(
            mode=review_mode,
            pr_title=pr.title,
            pr_body=pr_detail.get("body", ""),
            diff=diff,
            changed_files=[f["filename"] for f in changed_files],
            base_branch=pr.base_branch,
            head_branch=pr.head_branch,
        )

        # Call Claude
        started_at = datetime.utcnow()
        response = self.claude.review(system_prompt, review_prompt, model=model)
        completed_at = datetime.utcnow()
        duration_ms = int((completed_at - started_at).total_seconds() * 1000)

        # Parse and validate findings
        findings = self.parse_response(response["text"])

        # Estimate cost
        cost = estimate_cost(model, response["input_tokens"], response["output_tokens"])

        return ReviewResult(
            status="completed",
            model=response["model"],
            prompt_version=self.prompts.get_version(),
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=duration_ms,
            input_tokens=response["input_tokens"],
            output_tokens=response["output_tokens"],
            estimated_cost_usd=cost,
            summary=findings.get("summary", ""),
            risk_level=findings.get("risk_level", "low"),
            findings=findings.get("findings", []),
        )

    def parse_response(self, response: ClaudeResponse) -> dict:
        """Read the validated `submit_review` tool input from Claude's response.

        `claude_client.review()` already pulled the tool_use block out and
        attached it as `response.tool_use_input`. We just validate it against
        the pydantic ReviewResult schema. If Claude failed to call the tool,
        `tool_use_input` is None and we treat that as a permanent error.
        """
        if response.tool_use_input is None:
            raise PermanentError("Claude did not call the submit_review tool")
        return response.tool_use_input
```

## Cost Estimation

```python
# Pricing placeholders — VERIFY against current Anthropic pricing before relying on
# the cost numbers in reporting. Cache reads ≈ 10% of input; 5-minute cache writes ≈ 1.25x input.
PRICING = {
    "claude-sonnet-4-6": {
        "input": 3.00 / 1_000_000,
        "output": 15.00 / 1_000_000,
        "cache_read": 0.30 / 1_000_000,
        "cache_write_5m": 3.75 / 1_000_000,
    },
    "claude-opus-4-7": {
        "input": 15.00 / 1_000_000,
        "output": 75.00 / 1_000_000,
        "cache_read": 1.50 / 1_000_000,
        "cache_write_5m": 18.75 / 1_000_000,
    },
}

def estimate_cost(model, input_tokens, output_tokens, cache_read_tokens=0, cache_write_tokens=0):
    p = PRICING.get(model, PRICING["claude-sonnet-4-6"])
    return round(
        input_tokens * p["input"]
        + output_tokens * p["output"]
        + cache_read_tokens * p["cache_read"]
        + cache_write_tokens * p["cache_write_5m"],
        6,
    )
```

## Diff Size Calculation

```python
def count_diff_lines(diff: str) -> int:
    """Count added/removed lines in unified diff (excluding context lines)."""
    count = 0
    for line in diff.split("\n"):
        if line.startswith("+") and not line.startswith("+++"):
            count += 1
        elif line.startswith("-") and not line.startswith("---"):
            count += 1
    return count
```

## Decline Handling

When a PR is declined (diff too large), the worker:

1. Creates a `review_run` with `status = "declined"` and `decline_reason`.
2. Posts a Check Run with conclusion `neutral` and a message:

```
⚠️ ARIA declined this review.

This PR changes {n} lines, which exceeds the configured maximum of 1000 lines.
Please split this PR into smaller, focused changes for effective review.
```

3. Posts a PR comment with the same message.

## Worker Dockerfile

```dockerfile
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/bash worker
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY worker/ ./worker/
COPY prompts/ ./prompts/

USER worker

CMD ["python", "-m", "worker.main"]
```

## Resource Limits

```yaml
# docker-compose.yml
worker:
  deploy:
    resources:
      limits:
        cpus: "2.0"
        memory: 1G
      reservations:
        cpus: "0.5"
        memory: 256M
```

The main resource consumer is the httpx call to Claude (network I/O, not CPU). Memory usage stays low since we process diffs as strings, not ASTs.
