# 11 — Notifications and Alerting

## Overview

Notifications serve two purposes:

1. **Alerts**: Something went wrong (worker failure, repeated errors, permissions revoked).
2. **Critical findings**: A review found critical or high-risk issues that need immediate attention.

## Google Chat Incoming Webhook

### Setup

1. Open the Google Chat space where you want notifications.
2. Click the space name → "Manage webhooks."
3. Click "Add webhook."
4. Name it "ARIA PR Reviewer."
5. Copy the webhook URL.
6. Add it to your `.env`:

```
GOOGLE_CHAT_WEBHOOK_URL=https://chat.googleapis.com/v1/spaces/SPACE_ID/messages?key=KEY&token=TOKEN
```

### Sending Messages

```python
import httpx
import structlog

logger = structlog.get_logger()

class GoogleChatNotifier:
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url
        self.client = httpx.Client(timeout=10.0)

    def send(self, text: str, thread_key: str | None = None) -> bool:
        if not self.webhook_url:
            return False

        url = self.webhook_url
        if thread_key:
            url += f"&threadKey={thread_key}&messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"

        try:
            resp = self.client.post(url, json={"text": text})
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error("gchat_notification_failed", error=str(e))
            return False
```

### Thread Keys

Use thread keys to group related messages. For example, all notifications about the same PR go into the same thread:

```python
thread_key = f"review-{repo_full_name}-{pr_number}"
```

## Alert Rules

### When to Notify

| Event | Notify? | Urgency |
|---|---|---|
| Review completed with critical findings | Yes | High |
| Review failed (all retries exhausted) | Yes | High |
| Worker down (health check fails) | Yes | High |
| Review completed with major findings | Optional | Medium |
| GitHub permissions error (403) | Yes | High |
| Claude API quota exceeded | Yes | High |
| Diff declined (too large) | No | — |
| Review completed clean | No | — |

### Message Templates

**Critical Findings Alert:**

```
🔴 *ARIA Critical Finding*

*Repo:* org/odoo-mod
*PR:* #94 — Add partner validation endpoint
*Author:* charlie
*Findings:* 1 critical, 2 major

> SQL injection risk in src/controllers/partner.py:42

🔗 https://github.com/org/odoo-mod/pull/94
```

**Review Failure Alert:**

```
❌ *ARIA Review Failed*

*Repo:* org/api-svc
*PR:* #77 — Update payment flow
*Error:* Claude API 503 — Service unavailable
*Attempts:* 3/3 (all exhausted)

This PR will not be reviewed until manually retriggered with `/review`.
```

**Permissions Error:**

```
🔑 *ARIA Permissions Error*

*Repo:* org/webapp
*Error:* GitHub API returned 403 Forbidden

The GitHub App may have been uninstalled or permissions were changed.
Please check the app installation at:
https://github.com/organizations/org/settings/installations
```

### Implementation in Worker

```python
def post_review_notification(self, review_run, findings, repo, pr):
    """Send notification if review has critical/major findings."""
    critical = sum(1 for f in findings if f["severity"] == "critical")
    major = sum(1 for f in findings if f["severity"] == "major")

    if critical == 0 and major == 0:
        return

    top_finding = next(
        (f for f in findings if f["severity"] == "critical"),
        next((f for f in findings if f["severity"] == "major"), None),
    )

    emoji = "🔴" if critical > 0 else "🟠"
    text = (
        f"{emoji} *ARIA {'Critical' if critical else 'Major'} Finding*\n\n"
        f"*Repo:* {repo.full_name}\n"
        f"*PR:* #{pr.pr_number} — {pr.title}\n"
        f"*Author:* {pr.author_login}\n"
        f"*Findings:* {critical} critical, {major} major\n"
    )

    if top_finding:
        text += f"\n> {top_finding['title']}\n"

    text += f"\n🔗 https://github.com/{repo.full_name}/pull/{pr.pr_number}"

    self.notifier.send(text, thread_key=f"review-{repo.full_name}-{pr.pr_number}")

def post_failure_notification(self, job, repo, pr, error):
    """Send notification on review failure after all retries."""
    text = (
        f"❌ *ARIA Review Failed*\n\n"
        f"*Repo:* {repo.full_name}\n"
        f"*PR:* #{pr.pr_number} — {pr.title}\n"
        f"*Error:* {error}\n"
        f"*Attempts:* {job.attempts}/{job.max_attempts} (all exhausted)\n\n"
        f"This PR will not be reviewed until manually retriggered with `/review`."
    )

    self.notifier.send(text, thread_key=f"failure-{repo.full_name}-{pr.pr_number}")
```

## TUI Failures View

The TUI's Failures view (doc 10) serves as the secondary notification channel. It shows all failed reviews with error details, sortable by time. The TUI refreshes on `[r]` or auto-refreshes every 30 seconds on the dashboard.

## Future: Email via Gmail API

If you want email alerts later, add a Gmail API service account with domain-wide delegation. This is more complex (OAuth2 setup) and not needed for the MVP since Google Chat covers real-time team visibility. Add it in a later phase for escalation workflows (e.g., critical findings not resolved after 24 hours).
