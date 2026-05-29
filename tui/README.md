# tui/ — operations dashboard (Go / Bubble Tea)

A terminal dashboard over REVA's internal API (`/api/v1`). Read-only views plus
two write actions (requeue, on-demand audit). **This is REVA's single dashboard** —
the Vue web frontend was retired (see [`frontend/`](../frontend)).

## Run

```bash
go run . --demo                                  # mock data, no server
REVA_API_URL=http://localhost:8080/api/v1 \
REVA_API_KEY=<key> \
REVA_ODOO_URL=https://odoo.example.com \
  go run .
```

| Env var | Default | Purpose |
|---|---|---|
| `REVA_API_URL` | `http://localhost:8080/api/v1` | Internal API base URL |
| `REVA_API_KEY` | _(unset)_ | Sent as `Authorization: Bearer`; required when the API has auth on (always in prod) |
| `REVA_ODOO_URL` | `http://localhost:8069` | Base URL for "open ticket in Odoo" |

If `REVA_API_KEY` is set against a non-HTTPS, non-loopback URL, the TUI warns
that the key would travel in plaintext.

## Layout

| Path | Role |
|---|---|
| `main.go` | Flag parsing, env wiring, client construction, Bubble Tea program start. |
| `internal/api/iface.go` | `ClientIface` — the surface the UI depends on. |
| `internal/api/client.go` | Real HTTP client (10 s timeout, Bearer auth). |
| `internal/api/mock.go` | `MockClient` for `--demo`. |
| `internal/api/types.go` | DTOs mirroring the API's JSON responses. |
| `internal/ui/*.go` | One file per tab (dashboard, reviews, findings, failures, repos, pending, tickets) plus `app.go` (tab routing), `styles.go`, `messages.go`. |

Tabs `1`–`7`; global keys `r` refresh, `q` quit. Per-tab keys are in the
top-level [README](../README.md#tui).

## Why a TUI (and why only one dashboard)

The TUI is a zero-deploy, SSH-friendly tool for operators — no browser, no
static hosting, one Go binary against the API. REVA briefly had a parallel Vue
web dashboard, but maintaining two hand-written clients of one API meant
constant drift; it was retired in favour of the TUI. If the internal API's
response shape changes, update `internal/api/types.go` here.

## Test / build

```bash
go build ./...
go vet ./...
go test ./...
```
