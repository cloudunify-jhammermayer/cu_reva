# 10 — TUI (Terminal User Interface)

## Overview

The TUI is a Go binary built with Bubble Tea. It connects to the FastAPI internal API over HTTP and provides read-only visibility into reviews, findings, metrics, failures, and developer statistics.

It runs locally on your workstation or directly on the server. It is not a container — it's a standalone binary.

## Tech Stack

| Component | Choice |
|---|---|
| Framework | Bubble Tea (github.com/charmbracelet/bubbletea) |
| Layout | Lip Gloss (github.com/charmbracelet/lipgloss) |
| Tables | Bubble Table (github.com/evertras/bubble-table) or custom |
| HTTP client | net/http (stdlib) |
| Config | Environment variables or config file |

## Configuration

```bash
# Environment variables
export ARIA_API_URL="https://reviews.yourdomain.com/api/v1"
# Or for local development:
export ARIA_API_URL="http://localhost:8080/api/v1"
```

Or a config file at `~/.config/aria/config.yaml`:

```yaml
api_url: https://reviews.yourdomain.com/api/v1
refresh_interval: 30s
default_repo: ""  # empty = all repos
```

## Views

### 1. Dashboard (Home)

The default view. Shows a real-time summary.

```
╔══════════════════════════════════════════════════════════════╗
║  🔍 ARIA — PR Review Dashboard                    [q] quit ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  Last 24 Hours                    Last 7 Days                ║
║  ─────────────                    ───────────                ║
║  Reviews completed:  8            Reviews completed:  34     ║
║  Failed reviews:     1            Failed reviews:     3      ║
║  Avg duration:       1m 48s       Avg duration:       2m 02s ║
║  Success rate:       88.9%        Success rate:       91.2%  ║
║                                                              ║
║  Findings (24h)                   Cost (7d)                  ║
║  ─────────────                    ─────────                  ║
║  🔴 Critical:  0                  Estimated:  $12.84         ║
║  🟠 Major:     2                  Avg/review: $0.38          ║
║  🟡 Minor:     11                                            ║
║  🔵 Info:      5                                             ║
║                                                              ║
║  Active Workers: 1/1              Queue: 0 pending           ║
║                                                              ║
╠══════════════════════════════════════════════════════════════╣
║  [1] Dashboard  [2] Reviews  [3] Findings  [4] Failures     ║
║  [5] Metrics    [6] Repos    [r] Refresh   [q] Quit         ║
╚══════════════════════════════════════════════════════════════╝
```

### 2. Reviews List

Paginated list of review runs. Filterable by repo, status, author.

```
╔══════════════════════════════════════════════════════════════╗
║  🔍 ARIA — Recent Reviews          [/] filter  [r] refresh  ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  Repo             PR#   Author    Status     Duration  Find  ║
║  ───────────────  ────  ────────  ─────────  ────────  ────  ║
║  org/webapp       #182  alice     ✅ passed   1m 32s    2    ║
║  org/api-svc      #77   bob       ❌ failed   0m 41s    -    ║
║  org/odoo-mod     #94   charlie   ⚠️  issues  3m 10s    6    ║
║  org/webapp       #180  alice     ✅ passed   1m 15s    0    ║
║  org/api-svc      #76   diana     ⛔ decline  0m 02s    -    ║
║  org/odoo-mod     #93   bob       ✅ passed   2m 45s    1    ║
║                                                              ║
║  Page 1/4                                   ← [j/k] → [↵]  ║
╠══════════════════════════════════════════════════════════════╣
║  Filter: repo=all  status=all  author=all                    ║
╚══════════════════════════════════════════════════════════════╝
```

Status indicators:
- ✅ `passed` — completed, no major/critical findings
- ⚠️ `issues` — completed, has major/critical findings
- ❌ `failed` — worker error
- ⛔ `declined` — diff too large
- 🔄 `running` — currently in progress
- ⏳ `queued` — waiting in queue

### 3. Review Detail

Selecting a review shows full details and findings.

