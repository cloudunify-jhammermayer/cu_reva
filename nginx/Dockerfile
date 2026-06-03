FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/nginx.conf
# Templates are processed by the nginx entrypoint: ${REVA_DOMAIN} is
# substituted from the environment before nginx starts.
COPY templates/ /etc/nginx/templates/
# Custom 404 page (served via error_page in the server block).
COPY 404.html /usr/share/nginx/html/404.html
