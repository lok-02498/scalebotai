#!/usr/bin/env bash
# =============================================================================
# bootstrap-minikube.sh - one-command local Kubernetes environment for ScaleBotAI
#
# Usage:  scripts/bootstrap-minikube.sh
#
# Steps
#   1. start minikube (if not already running)
#   2. enable the ingress + metrics-server addons (HPA needs metrics-server)
#   3. build the API image inside minikube (no registry needed)
#   4. create namespace scalebotai and TLS secret scalebotai-tls (k8s/tls/create-tls-secret.sh)
#   5. kubectl apply -k k8s/base
#   6. point deployment/scalebotai-api at the locally built image and wait for the rollouts
#   7. print the hosts-file line for api.scalebotai.local and next steps
#
# Environment (all optional)
#   MINIKUBE_PROFILE       minikube profile                         (default minikube)
#   MINIKUBE_DRIVER        docker | hyperv | virtualbox | ...       (default docker)
#   MINIKUBE_CPUS          CPUs for the node                        (default 4)
#   MINIKUBE_MEMORY        memory in MB                             (default 8192)
#   MINIKUBE_DISK          disk size                                (default 30g)
#   MINIKUBE_K8S_VERSION   e.g. v1.37.1 (default: minikube's default)
#   NAMESPACE              application namespace                    (default scalebotai)
#   APP_HOST               Ingress host                             (default api.scalebotai.local)
#   IMAGE_REPO             image repository   (default ${DOCKERHUB_USER:-scalebotai}/scalebotai-api)
#   IMAGE_TAG              image tag          (default local-<git short sha> or local-<timestamp>)
#   BUILD_METHOD           image-build (minikube image build) | docker-env | skip  (default image-build)
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

PROFILE="${MINIKUBE_PROFILE:-minikube}"
DRIVER="${MINIKUBE_DRIVER:-docker}"
CPUS="${MINIKUBE_CPUS:-4}"
MEMORY="${MINIKUBE_MEMORY:-8192}"
DISK="${MINIKUBE_DISK:-30g}"
K8S_VERSION="${MINIKUBE_K8S_VERSION:-}"
NAMESPACE="${NAMESPACE:-scalebotai}"
APP_HOST="${APP_HOST:-api.scalebotai.local}"
IMAGE_REPO="${IMAGE_REPO:-${DOCKERHUB_USER:-scalebotai}/scalebotai-api}"
GIT_SHORT="$(git -C "${REPO_ROOT}" rev-parse --short=7 HEAD 2>/dev/null || true)"
IMAGE_TAG="${IMAGE_TAG:-local-${GIT_SHORT:-$(date +%Y%m%d%H%M%S)}}"
IMAGE="${IMAGE_REPO}:${IMAGE_TAG}"
BUILD_METHOD="${BUILD_METHOD:-image-build}"
TLS_SCRIPT="${REPO_ROOT}/k8s/tls/create-tls-secret.sh"

ts()      { date -u +%Y-%m-%dT%H:%M:%SZ; }
log()     { printf '%s [bootstrap] %s\n' "$(ts)" "$*"; }
ok()      { printf '%s [bootstrap] OK    %s\n' "$(ts)" "$*"; }
warn()    { printf '%s [bootstrap] WARN  %s\n' "$(ts)" "$*" >&2; }
die()     { printf '%s [bootstrap] ERROR %s\n' "$(ts)" "$*" >&2; exit 1; }
section() { printf '\n%s [bootstrap] ===== %s =====\n' "$(ts)" "$*"; }

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  awk 'NR > 2 && /^# =+$/ { exit } NR > 2 && /^#/ { sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"
  exit 0
fi

mk() { minikube -p "${PROFILE}" "$@"; }
kc() { kubectl --context "${PROFILE}" "$@"; }

# Retry a command up to N times (admission webhooks need a few seconds after addon enablement).
retry() {
  local attempts="$1" delay="$2" n=1
  shift 2
  until "$@"; do
    if (( n >= attempts )); then
      return 1
    fi
    warn "attempt ${n}/${attempts} failed: $* - retrying in ${delay}s"
    sleep "${delay}"
    n=$((n + 1))
  done
}

