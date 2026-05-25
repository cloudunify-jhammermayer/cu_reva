# Odoo Module: cu_reva_ticket_analysis

**Date:** 2026-05-22
**Status:** Approved

## Overview

An Odoo module that integrates REVA ticket analysis into Odoo. Users click a button on a helpdesk ticket or project task; REVA analyses the ticket text asynchronously using Claude and writes structured HTML back to a dedicated field on the record.

---

## Module Structure

```
cu_reva_ticket_analysis/
├── __manifest__.py
├── models/
│   ├── reva_mixin.py              # abstract model: fields + button action
│   ├── helpdesk_ticket.py         # inherits mixin into helpdesk.ticket
│   ├── project_task.py            # inherits mixin into project.task
│   └── res_config_settings.py    # REVA config: URL, keys, per-model enable flags
├── views/
│   ├── helpdesk_ticket_views.xml
│   ├── project_task_views.xml
│   └── res_config_settings_views.xml
├── security/
│   └── ir.model.access.csv
├── controllers/
│   └── reva_router.py             # FastAPI router: POST /api/reva/write-field
└── data/
    └── ir_config_parameter.xml    # default (empty) values for REVA settings
```

**Odoo dependencies:** `helpdesk`, `project`, `fastapi` (OCA community addon)

---

## Data Model

### `reva.ticket.mixin` (abstract)

| Field | Type | Default | Purpose |
|---|---|---|---|
| `reva_status` | Selection | `draft` | Status badge on the form |
| `reva_analysis` | Html | `False` | HTML result written back by REVA |
| `reva_analysis_id` | Integer | `0` | REVA's `analysis_id` for callback correlation |

`reva_status` values: `draft` · `pending` · `completed` · `failed`

The "Analyse with REVA" button is visible only when `reva_status in ('draft', 'failed')`, allowing retries after failure.

### `helpdesk.ticket` / `project.task`

Both models inherit `reva.ticket.mixin`. No additional fields.

### `res.config.settings` extensions

| Setting field | `ir.config_parameter` key | Purpose |
|---|---|---|
| `reva_url` | `reva.url` | REVA API base URL |
| `reva_api_key` | `reva.api_key` | Bearer token sent outbound to REVA |
| `reva_callback_api_key` | `reva.callback_api_key` | Bearer token REVA sends on callback (validated inbound) |
| `reva_helpdesk_enabled` | `reva.helpdesk_enabled` | Show button on helpdesk.ticket |
| `reva_project_enabled` | `reva.project_enabled` | Show button on project.task |

---

## Component Flow

### Outbound — Odoo → REVA

```
User clicks "Analyse with REVA"
  → reva.ticket.mixin.action_analyse_reva()
      reads reva_url, reva_api_key from ir.config_parameter (sudo)
      checks model-enabled flag
      strips HTML from record.description via html2plaintext (Odoo built-in)
      POSTs to REVA /api/v1/ticket-analysis:
        {
          "ticket_id":  <record.id>,
          "model_name": <record._name>,
          "field_name": "reva_analysis",
          "text":       <plain text of record.description>
        }
      on 202: reva_status = "pending", reva_analysis_id = <analysis_id>
      on error: raise UserError (reva_status unchanged)
```

### Inbound — REVA → Odoo

```
REVA worker completes → POSTs to POST /api/reva/write-field
  → reva_router.py (FastAPI)
      validates Authorization: Bearer {reva_callback_api_key}
      resolves record: env[model_name].browse(ticket_id)
      record.reva_analysis = html
      record.reva_status = "completed"
      returns { "ok": true }

  on bad token   → 401 (REVA treats as permanent failure)
  on bad model   → 400
  on missing record → 404 (REVA treats as permanent failure)
```

REVA's retry policy (3x with backoff) only fires on 5xx or network errors. 4xx responses are permanent failures — the callback endpoint intentionally returns 4xx for auth and not-found cases.

---

## Error Handling

### Button action (outbound)

| Condition | Behaviour |
|---|---|
| `reva_url` not set | `UserError("REVA is not configured. Please set the REVA API URL in Settings → Technical → REVA.")` |
| Model not enabled | Button hidden via `invisible` attribute — no error shown |
| HTTP timeout | `UserError("REVA did not respond in time. Please try again in a moment.")` |
| Connection error | `UserError("Could not reach REVA. Check that the REVA API URL is correct and the service is running.")` |
| REVA returns non-202 | `UserError(f"REVA returned an unexpected response ({status_code}). Please contact your administrator.")` |

### Callback endpoint (inbound)

| Condition | Response |
|---|---|
| Wrong / missing Bearer token | 401 |
| Unknown `model_name` | 400 |
| Record not found | 404 |
| Record found, `reva_status != 'pending'` | Overwrite anyway (idempotent) |
| REVA signals job failed | `reva_status = 'failed'`, `reva_analysis` unchanged |

### Security

- Callback API key stored in `ir.config_parameter`, never hardcoded
- `sudo()` used only to read config parameters, not to write record fields
- FastAPI endpoint validates Bearer token on every request

---

## Views

### Helpdesk ticket form

A new **REVA Analysis** tab added to the helpdesk ticket form:
- "Analyse with REVA" button (hidden when `reva_status` not in `['draft', 'failed']` or helpdesk not enabled)
- `reva_status` status badge (readonly)
- `reva_analysis` Html field (readonly, displayed when `reva_status == 'completed'`)

### Project task form

Same tab/button pattern applied to the project task form.

### Settings

A **REVA** section under Settings → Technical:
- REVA API URL
- REVA API Key (password widget)
- REVA Callback API Key (password widget)
- ☑ Enable for Helpdesk tickets
- ☑ Enable for Project tasks

---

## Tests

Odoo `TransactionCase` tests:

| Test | Assertion |
|---|---|
| `test_mixin_fields_default` | Fresh ticket has `reva_status = 'draft'` |
| `test_action_sets_pending` | Mock outbound POST; click button → `reva_status = 'pending'` |
| `test_callback_writes_html` | POST to FastAPI endpoint with valid token → `reva_analysis` updated, `reva_status = 'completed'` |
| `test_callback_rejects_bad_token` | POST with wrong token → 401 |
| `test_callback_record_not_found` | POST with unknown ticket_id → 404 |
| `test_button_hidden_when_disabled` | Model flag off → button invisible |

---

## Out of Scope

- Auto-refresh / polling UI (user refreshes manually to see result)
- Chatter messages on completion
- Support for models other than `helpdesk.ticket` and `project.task`
- Rate limiting on the callback endpoint
