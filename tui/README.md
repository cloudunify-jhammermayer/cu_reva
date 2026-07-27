# tui/ — operations dashboard (Go / Bubble Tea)

A terminal dashboard over REVA's internal API (`/api/v1`). Mostly read views,
plus write actions (requeue, on-demand audit, repo + Odoo-instance management,
persona configuration). **This is REVA's single dashboard** —
the Vue web frontend was retired.

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
| `internal/ui/*.go` | One file per tab (dashboard, reviews, findings, failures, repos, pending, tickets, audits, feedback) plus `app.go` (tab routing), `styles.go`, `messages.go`. |

Tabs `1`–`9` (Dashboard, Reviews, Findings, Failures, Repos, Pending, Tickets,
Audits, Feedback); navigate lists with `j`/`k`, `g`/`G` (top/bottom),
`Ctrl+D`/`Ctrl+U` (half-page) and PgUp/PgDn; `/` filters the Findings, Repos and
Tickets lists; global keys `r` refresh, `q` quit. The **Tickets** tab groups by
GitHub repo into foldable sections, collapsed by default — `enter`/`space`
toggles the group at the cursor, `z` expands/collapses all. Opening a ticket's
issue drill-down (`enter` on a row with issues) also loads a read-only
**Journey** timeline — analyses, issues, reviews, change notes, and readiness —
into the bottom of the detail pane, from `GET /ticket-journeys`. The **Feedback** tab (`9`)
shows the Tier-3 learning signals — per (repo, category) findings / dismissals
(`/dismiss`) / fixes from `GET /metrics/learning`, plus active `/mute`s from
`GET /metrics/mutes`. The Repos tab adds `n` to register a
new app-installed repo (type `owner/name` → POST `/repos`), `a` to trigger a
repository audit (POST `/repos/{id}/audit`, with an inline status line), and `o`
to open the repo on GitHub. The Audits tab lists repo-audit findings (severity,
title, repo, file:line, GitHub issue `#NN`) from `GET /audit-findings`, with the
same severity filters as Findings (`a` all · `c` critical · `m` major · `n`
minor · `i` info; `r` refresh). Per-tab keys are in the top-level
[README](../README.md#tui).

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
