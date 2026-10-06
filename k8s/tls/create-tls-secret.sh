#!/usr/bin/env bash
# =============================================================================
# k8s/tls/create-tls-secret.sh — self-signed TLS for the lab Ingress
# =============================================================================
# Generates a self-signed certificate for api.scalebotai.local (plus the
# preview/kibana/grafana hostnames as Subject Alternative Names) and stores it
# as a `kubernetes.io/tls` Secret named scalebotai-tls, which the Ingress
# (k8s/base/08-ingress.yaml) references in spec.tls[].secretName.
#
# Usage (from the repository root; Linux, macOS or Git Bash on Windows):
#   ./k8s/tls/create-tls-secret.sh
#   NAMESPACES="scalebotai logging" ./k8s/tls/create-tls-secret.sh   # also for kibana-ingress
#   CERT_DIR=./certs DAYS=30 ./k8s/tls/create-tls-secret.sh
#
# Idempotent: re-running replaces the Secret (create --dry-run | apply).
# Browsers/curl will not trust a self-signed cert: use `curl -k` or import
# tls.crt into your OS trust store. Production: cert-manager + Let's Encrypt
# (k8s/tls/cert-manager-issuer.yaml).
# =============================================================================
set -euo pipefail

SECRET_NAME="${SECRET_NAME:-scalebotai-tls}"
# Space-separated list; NAMESPACE (singular, as passed by
# scripts/bootstrap-minikube.sh) is honoured as well.
NAMESPACES="${NAMESPACES:-${NAMESPACE:-scalebotai}}"
HOST="${HOST:-api.scalebotai.local}"
EXTRA_SANS="${EXTRA_SANS:-preview.api.scalebotai.local,kibana.scalebotai.local,grafana.scalebotai.local}"
DAYS="${DAYS:-365}"
CERT_DIR="${CERT_DIR:-$(mktemp -d)}"

command -v openssl >/dev/null 2>&1 || { echo "ERROR: openssl not found" >&2; exit 1; }
command -v kubectl >/dev/null 2>&1 || { echo "ERROR: kubectl not found" >&2; exit 1; }

mkdir -p "$CERT_DIR"
KEY_FILE="$CERT_DIR/tls.key"
CRT_FILE="$CERT_DIR/tls.crt"
CNF_FILE="$CERT_DIR/openssl.cnf"

# Build "DNS:a,DNS:b,..." for the subjectAltName extension. Modern clients
# ignore the CN and ONLY check SANs, so the host must be listed there.
SAN="DNS:${HOST}"
IFS=',' read -r -a extra <<< "$EXTRA_SANS"
for name in "${extra[@]}"; do
  if [[ -n "$name" ]]; then
    SAN="${SAN},DNS:${name}"
  fi
done

# An OpenSSL config file instead of `-subj "/CN=..."`: portable across OpenSSL
# versions and immune to Git-Bash-on-Windows path conversion of "/CN=...".
cat > "$CNF_FILE" <<EOF
[req]
prompt             = no
distinguished_name = dn
x509_extensions    = v3_server

[dn]
CN = ${HOST}
O  = ScaleBotAI

[v3_server]
basicConstraints = critical, CA:FALSE
keyUsage         = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName   = ${SAN}
EOF

echo "==> Generating self-signed certificate (${DAYS} days) for: ${SAN}"
# -x509   : output a self-signed certificate instead of a signing request
# -nodes  : do not encrypt the private key (the ingress controller must read it)
# -newkey : create a fresh 2048-bit RSA key pair
if ! openssl req -x509 -nodes -newkey rsa:2048 \
    -days "$DAYS" \
    -keyout "$KEY_FILE" \
    -out "$CRT_FILE" \
    -config "$CNF_FILE" >"$CERT_DIR/openssl.log" 2>&1; then
  echo "ERROR: openssl failed:" >&2
  cat "$CERT_DIR/openssl.log" >&2
  exit 1
fi

openssl x509 -in "$CRT_FILE" -noout -subject -enddate

for ns in $NAMESPACES; do
  if ! kubectl get namespace "$ns" >/dev/null 2>&1; then
    echo "WARN: namespace '$ns' does not exist yet - creating it"
    kubectl create namespace "$ns"
  fi
  echo "==> Creating/updating secret ${SECRET_NAME} in namespace ${ns}"
  kubectl create secret tls "$SECRET_NAME" \
    --namespace "$ns" \
    --cert "$CRT_FILE" \
    --key "$KEY_FILE" \
    --dry-run=client -o yaml | kubectl apply -f -
done

echo "==> Done. Certificate files kept in: ${CERT_DIR}"
echo "    Verify: kubectl -n ${NAMESPACES%% *} get secret ${SECRET_NAME} -o jsonpath='{.type}'   # kubernetes.io/tls"
echo "    Test:   curl -k https://${HOST}/health"
