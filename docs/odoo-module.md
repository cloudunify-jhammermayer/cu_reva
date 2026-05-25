# REVA — Odoo Module Integration

The `cu_reva_ticket_analysis` Odoo 19 module connects REVA's ticket analysis API to Odoo Helpdesk tickets and Project tasks.

See [ticket-analysis.md](ticket-analysis.md) for the full REVA API reference and HTML output format.

---

## REVA configuration

Add to your REVA `.env` (both `worker` and `api` containers):

```bash
# URL of the Odoo FastAPI callback endpoint
ODOO_CALLBACK_URL=https://your-odoo.example.com/api/reva/write-field

# Shared secret — REVA sends this as Bearer token, Odoo validates it
ODOO_CALLBACK_API_KEY=<generate with: openssl rand -hex 32>
```

Leave `ODOO_CALLBACK_URL` empty to disable write-back (analysis is still stored in REVA's DB and available via the poll endpoint).

---

## Flow

```
Odoo: user clicks "Analyse with REVA"
  → POST /api/v1/ticket-analysis (REVA)
      body: { ticket_id, model_name, field_name="reva_analysis", text }
      ← 202 { analysis_id, job_id, status: "pending" }
  → Odoo sets reva_status = "pending"

REVA worker (async, RQ)
  → Claude API (tool_use)
  → format HTML
  → POST /api/reva/write-field (Odoo)
      Authorization: Bearer {ODOO_CALLBACK_API_KEY}
      body: { ticket_id, model_name, field_name, html }
      ← 200 { "ok": true }
  → Odoo sets reva_status = "completed", writes reva_analysis
```

---

## Odoo module location

```
~/Projects/ast-odoo/custom_addons/cu_reva_ticket_analysis/
```

See the module's own `README.md` for installation and configuration instructions.

---

## Fields written to Odoo records

| Field | Type | Written by |
|---|---|---|
| `reva_status` | Selection: `draft/pending/completed/failed` | Odoo (button action sets `pending`; callback sets `completed`) |
| `reva_analysis` | Html | REVA callback (`/api/reva/write-field`) |
| `reva_analysis_id` | Integer | Odoo (stores REVA's `analysis_id` on 202 response) |

---

## Callback security

The Odoo endpoint validates `Authorization: Bearer <key>` against the `reva.callback_api_key` `ir.config_parameter` using `secrets.compare_digest` (timing-safe).

- Wrong/missing token → `401` (REVA treats as permanent failure, no retry)
- Unknown `model_name` → `400`
- Record not found → `404`
- Server error → `5xx` (REVA retries: 3 attempts, 30 s / 120 s / 300 s backoff)

---

## Supported models

| `model_name` | Odoo object |
|---|---|
| `helpdesk.ticket` | Helpdesk ticket |
| `project.task` | Project task |
