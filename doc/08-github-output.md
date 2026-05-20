# 08 — GitHub Output

## Overview

After a review completes, the worker publishes results to GitHub through two channels:

1. **Check Run** — a status summary visible in the PR's "Checks" tab. Can block merging.
2. **PR Review** — a grouped review with inline comments on specific lines. Appears in the "Conversation" and "Files changed" tabs.

Both are posted in a single operation at the end of the worker's job, after the stale-check confirms the head SHA is still current.

## Check Run

### Creating a Check Run

```
POST /repos/{owner}/{repo}/check-runs
```

```json
{
  "name": "REVA Review",
  "head_sha": "{head_sha}",
  "status": "completed",
  "conclusion": "failure",
  "started_at": "2026-05-15T10:00:00Z",
  "completed_at": "2026-05-15T10:02:14Z",
  "output": {
    "title": "REVA found 3 issues (1 critical, 2 minor)",
    "summary": "## Review Summary\n\nThis PR introduces a new API endpoint...\n\n| Severity | Count |\n|---|---|\n| Critical | 1 |\n| Major | 0 |\n| Minor | 2 |\n| Info | 0 |\n\n**Risk Level**: high\n\n**Review Duration**: 2m 14s\n**Cost**: $0.042\n**Prompt Version**: v1.0",
    "text": "Full findings details..."
  }
}
```

### Conclusion Mapping

| Review Result | Check Conclusion | Blocks Merge? |
|---|---|---|
| No findings or info only | `success` | No |
| Minor findings only | `neutral` | No |
| Major findings | `failure` | Yes |
| Critical findings | `failure` | Yes |
| Review declined (diff too large) | `neutral` | No |
| Worker crashed / permanent error | `failure` | Yes (safety) |
| Review skipped (draft, stale) | `skipped` | No |

### Enabling Branch Protection

To make Check Run conclusions actually block merging:

1. Go to repo Settings → Branches → Branch protection rules.
2. Edit the rule for `main` (or your default branch).
3. Enable "Require status checks to pass before merging."
4. Search for "REVA Review" and add it as a required check.

Now PRs with `failure` conclusion cannot be merged until the issues are resolved and a new review passes.

## PR Review

### Creating a PR Review with Inline Comments

```
POST /repos/{owner}/{repo}/pulls/{pr_number}/reviews
```

```json
{
  "commit_id": "{head_sha}",
  "event": "COMMENT",
  "body": "## 🔍 REVA Review\n\n{summary}\n\n---\n*REVA v1.0 | claude-sonnet-4 | 2m 14s | $0.042*",
  "comments": [
    {
      "path": "src/controllers/main.py",
      "line": 42,
      "body": "### 🔴 Critical: SQL Injection Risk\n\n**Confidence**: 0.95\n\nThis query uses string formatting to build SQL...\n\n**Suggestion**:\n```python\nself.env.cr.execute(\"SELECT id FROM res_partner WHERE name = %s\", [name])\n```"
    },
    {
      "path": "src/models/sale_order.py",
      "line": 87,
      "body": "### 🟡 Minor: Missing @api.depends\n\n**Confidence**: 0.80\n\nThe compute method `_compute_total` reads `line_ids.price_subtotal` but..."
    }
  ]
}
```

### Review Event Types

| Event | When to use |
|---|---|
| `COMMENT` | Default — posts findings without approving or requesting changes |
| `REQUEST_CHANGES` | Could be used for critical findings, but risks annoying developers. Use `COMMENT` and let the Check Run handle blocking. |
| `APPROVE` | Never — REVA should not auto-approve PRs |

Use `COMMENT` always. The Check Run handles the blocking. This avoids the UX friction of "dismiss review" that `REQUEST_CHANGES` creates.

### Comment Formatting

Each inline comment follows a consistent template:

```markdown
### {severity_emoji} {severity}: {title}

**Confidence**: {confidence}
**Category**: {category}

{body}

{suggestion_block}
```

Severity emoji mapping:

| Severity | Emoji |
|---|---|
| critical | 🔴 |
| major | 🟠 |
| minor | 🟡 |
| info | 🔵 |

Suggestion block (only if suggestion is provided):

```markdown
**Suggestion**:
```{language}
{suggestion}
```
```

### Review Body Template

The PR review body (not inline — the top-level review message) uses this structure:

```markdown
## 🔍 REVA Review

{summary}

### Findings Summary

| Severity | Count |
|---|---|
| 🔴 Critical | {critical_count} |
| 🟠 Major | {major_count} |
| 🟡 Minor | {minor_count} |
| 🔵 Info | {info_count} |

**Risk Level**: {risk_level}

{unmapped_findings_section}

---
*REVA {prompt_version} | {model} | {duration} | ${cost} | [Run #{review_run_id}]*
*React with 👍 or 👎 on individual comments to help me improve.*
```

The "unmapped findings" section contains any findings where `file` is null — these are general observations that couldn't be mapped to a specific line.

## Line Mapping

GitHub inline comments require the `line` number to be within the diff hunk. If a finding references a line that's not in the diff, the comment can't be posted inline.

The worker maps findings to diff lines:

```python
def map_finding_to_diff_line(finding: dict, diff_hunks: list[DiffHunk]) -> int | None:
    """Map a finding's line number to a diff-relative position.
    Returns the line number if it's within a changed hunk, None otherwise."""
    file_path = finding.get("file")
    line = finding.get("line_start")

    if not file_path or not line:
        return None

    for hunk in diff_hunks:
        if hunk.file_path == file_path:
            if hunk.new_start <= line <= hunk.new_start + hunk.new_count:
                return line

    return None
```

Findings that can't be mapped to a diff line are included in the review body instead of as inline comments.

## Idempotency and Deduplication

The worker checks before posting:

1. **Same SHA, same mode**: If a `review_run` already has `check_run_id` and `review_id` set, skip posting. This prevents duplicate reviews on retry.
2. **Stale SHA**: Before posting, re-fetch the PR and verify `head_sha` matches. If it doesn't, mark the review as `stale` and don't post.

```python
def should_post(review_run, current_pr_sha: str) -> bool:
    if review_run.check_run_id is not None:
        return False  # already posted
    if review_run.head_sha != current_pr_sha:
        return False  # stale
    return True
```

## Decline Message

When a PR is declined due to diff size:

```markdown
## ⚠️ REVA Review — Declined

This PR changes **{diff_lines}** lines, which exceeds the configured maximum of **{max_lines}** lines.

Large PRs are harder to review effectively. Please consider splitting this into smaller, focused PRs:
- Separate refactoring from feature changes
- Split backend and frontend changes
- Break large migrations into incremental steps

You can trigger a review on a smaller PR by pushing changes or commenting `/review`.

---
*REVA {prompt_version}*
```

## Rate Limit Awareness

The worker tracks GitHub API rate limit headers:

```python
def check_rate_limit(response: httpx.Response):
    remaining = int(response.headers.get("X-RateLimit-Remaining", 999))
    reset_at = int(response.headers.get("X-RateLimit-Reset", 0))

    if remaining < 100:
        logger.warning("github_rate_limit_low", remaining=remaining, reset_at=reset_at)

    if remaining < 10:
        wait_seconds = max(reset_at - int(time.time()), 0) + 5
        logger.warning("github_rate_limit_critical", waiting=wait_seconds)
        time.sleep(wait_seconds)
```
