# Build context is the repo root (see docker-compose.prod.yml) so this image can
# also build the consultant docs SPA from docs-ui/.

# Stage 1 — build the docs SPA (served at /docs by nginx; Vite base=/docs/).
FROM node:22-alpine AS docs-build
WORKDIR /build
COPY docs-ui/package.json docs-ui/package-lock.json ./
RUN npm ci
COPY docs-ui/index.html docs-ui/vite.config.js ./
COPY docs-ui/src ./src
RUN npm run build

# Stage 2 — nginx reverse proxy + static docs site.
FROM nginx:1.27-alpine
# Drop the stock default.conf: it has `server_name localhost` and would shadow
# reva.conf for the loopback healthcheck probe (Host: localhost), serving the
# filesystem and 404ing /nginx-health. Removing it makes reva.conf the sole,
# default server — matching the assumption in templates/reva.conf.template.
RUN rm -f /etc/nginx/conf.d/default.conf
COPY nginx/nginx.conf /etc/nginx/nginx.conf
# Templates are processed by the nginx entrypoint: ${REVA_DOMAIN} is
# substituted from the environment before nginx starts.
COPY nginx/templates/ /etc/nginx/templates/
# Custom 404 page (served via error_page in the server block).
COPY nginx/404.html /usr/share/nginx/html/404.html
# Built docs SPA, served from /docs/ (Vite base matches).
COPY --from=docs-build /build/dist /usr/share/nginx/html/docs
