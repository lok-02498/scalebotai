#!/usr/bin/env bash
# =============================================================================
# 10-generate-self-signed-cert.sh
# -----------------------------------------------------------------------------
# Runs automatically at container start: the official nginx image entrypoint
# (/docker-entrypoint.sh) executes every executable *.sh in /docker-entrypoint.d/
# in sorted order before launching nginx.
#
# If no TLS key pair exists at /etc/nginx/certs/tls.{crt,key}, generate a
# self-signed certificate for local/dev use with SANs:
#   DNS:api.scalebotai.local, DNS:localhost, IP:127.0.0.1
# If a real certificate is mounted (e.g. from mkcert or a CA), it is left untouched.
#
# Optional environment overrides:
#   TLS_CERT_DIR     (default /etc/nginx/certs)
#   TLS_COMMON_NAME  (default api.scalebotai.local)
#   TLS_CERT_DAYS    (default 365)
# =============================================================================
set -euo pipefail

CERT_DIR="${TLS_CERT_DIR:-/etc/nginx/certs}"
CERT_FILE="${CERT_DIR}/tls.crt"
KEY_FILE="${CERT_DIR}/tls.key"
COMMON_NAME="${TLS_COMMON_NAME:-api.scalebotai.local}"
DAYS="${TLS_CERT_DAYS:-365}"

log() {
    echo "[10-generate-self-signed-cert] $*"
}

if [[ -s "${CERT_FILE}" && -s "${KEY_FILE}" ]]; then
    log "Existing certificate found at ${CERT_FILE} - skipping generation."
    exit 0
fi

if ! command -v openssl >/dev/null 2>&1; then
    log "ERROR: openssl is not installed in this image." >&2
    exit 1
fi

mkdir -p "${CERT_DIR}"

log "No certificate found - generating a self-signed certificate for CN=${COMMON_NAME} (valid ${DAYS} days)."

# Create the key with restrictive permissions from the very first byte.
umask 077

if ! openssl_output="$(openssl req -x509 -nodes -newkey rsa:2048 -sha256 -days "${DAYS}" \
        -keyout "${KEY_FILE}" \
        -out "${CERT_FILE}" \
        -subj "/C=IN/O=ScaleBotAI/OU=Capstone/CN=${COMMON_NAME}" \
        -addext "subjectAltName=DNS:${COMMON_NAME},DNS:localhost,IP:127.0.0.1" \
        -addext "basicConstraints=critical,CA:FALSE" \
        -addext "keyUsage=critical,digitalSignature,keyEncipherment" \
        -addext "extendedKeyUsage=serverAuth" 2>&1)"; then
    log "ERROR: certificate generation failed:" >&2
    echo "${openssl_output}" >&2
    rm -f "${KEY_FILE}" "${CERT_FILE}"
    exit 1
fi

# The NGINX master process (root) reads the key; workers never need it.
chmod 0600 "${KEY_FILE}"
chmod 0644 "${CERT_FILE}"

log "Certificate written to ${CERT_FILE}:"
openssl x509 -in "${CERT_FILE}" -noout -subject -enddate -ext subjectAltName 2>/dev/null \
    | sed 's/^/[10-generate-self-signed-cert]   /' || true
