FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/nginx.conf
# Templates are processed by the nginx entrypoint: ${REVA_DOMAIN} is
# substituted from the environment before nginx starts.
COPY templates/ /etc/nginx/templates/
