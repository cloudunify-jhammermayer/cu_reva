FROM nginx:1.27-alpine
# Drop the stock default.conf: it has `server_name localhost` and would shadow
# reva.conf for the loopback healthcheck probe (Host: localhost), serving the
# filesystem and 404ing /nginx-health. Removing it makes reva.conf the sole,
# default server — matching the assumption in templates/reva.conf.template.
RUN rm -f /etc/nginx/conf.d/default.conf
COPY nginx.conf /etc/nginx/nginx.conf
# Templates are processed by the nginx entrypoint: ${REVA_DOMAIN} is
# substituted from the environment before nginx starts.
COPY templates/ /etc/nginx/templates/
# Custom 404 page (served via error_page in the server block).
COPY 404.html /usr/share/nginx/html/404.html
