# REVA Ticket Analysis

Automated requirements analysis for Odoo tickets powered by Claude.

Submit a ticket's text via the REST API. REVA analyses it asynchronously and writes a structured HTML template back to a field of your choice on the Odoo record.

---

## What it does

Given a ticket description (in any language — German or English), REVA generates:

| Section | Contents |
|---|---|
| **Summary** | 2–4 sentence assessment of clarity and critical gaps |
| **Missing Information** | Concrete list of what is absent (who, what, why, scope, edge cases, permissions, Odoo module, …) |
| **Acceptance Criteria** | GIVEN / WHEN / THEN format, independently testable, max 10 |
| **Test Cases** | Grouped: Happy Path · Edge Cases · Error Scenarios |
| **Definition of Ready** | Checklist of conditions before dev can start |
| **Definition of Done** | Checklist of conditions before ticket can be closed |
| **Odoo-Specific Notes** | Affected modules/models, view/security changes, data migration, Odoo 19 quirks |

The result is written as HTML directly into a field on the Odoo record (e.g. `description` on `helpdesk.ticket`).

---

## Flow

```
Odoo (button / action)
  → POST /api/v1/ticket-analysis     (REVA)
      ← 202 { analysis_id, job_id }

REVA worker (async)
  → Claude API (tool_use)
  → format HTML
  → POST {ODOO_CALLBACK_URL}         (your Odoo FastAPI endpoint)
      body: { ticket_id, model_name, field_name, html }
      ← 200 { "ok": true }

REVA DB stores full result + token cost
Odoo field is updated with the generated HTML
```

---

## API Reference

### Submit a ticket for analysis

```
POST /api/v1/ticket-analysis
Content-Type: application/json

{
  "ticket_id":  123,
  "model_name": "helpdesk.ticket",
  "field_name": "description",
  "text":       "Als Benutzer möchte ich...",
  "attachment": {"filename": "spec.pdf", "content_base64": "..."},
  "github_url":  "https://github.com/acme/widgets"
}
```

`attachment` is optional (`null`/omitted for text-only tickets). When present it must be a `.docx`, `.pdf`, or `.txt` file; its text is extracted and folded into the prompt alongside `text`. An unsupported extension, bad base64, or content/extension mismatch is rejected with `422` at accept time (`reva/attachment_text.py`).

`github_url` is optional (`null`/`""`/omitted): the record's project repo, persisted for dashboard repo grouping (TUI Tickets tab). Format-validated at accept time (`422` on a non-`https://github.com/{owner}/{repo}` URL), with no reachability check.

**Response `202 Accepted`:**

```json
{
  "analysis_id": 42,
  "job_id":      "rq:job:abc123",
  "status":      "pending"
}
```

| Field | Description |
|---|---|
| `ticket_id` | Numeric ID of the Odoo record |
| `model_name` | `"helpdesk.ticket"` or `"project.task"` |
| `field_name` | Field on the record where REVA writes the result |
| `text` | Full ticket description text. German and English are supported. |
| `attachment` | Optional `{filename, content_base64}` — a `.docx`/`.pdf`/`.txt` file folded into the analysis. Omit or `null` for text-only. |
| `github_url` | Optional repository URL from the record's project, for dashboard repo grouping. Format-validated, no reachability check. Omit, `null`, or `""` when unknown. |

---

### Poll for status / result

```
GET /api/v1/ticket-analysis/{analysis_id}
```

**Response `200 OK`:**

```json
{
  "id":                 42,
  "job_id":             "rq:job:abc123",
  "ticket_id":          123,
  "model_name":         "helpdesk.ticket",
  "field_name":         "description",
  "status":             "completed",
  "result_html":        "<h2>Summary</h2>...",
  "error_message":      null,
  "model":              "claude-sonnet-4-6",
  "input_tokens":       1840,
  "output_tokens":      712,
  "estimated_cost_usd": 0.003210,
  "created_at":         "2026-05-22T09:00:00Z",
  "completed_at":       "2026-05-22T09:00:18Z"
}
```

`status` values: `pending` · `completed` · `failed`

---

## Callback Contract

REVA POSTs the result to your `ODOO_CALLBACK_URL` endpoint once analysis is complete.

**Your endpoint must accept:**

```
POST {ODOO_CALLBACK_URL}
Authorization: Bearer {ODOO_CALLBACK_API_KEY}
Content-Type: application/json

{
  "ticket_id":  123,
  "model_name": "helpdesk.ticket",
  "field_name": "description",
  "html":       "<h2>Summary</h2><p>...</p>"
}
```

**Expected response:**

```json
{ "ok": true }
```

**Error handling:**
- `4xx` → REVA treats as permanent failure (no retry, stored as `failed` in DB)
- `5xx` / network error → REVA retries (RQ retry policy: 3 attempts, 30s / 120s / 300s backoff)

---

## HTML Output Format

The HTML written to the Odoo field follows this structure (renders correctly in Odoo HTML fields):

```html
<h2>Summary</h2>
<p>...</p>

<h2>Missing Information</h2>
<ul>
  <li>User role not specified</li>
  <li>Error handling behaviour not described</li>
</ul>

<h2>Acceptance Criteria</h2>
<ul>
  <li><strong>Given</strong> a logged-in user <strong>When</strong> they submit the form <strong>Then</strong> the record is saved and a confirmation is shown</li>
</ul>

<h2>Test Cases</h2>
<h3>Happy Path</h3>
<ul><li>Submit a valid form and verify the record is created</li></ul>
<h3>Edge Cases</h3>
<ul><li>Submit with an empty required field</li></ul>
<h3>Error Scenarios</h3>
<ul><li>Submit without the required access right and verify the action is blocked</li></ul>

<h2>Definition of Ready</h2>
<ul>
  <li>&#9744; Problem statement is clearly defined</li>
  <li>&#9744; Affected Odoo module identified</li>
</ul>

<h2>Definition of Done</h2>
<ul>
  <li>&#9744; Code reviewed by a peer</li>
  <li>&#9744; All acceptance criteria verified on staging</li>
</ul>

<h2>Odoo-Specific Notes</h2>
<ul>
  <li>Affects helpdesk.ticket — no model changes required</li>
</ul>

<p><em>Generated by REVA</em></p>
```

---

## Configuration

Add to `.env` (both `worker` and `api` containers):

```bash
# URL of your Odoo FastAPI callback endpoint
ODOO_CALLBACK_URL=https://your-odoo.example.com/api/reva/write-field

# Shared secret — REVA sends this as Bearer token, your endpoint validates it
ODOO_CALLBACK_API_KEY=generate-with-openssl-rand-hex-32
```

Leave `ODOO_CALLBACK_URL` empty to disable the write-back (analysis is still stored in REVA's DB and available via the poll endpoint).

---

## Language support

Ticket text may be in German or English (or mixed). REVA always responds in English.

---

## Cost

Ticket analysis uses Claude Sonnet 4.6 by default. A typical ticket (300–800 words) costs approximately **$0.002–$0.008** per analysis. Token counts and cost are stored in `ticket_analyses` and visible via the `GET` endpoint.
