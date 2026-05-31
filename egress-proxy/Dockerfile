# Minimal allowlisting forward proxy for the worker's outbound traffic (A2).
# Built from alpine + tinyproxy — no third-party image.
FROM alpine:3.20

RUN apk add --no-cache tinyproxy

COPY tinyproxy.conf /etc/tinyproxy/tinyproxy.conf
COPY filter /etc/tinyproxy/filter

EXPOSE 8888

# Starts as root and drops to the unprivileged `tinyproxy` user (per the conf).
ENTRYPOINT ["tinyproxy", "-d", "-c", "/etc/tinyproxy/tinyproxy.conf"]