```
╔══════════════════════════════════════════════════════════════╗
║  🔍 Review #42 — org/odoo-mod PR #94                       ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  Title:    Add partner validation endpoint                   ║
║  Author:   charlie                                           ║
║  SHA:      a1b2c3d4                                          ║
║  Mode:     diff                                              ║
║  Model:    claude-sonnet-4                                   ║
║  Prompt:   v1.0                                              ║
║  Duration: 3m 10s                                            ║
║  Tokens:   4,521 in / 1,832 out                              ║
║  Cost:     $0.041                                            ║
║  Risk:     high                                              ║
║                                                              ║
║  Summary:                                                    ║
║  PR adds a new controller for partner validation but has     ║
║  an SQL injection risk and missing access control checks.    ║
║                                                              ║
║  Findings (6):                                               ║
║  ──────────────                                              ║
║  🔴 CRIT  SQL injection in partner query     0.95   👍3 👎0 ║
║  🟠 MAJ   Missing ir.rule for new model     0.88   👍2 👎0 ║
║  🟡 MIN   Unused import                     0.92   👍0 👎1 ║
║  🟡 MIN   Missing docstring on controller   0.70   👍1 👎0 ║
║  🔵 INFO  Consider using search_fetch()     0.65   👍0 👎0 ║
║  🔵 INFO  Good error handling pattern       0.90   👍2 👎0 ║
║                                                              ║
║  [o] Open on GitHub  [←] Back                                ║
╚══════════════════════════════════════════════════════════════╝
```

The `[o]` keybinding opens the PR URL in the default browser.

### 4. Findings View

Cross-repo finding search. Useful for identifying recurring patterns.

Filterable by: severity, category, repo, author, date range, confidence threshold.

### 5. Failures View

Shows recent failed reviews with error details. Filterable by error class (transient/permanent).

```
╔══════════════════════════════════════════════════════════════╗
║  🔍 ARIA — Failures                                         ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  Time          Repo            PR#   Error                   ║
║  ────────────  ──────────────  ────  ──────────────────────  ║
║  2h ago        org/api-svc     #77   Claude API 503 (3/3)    ║
║  1d ago        org/webapp      #175  JSON parse error         ║
║  3d ago        org/odoo-mod    #89   GitHub 403 permissions   ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
```

### 6. Metrics View

Developer and business metrics. The "improvement over time" calculation.

```
╔══════════════════════════════════════════════════════════════╗
║  🔍 ARIA — Developer Metrics (Last 3 months)                ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  Developer  Reviews  Avg Findings  Major/Crit  Trend         ║
║  ─────────  ───────  ────────────  ──────────  ─────         ║
║  alice      28       1.8           0.3         📉 improving  ║
║  bob        22       3.2           1.1         📈 worsening  ║
║  charlie    31       2.1           0.5         ➡️  stable    ║
║  diana      18       1.2           0.1         📉 improving  ║
║                                                              ║
║  Trend compares last 4 weeks vs prior 4 weeks.               ║
║                                                              ║
║  Feedback Quality:                                           ║
║  ──────────────────                                          ║
║  Overall approval rate:  78%                                 ║
║  Best category:          security (92% 👍)                   ║
║  Worst category:         style (41% 👍)                      ║
║                                                              ║
║  Cost this month:  $42.18                                    ║
║  Avg cost/review:  $0.38                                     ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
```

The **improvement trend** is calculated as:

```
recent_avg = avg(finding_count) for reviews in last 4 weeks
prior_avg = avg(finding_count) for reviews in prior 4 weeks

if recent_avg < prior_avg * 0.8: "improving"
elif recent_avg > prior_avg * 1.2: "worsening"
else: "stable"
```

### 7. Repositories View

Lists installed repos with their config and review stats.

## Go Project Structure

```
tui/
├── go.mod
├── go.sum
├── main.go                  # Entry point, config loading
├── cmd/
│   └── root.go              # CLI flags and config
└── internal/
    ├── api/
    │   └── client.go        # HTTP client for FastAPI
    ├── ui/
    │   ├── app.go           # Main Bubble Tea model (router)
    │   ├── dashboard.go     # Dashboard view
    │   ├── reviews.go       # Reviews list + detail
    │   ├── findings.go      # Findings search
    │   ├── failures.go      # Failures view
    │   ├── metrics.go       # Metrics view
    │   ├── repos.go         # Repositories view
    │   ├── styles.go        # Lip Gloss styles
    │   └── keys.go          # Keybindings
    └── models/
        └── types.go         # Shared data types
```

## Build and Distribution

```bash
cd tui/
go build -o aria-tui .

# Cross-compile for Linux (server)
GOOS=linux GOARCH=amd64 go build -o aria-tui-linux .

# Install locally
go install .
```

The binary connects to the FastAPI API. No database credentials needed on the developer's machine.

## Auto-Refresh

The dashboard view auto-refreshes every 30 seconds using Bubble Tea's tick command pattern. Other views refresh on demand with `[r]`.