check_prereqs() {
  section "1/8 Prerequisites"
  local tool
  for tool in minikube kubectl; do
    command -v "${tool}" >/dev/null 2>&1 || die "${tool} not found in PATH"
  done
  if [[ "${BUILD_METHOD}" == "docker-env" ]]; then
    command -v docker >/dev/null 2>&1 || die "docker CLI required for BUILD_METHOD=docker-env"
  fi
  [[ -f "${REPO_ROOT}/docker/api/Dockerfile" ]] || die "docker/api/Dockerfile not found under ${REPO_ROOT}"
  [[ -f "${REPO_ROOT}/k8s/base/kustomization.yaml" ]] || die "k8s/base/kustomization.yaml not found under ${REPO_ROOT}"
  ok "minikube $(minikube version --short 2>/dev/null || echo '?'), kubectl $(kubectl version --client 2>/dev/null | head -n 1 || echo '?')"
}

start_minikube() {
  section "2/8 Start minikube (profile ${PROFILE})"
  if [[ "$(mk status --format '{{.Host}}' 2>/dev/null || true)" == "Running" ]]; then
    ok "minikube profile ${PROFILE} already running"
    mk update-context >/dev/null 2>&1 || true
    return 0
  fi
  local -a args=(start --driver="${DRIVER}" --cpus="${CPUS}" --memory="${MEMORY}" --disk-size="${DISK}")
  if [[ -n "${K8S_VERSION}" ]]; then
    args+=(--kubernetes-version="${K8S_VERSION}")
  fi
  log "minikube -p ${PROFILE} ${args[*]}"
  mk "${args[@]}"
  ok "minikube started: node IP $(mk ip)"
}

enable_addons() {
  section "3/8 Addons: ingress + metrics-server"
  mk addons enable ingress
  mk addons enable metrics-server
  log "Waiting for the ingress-nginx controller to become ready"
  retry 10 10 kc -n ingress-nginx wait --for=condition=ready pod \
    --selector=app.kubernetes.io/component=controller --timeout=60s \
    || die "ingress-nginx controller not ready"
  ok "ingress-nginx and metrics-server enabled"
}

build_image() {
  section "4/8 Build ${IMAGE} inside minikube (${BUILD_METHOD})"
  local build_date
  build_date="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  case "${BUILD_METHOD}" in
    image-build)
      (
        cd "${REPO_ROOT}"
        mk image build -t "${IMAGE}" -f docker/api/Dockerfile \
          --build-opt="build-arg=VERSION=${IMAGE_TAG}" \
          --build-opt="build-arg=VCS_REF=${GIT_SHORT:-unknown}" \
          --build-opt="build-arg=BUILD_DATE=${build_date}" \
          .
      )
      ;;
    docker-env)
      (
        cd "${REPO_ROOT}"
        eval "$(mk docker-env --shell bash)"
        docker build -f docker/api/Dockerfile \
          --build-arg VERSION="${IMAGE_TAG}" \
          --build-arg VCS_REF="${GIT_SHORT:-unknown}" \
          --build-arg BUILD_DATE="${build_date}" \
          -t "${IMAGE}" .
      )
      ;;
    skip)
      warn "BUILD_METHOD=skip - using the image referenced by the manifests"
      return 0
      ;;
    *)
      die "unknown BUILD_METHOD '${BUILD_METHOD}' (image-build | docker-env | skip)"
      ;;
  esac
  ok "image ${IMAGE} available inside minikube"
}

