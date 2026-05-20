# 02 — GitHub App Setup

## Step-by-Step Creation

### 1. Create the GitHub App

1. Go to your GitHub organization → Settings → Developer settings → GitHub Apps → New GitHub App.
2. Fill in:
   - **App name**: `ARIA PR Reviewer` (or your chosen name)
   - **Homepage URL**: `https://reviews.yourdomain.com` (your server's domain)
   - **Webhook URL**: `https://reviews.yourdomain.com/webhooks/github`
   - **Webhook secret**: Generate a strong random string (min 32 characters). Save it — you'll need it for `.env`.
     ```bash
     openssl rand -hex 32
     ```

### 2. Set Permissions

Under "Permissions & events" → "Repository permissions":

| Permission | Access | Purpose |
|---|---|---|
| Contents | Read-only | Fetch repository files, diffs, CLAUDE.md, .claude-review.yml |
| Metadata | Read-only | Required by GitHub Apps (automatic) |
| Pull requests | Read & write | Read PR data, post PR reviews with inline comments |
| Checks | Read & write | Create Check Run summaries |
| Issues | Read-only | Optional — read issue context if referenced in PRs |

Under "Organization permissions": none needed.

Under "Account permissions": none needed.

### 3. Subscribe to Webhook Events

Check these events:

| Event | Purpose |
|---|---|
| `Pull request` | Main trigger — opened, synchronize, reopened, ready_for_review |
| `Pull request review comment` | Capture developer reactions/feedback on ARIA's comments |
| `Issue comment` | Capture `/review` and `/deep-review` manual trigger commands |

Optional (for future features):

| Event | Purpose |
|---|---|
| `Installation` | Track when app is installed/uninstalled on repos |
| `Installation repositories` | Track which repos are added/removed |
| `Check run` | Support re-run button in GitHub UI |

### 4. Generate Private Key

After creating the app:

1. Scroll to "Private keys" section.
2. Click "Generate a private key".
3. A `.pem` file downloads automatically.
4. Move it to your server: `scp github-app-private-key.pem user@server:/opt/claude-reviewer/secrets/`
5. Lock permissions: `chmod 600 /opt/claude-reviewer/secrets/github-app-private-key.pem`

### 5. Note Your App Credentials

You need three values for your `.env`:

```
GITHUB_APP_ID=123456          # Shown on the app's settings page
GITHUB_WEBHOOK_SECRET=abc...  # The secret you generated in step 1
# Private key is mounted via Docker secrets, not in .env
```

### 6. Install the App

1. Go to your GitHub App's settings page → "Install App" in the left sidebar.
2. Choose your organization.
3. Select "Only select repositories" and pick your initial 5 repos.
4. Click "Install".
5. Note the **installation ID** from the URL: `https://github.com/settings/installations/INSTALLATION_ID`

The installation ID is also delivered in every webhook payload as `installation.id`, so the system will capture it automatically on the first event.

## Authentication Flow

GitHub Apps use a two-step authentication process:

### Step 1: JWT (App-level)

The API/worker creates a short-lived JWT signed with the private key:

```python
import jwt
import time

def create_app_jwt(app_id: int, private_key: str) -> str:
    now = int(time.time())
    payload = {
        "iat": now - 60,          # issued at (60s in the past for clock drift)
        "exp": now + (10 * 60),   # expires in 10 minutes (GitHub max)
        "iss": app_id,
    }
    return jwt.encode(payload, private_key, algorithm="RS256")
```

### Step 2: Installation Token (Installation-level)

Use the JWT to request an installation access token:

```
POST /app/installations/{installation_id}/access_tokens
Authorization: Bearer {jwt}
```

The returned token:
- Is scoped to the specific installation (org/repos).
- Has only the permissions the app was granted.
- Expires after 1 hour.
- Should be cached and refreshed before expiry.

```python
import httpx

async def get_installation_token(app_jwt: str, installation_id: int) -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
            },
        )
        resp.raise_for_status()
        return resp.json()["token"]
```

## Webhook Signature Verification

Every webhook from GitHub includes an `X-Hub-Signature-256` header. Always verify it:

```python
import hashlib
import hmac

def verify_webhook_signature(payload_body: bytes, signature_header: str, secret: str) -> bool:
    if not signature_header:
        return False
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"),
        payload_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)
```

Reject any request that fails verification. This prevents spoofed webhooks.

## Webhook Delivery and Redelivery

GitHub retries failed webhook deliveries (non-2xx responses) with exponential backoff. The `X-GitHub-Delivery` header is a unique UUID per delivery. Use it for deduplication — store it in `github_events.delivery_id` with a unique constraint.

If you need to manually replay a webhook:
1. Go to the GitHub App settings → "Advanced" → "Recent Deliveries".
2. Click "Redeliver" on any past delivery.

## Rate Limits

GitHub API rate limits for App installations: 5000 requests per hour per installation. With your current volume (1–2 PRs every 3 hours, 5 repos), you'll use roughly 20–40 requests per PR review (fetching PR data, diff, files, posting Check Run, posting review). You're well under the limit.

The worker should still check `X-RateLimit-Remaining` headers and back off if approaching the limit.

## Testing the Webhook

Before deploying, you can test with a tool like `smee.io`:

1. Go to https://smee.io/new — get a unique URL.
2. Set that URL as your webhook URL temporarily in the GitHub App settings.
3. Run the smee client locally: `npx smee-client --url https://smee.io/YOUR_ID --target http://localhost:8080/webhooks/github`
4. Open a test PR — the event will be forwarded to your local FastAPI.
5. Switch back to your real URL before production.
