# scheduler/ — debounce poller + weekly-report scheduler

A standalone container with one loop that does two time-driven jobs the API
can't (the API only reacts to webhooks):

1. **Debounce poller** (`poller.py`) — polls `pending_reviews` for rows that are
   `consumed = false AND scheduled_at <= now()`, claims each row `FOR UPDATE SKIP
   LOCKED` and marks it **consumed first** (crash-safe and multi-instance-safe),
   checks idempotency against `review_runs`, and enqueues `worker.tasks.run_review`
   with `Retry(max=3, interval=[30,120,300])`.
2. **Weekly report** (`reporter.py`) — on the configured weekday/hour
   (`REVA_REPORT_WEEKDAY` / `REVA_REPORT_HOUR_UTC`) enqueues the weekly summary,
   de-duplicated across restarts via the `weekly_reports` table.
3. **Operational monitor** (`monitor.py`) — each tick checks RQ queue depth,
   the failed-job registry, and repo-cache disk usage; posts a Google Chat alert
   when a threshold is crossed (and a recovery note when it clears). Alerts fire
   on transition, not every tick, so a sustained issue doesn't spam the channel.
   Thresholds: `REVA_QUEUE_DEPTH_ALERT` / `REVA_FAILED_JOBS_ALERT` /
   `REVA_REPO_CACHE_DISK_PCT_ALERT` (needs `GOOGLE_CHAT_WEBHOOK_URL`).
4. **Stale-run reaper** — each tick fails `review_runs` stuck in `running` longer
   than `REVA_STALE_RUNNING_SECONDS` (default 2× the review job timeout), so a
   worker killed mid-review doesn't leave an orphaned row forever.

Each tick also touches a heartbeat file (`REVA_SCHEDULER_HEARTBEAT_PATH`) that the
container healthcheck reads to detect a hung loop.

| File | Role |
|---|---|
| `scheduler/main.py` | SIGTERM/SIGINT-safe loop; ticks every `poll_interval_seconds`, runs poller → reporter → monitor → reaper, then writes the heartbeat. |
| `scheduler/poller.py` | `Poller.poll()` — fetch due ids, claim each `FOR UPDATE SKIP LOCKED`, consume, idempotency-guard, enqueue. |
| `scheduler/reporter.py` | `WeeklyReporter` — schedule check + dedup insert. |
| `scheduler/monitor.py` | `Monitor.check()` — threshold alerts with transition dedup. |
| `scheduler/settings.py` | Frozen `Settings`; `from_env()`. |

## Why a separate container

The debounce window ("wait 10 min after the last push before reviewing") and
the weekly cron are independent of HTTP traffic, so they live apart from the
API and scale/restart on their own.

**Consume-first** is deliberate: if the process dies after marking `consumed`
but before enqueuing, that one row is lost — acceptable, and far safer than
enqueue-first, which would double-enqueue on restart. The `FOR UPDATE SKIP
LOCKED` claim extends this to **multiple scheduler replicas**: two pollers never
consume the same row. `record_review_started`'s idempotency on
`(repo, pr, head_sha, mode)` is the second line of defence.

## Tests

```bash
cd scheduler && python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/     # 16
```

`FakeQueue` + SQLite in-memory; no Redis or Docker required.