create_namespace_and_tls() {
  section "5/8 Namespace ${NAMESPACE} + TLS secret scalebotai-tls"
  kc create namespace "${NAMESPACE}" --dry-run=client -o yaml | kc apply -f -
  if [[ -f "${TLS_SCRIPT}" ]]; then
    log "Running k8s/tls/create-tls-secret.sh"
    kubectl config use-context "${PROFILE}" >/dev/null
    NAMESPACE="${NAMESPACE}" HOST="${APP_HOST}" bash "${TLS_SCRIPT}"
  else
    warn "${TLS_SCRIPT} not found - generating a self-signed certificate with openssl"
    command -v openssl >/dev/null 2>&1 || die "openssl not found"
    local tmp
    tmp="$(mktemp -d)"
    openssl req -x509 -nodes -newkey rsa:2048 -days 365 \
      -keyout "${tmp}/tls.key" -out "${tmp}/tls.crt" \
      -subj "/CN=${APP_HOST}/O=ScaleBotAI" -addext "subjectAltName=DNS:${APP_HOST}" >/dev/null 2>&1
    kc -n "${NAMESPACE}" create secret tls scalebotai-tls --cert="${tmp}/tls.crt" --key="${tmp}/tls.key" \
      --dry-run=client -o yaml | kc apply -f -
    rm -rf "${tmp}"
  fi
  kc -n "${NAMESPACE}" get secret scalebotai-tls >/dev/null || die "secret scalebotai-tls missing after TLS step"
  ok "TLS secret scalebotai-tls present"
}

apply_base() {
  section "6/8 kubectl apply -k k8s/base"
  retry 3 15 kc apply -k "${REPO_ROOT}/k8s/base" || die "kubectl apply -k k8s/base failed"
  if [[ "${BUILD_METHOD}" != "skip" ]]; then
    log "Pointing deployment/scalebotai-api at ${IMAGE} (imagePullPolicy IfNotPresent)"
    kc -n "${NAMESPACE}" patch deployment scalebotai-api --type=strategic -p \
      "{\"spec\":{\"template\":{\"spec\":{\"containers\":[{\"name\":\"api\",\"image\":\"${IMAGE}\",\"imagePullPolicy\":\"IfNotPresent\"}]}}}}"
    kc -n "${NAMESPACE}" annotate deployment scalebotai-api \
      "kubernetes.io/change-cause=bootstrap-minikube local build ${IMAGE}" --overwrite >/dev/null
  fi
}

wait_workloads() {
  section "7/8 Wait for workloads"
  if kc -n "${NAMESPACE}" get statefulset postgres >/dev/null 2>&1; then
    kc -n "${NAMESPACE}" rollout status statefulset/postgres --timeout=300s
  fi
  if kc -n "${NAMESPACE}" get deployment redis >/dev/null 2>&1; then
    kc -n "${NAMESPACE}" rollout status deployment/redis --timeout=180s
  fi
  kc -n "${NAMESPACE}" rollout status deployment/scalebotai-api --timeout=600s \
    || { kc -n "${NAMESPACE}" get pods -o wide; die "deployment/scalebotai-api did not become ready"; }
  kc -n "${NAMESPACE}" get pods,svc,ingress,hpa -o wide || true
}

print_access_info() {
  section "8/8 Access"
  local ip
  ip="$(mk ip 2>/dev/null || echo '<minikube-ip>')"
  cat <<EOF

ScaleBotAI is running in minikube profile '${PROFILE}' (namespace ${NAMESPACE}, image ${IMAGE}).

Add this line to your hosts file
  Linux/macOS: /etc/hosts      Windows: C:\\Windows\\System32\\drivers\\etc\\hosts (as Administrator)

    ${ip} ${APP_HOST}

With the docker driver on Windows/macOS the node IP is not routable from the host. Instead run
'minikube -p ${PROFILE} tunnel' in a separate terminal and use:

    127.0.0.1 ${APP_HOST}

Then verify:
    curl -k https://${APP_HOST}/health
    INSECURE=1 scripts/smoke_test.sh https://${APP_HOST}

Deploy a new version the way Jenkins does:
    scripts/deploy.sh rolling ${IMAGE_REPO}:<tag>
EOF
}

main() {
  check_prereqs
  start_minikube
  enable_addons
  build_image
  create_namespace_and_tls
  apply_base
  wait_workloads
  print_access_info
}

main "$@"
