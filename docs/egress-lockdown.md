# Worker egress lockdown (Phase-2 A2)

Constrains the worker's outbound network to an allowlist, so a prompt injection
that somehow gained network access can't exfiltrate to an attacker host. This is
**defense-in-depth**: A1 already removed the review subprocess's shell/web tools,
so it can't open arbitrary connections in the first place — this is the second layer.

## How it works

- A tiny **tinyproxy** sidecar (`egress-proxy/`, built from alpine) forwards
  outbound HTTP/HTTPS and **default-denies** every destination except the
  allowlist in `egress-proxy/filter`.
- The worker's `HTTP_PROXY`/`HTTPS_PROXY` point at it. The worker's `httpx`
  (GitHub/Chat/Odoo calls), `git` (clone/fetch), and the headless `claude`
  subprocess all honour those vars — the last confirmed by smoke test, and the
  runner forwards the proxy env into the subprocess (`_ENV_ALLOWLIST`).
- `NO_PROXY` keeps Postgres/Redis traffic direct.

Apply it as an overlay (kept separate so it can't break the base stack until you've validated it):

```bash
docker compose -f docker-compose.prod.yml -f docker-compose.egress.yml up -d
```

## Allowlist (`egress-proxy/filter`)

`api.anthropic.com`, `api.github.com`, `github.com`, `codeload.github.com`,
`chat.googleapis.com`. **Odoo:** the ticket-analysis tool (same worker container)
POSTs to `ODOO_CALLBACK_URL` — *not* used by PR reviews, but if you enable ticket
analysis, add your Odoo host to the filter or its callbacks will be blocked.

## Validation (run in staging before prod)

1. **Allowed host works:** `docker compose ... exec worker sh -lc 'curl -s -o /dev/null -w "%{http_code}\n" https://api.github.com'` → expect a normal code (e.g. 200/301).
2. **Disallowed host blocked:** `... curl -sS https://example.com` → expect a proxy **403/refused**, not a page.
3. **A real review still completes** (exercises the `claude` subprocess + git clone through the proxy): trigger a `/review` on a test PR and confirm it posts.
4. Check `docker compose ... logs egress-proxy` for `Filter` denials if something unexpectedly fails.

If a review fails to clone or reach Anthropic, a host is missing from the allowlist — add it to `egress-proxy/filter` and rebuild the proxy.

## Advisory vs. enforcing

This overlay sets the proxy **env**, which routes *compliant* clients. A process
that deliberately ignored the env could still attempt a direct connection. To make
it a **hard** block (so nothing can bypass the proxy):

- Put the worker on a dedicated `internal: true` Docker network for DB/Redis and
  the proxy, and remove it from any internet-capable network — the proxy becomes
  the only outbound path. (Requires separating Postgres/Redis onto an internal
  network so the other services keep their own internet; a larger topology change
  — do it deliberately, in staging.)
- Or add a host-level `DOCKER-USER` iptables rule dropping the worker container's
  direct egress except to the proxy.

Given A1 already closes the primary exfil path, the advisory routing here is a
reasonable first layer; add the hard block when you want the stronger guarantee.

> **Status:** the proxy config + overlay are unvalidated in CI (they need a live
> Docker host). Treat the staging validation above as the acceptance test before
> enabling in prod.
