# nginx/ — production reverse proxy

Front door for the production stack (`docker-compose.prod.yml`). Runs **plain
HTTP** behind a **Cloudflare tunnel** — TLS is terminated at the Cloudflare edge,
and `cloudflared` (on the host) forwards `reva.dev.cloudunify.org` to nginx on
`127.0.0.1:8080`. No certificates live here. The dev stack
(`docker-compose.yml`) skips nginx and exposes the API port directly.

| File | Role |
|---|---|
| `Dockerfile` | `nginx:1.27-alpine` + env-substituted config. |
| `nginx.conf` | Base config: JSON access log, `client_max_body_size`, and **real-IP restore** from Cloudflare's `CF-Connecting-IP`. |
| `templates/reva.conf.template` | Server config. `${REVA_DOMAIN}` is substituted at container start (`NGINX_ENVSUBST_FILTER=REVA_DOMAIN`, so nginx's own `$host` etc. are left intact). |

## What it does

- Listens on **port 80 only** (plain HTTP) for `${REVA_DOMAIN}`; published to the
  host as `127.0.0.1:8080` so only the local `cloudflared` tunnel can reach it.
- **Real client IP** — restores the true visitor IP from `CF-Connecting-IP`
  (`set_real_ip_from` trusts the private ranges cloudflared connects from), so
  logs, rate limits, and the webhook allowlist see the real source, not the
  tunnel connector.
- **`/webhooks/`** → API, rate-limited, with an IP allowlist for GitHub's hook
  ranges (matched against the restored real IP). HMAC signature is still the
  primary auth.
- **`/api/`** → API, rate-limited (the internal dashboard API).
- **`/health`** → API readiness (proxied; reports DB + Redis).
- **`/nginx-health`** → served by nginx itself for the container healthcheck.

## TLS / certificates

None here. Cloudflare terminates TLS at the edge and enforces HTTPS/HSTS. There
is no certbot, no Let's Encrypt, no `:443`. If you ever move off the tunnel,
re-introduce a TLS server block + cert provisioning.

## Cloudflare tunnel ingress

Point your `cloudflared` config (Zero Trust dashboard or `config.yml`) at this
container's loopback port:

```yaml
ingress:
  - hostname: reva.dev.cloudunify.org
    service: http://localhost:8080
  - service: http_status:404
```

## Why a template, not a baked config

Keeping the domain in an env var means the image is stateless — changing
`REVA_DOMAIN` needs no rebuild. The filter is scoped to `REVA_DOMAIN` so nginx
runtime variables aren't clobbered by substitution.
