# syntax=docker/dockerfile:1.7
# =============================================================================
# ScaleBotAI edge proxy - NGINX reverse proxy, load balancer and TLS terminator
# -----------------------------------------------------------------------------
# Build context is THIS directory (docker/nginx), as configured in docker-compose.yml:
#   docker build -t scalebotai/scalebotai-nginx:local docker/nginx
#
# What is customised on top of the official image:
#   * nginx.conf            worker tuning, JSON access log, gzip, rate-limit zones, security headers
#   * conf.d/scalebotai.conf upstream chatbot-api:8000, HTTP->HTTPS, TLS, /api/ routing, stub_status
#   * 10-generate-self-signed-cert.sh
#       dropped into /docker-entrypoint.d/; the official entrypoint runs every
#       executable *.sh there (sorted) before starting nginx, so a self-signed
#       certificate is created on first start if none was mounted.
# =============================================================================

# Pinned patch release of the 1.27 line. >= 1.27.3 is REQUIRED: conf.d/scalebotai.conf
# uses "server chatbot-api:8000 resolve;" (re-resolving upstream DNS at runtime, which
# is what lets `docker compose up --scale chatbot-api=N` be picked up without a reload).
ARG NGINX_VERSION=1.27.5
FROM nginx:${NGINX_VERSION}-alpine

# openssl -> generate the self-signed certificate at container start
# bash    -> the entrypoint script follows the project shell standard (bash, set -euo pipefail)
# The stock default.conf (a "Welcome to nginx" server on :80) is removed so it
# cannot shadow our default_server.
RUN apk add --no-cache openssl bash \
 && rm -f /etc/nginx/conf.d/default.conf \
 && mkdir -p /etc/nginx/certs \
 && chmod 0750 /etc/nginx/certs

COPY nginx.conf /etc/nginx/nginx.conf
COPY conf.d/ /etc/nginx/conf.d/

# --chmod sets the execute bit even when the file comes from a Windows checkout
# (NTFS has no POSIX permissions). The sed strips CRLF line endings in case Git
# converted them (core.autocrlf=true), which would otherwise break the shebang
# with "/usr/bin/env: 'bash\r': No such file or directory".
COPY --chmod=0755 10-generate-self-signed-cert.sh /docker-entrypoint.d/10-generate-self-signed-cert.sh
RUN sed -i 's/\r$//' /docker-entrypoint.d/10-generate-self-signed-cert.sh \
        /etc/nginx/nginx.conf /etc/nginx/conf.d/*.conf

# 80   plain HTTP: only redirects to HTTPS (+ /nginx-health for probes)
# 443  HTTPS / HTTP2 public API entry point
# 8080 internal-only status server (stub_status for nginx-prometheus-exporter); never publish it
EXPOSE 80 443 8080

# Cheap local probe that does not depend on the API being up: the proxy itself
# is healthy if it answers. (API health is checked separately by Compose/k8s.)
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1/nginx-health || exit 1

# Base image already provides: ENTRYPOINT ["/docker-entrypoint.sh"], CMD ["nginx", "-g", "daemon off;"],
# STOPSIGNAL SIGQUIT (graceful: finish in-flight requests, then exit).
