# REVA Ticket Analysis

Automated requirements analysis for Odoo tickets powered by Claude.

Submit a ticket's text via the REST API. REVA analyses it asynchronously and writes a structured HTML template back to a field of your choice on the Odoo record.

---

## What it does

Given a ticket description (in any language — German or English), REVA generates:

| Section | Contents |
|---|---|
| **Summary** | 2–4 sentence business assessment of clarity and critical gaps |
| **Missing Information** | Concrete questions for the ticket author (who, what, why, scope, edge cases, permissions, …), each tagged `certain`/`likely`/`possible` |
| **Odoo-Specific Notes** | Consultant-level observations, tagged `explicit`/`inferred`/`assumed` |
| **Standard Odoo Coverage** | Whether stock Odoo already covers the request — grounded in the retrieved official Odoo knowledge. Rendered only when a knowledge block was available. |
| **Existing Customizations** | Whether the customer's own custom addons (as documented in their repo) already cover or touch the request — grounded in the repo's docs. Rendered only when the repo-docs block was available. |
| **Development Estimate** | Per-user-story hour ranges (mid-level dev, AI-assisted; implementation + developer testing) with assumptions, plus a total |

The result is written as HTML directly into a field on the Odoo record (e.g. `description` on `helpdesk.ticket`). Sections whose grounding is absent are omitted rather than shown empty.

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

`github_url` is optional (`null`/`""`/omitted): the record's project repo. It drives dashboard repo grouping (TUI Tickets tab) **and** grounds the analysis in the repo's own docs — the worker lazily indexes `custom_addons/**/*.md` plus the repo-root `docs/**/*.md` from the repo's default branch and injects the sections relevant to the ticket, backing the *Existing Customizations* section (see [Repo-docs grounding](#repo-docs-grounding)). Format-validated at accept time (`422` on a non-`https://github.com/{owner}/{repo}` URL), with no reachability check — a well-formed but uninstalled repo degrades silently to a core-only analysis.

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
| `github_url` | Optional repository URL from the record's project — dashboard repo grouping and repo-docs grounding of the *Existing Customizations* section. Format-validated, no reachability check. Omit, `null`, or `""` when unknown. |

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

<h2>Odoo-Specific Notes</h2>
<ul>
  <li>Concerns the quotation workflow in Sales</li>
</ul>

<!-- Only when an Odoo knowledge block was retrieved -->
<h2>Standard Odoo Coverage</h2>
<p><strong>Coverage:</strong> partial</p>
<ul>
  <li><strong>Quotation templates</strong> (sale_management, feature) - Enable under Sales &gt; Configuration <em>[applications/sales/sale.rst#quotation-templates]</em> <small>confidence: high</small></li>
</ul>

<!-- Only when the repo-docs block was retrieved -->
<h2>Existing Customizations</h2>
<p><strong>Coverage:</strong> partial</p>
<ul>
  <li><strong>Custom quotation layout</strong> (cu_sale_reports) - extends the existing quotation PDF layout <em>[custom_addons/cu_sale_reports/README.md#layout]</em> <small>confidence: high</small></li>
</ul>

<h2>Development Estimate</h2>
<ul>
  <li>As a sales user I want a custom quote layout — <strong>3–6 h</strong> <small>custom development · confidence: medium</small><br><small>Assumptions: extends the existing report layout</small></li>
</ul>
<p><strong>Total: 3–6 h</strong> <small>(mid-level Odoo developer, AI-assisted; implementation + developer testing only)</small></p>

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

## Repo-docs grounding

When the request carries a `github_url`, the worker grounds the *Existing
Customizations* section in the repo's own documentation:

- A single core-query planner call derives English search terms from the ticket;
  those same terms drive both the Odoo-core retrieval and the repo-docs retrieval.
- The repo's default-branch markdown docs under `custom_addons/`/`custom-addons/`
  and the repo-root `docs/` folder (excluding `CLAUDE.md` and any `superpowers/`
  folder) are indexed **section-level** into Postgres
  (`repo_doc_sections`, full-text searchable). Indexing is **lazy**: at analysis
  time the worker compares the default branch's git-tree SHA against the stored
  one (`repo_docs_sync`) and re-indexes only when it changed — the common case is
  a 2-call no-op. Concurrent analyses of the same repo are serialized by a
  per-repo advisory lock (a losing worker just uses the current index).
- Only the sections relevant to the ticket are injected (capped), so the added
  prompt cost stays small. The count of injected sections is stored on
  `ticket_analyses.repo_docs_sections_used` and surfaced on the
  `GET /api/v1/ticket-analyses` list endpoint and the TUI Tickets tab.
- Everything degrades soft: an invalid URL, an uninstalled app, a sync failure,
  or no matching docs all fall back to a core-only analysis and are recorded as
  `repo_docs` ops events (visible in the TUI Failures tab), never a failed
  analysis. The repo-docs block is fenced as untrusted data, like the ticket text.

Docs are always read from the repository's **default branch**.

---

## Language support

Ticket text may be in German or English (or mixed). **REVA responds in the same
language the ticket is written in.**

---

## Cost

Ticket analysis uses Claude Sonnet 4.6 by default. A typical ticket (300–800 words) costs approximately **$0.002–$0.008** per analysis. Token counts and cost are stored in `ticket_analyses` and visible via the `GET` endpoint.
