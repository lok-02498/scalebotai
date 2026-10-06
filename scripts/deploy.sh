#!/usr/bin/env bash
# =============================================================================
# deploy.sh - deploy a ScaleBotAI API image to Kubernetes using a release strategy
#
# Usage:    scripts/deploy.sh <rolling|canary|blue-green> <image:tag>
# Example:  scripts/deploy.sh rolling scalebotai/scalebotai-api:42-a1b2c3d
#
# Strategies
#   rolling     kubectl set image on Deployment scalebotai-api (RollingUpdate maxSurge 25% /
#               maxUnavailable 0), record change-cause, wait for rollout status.
#   canary      apply k8s/strategies/canary/deployment-canary.yaml, set the new image on
#               scalebotai-api-canary, smoke test it, bake under live traffic, check the 5xx
#               error rate / p95 latency in Prometheus (if PROMETHEUS_URL is set), then promote
#               the image to scalebotai-api and remove the canary. Any failure before promotion
#               deletes the canary and exits 1 (stable never changed).
#   blue-green  read the live colour from Service scalebotai-api-active, deploy to the idle
#               colour, smoke test it through Service scalebotai-api-preview, then switch the
#               active selector. The old colour keeps running for an instant rollback.
#
# Rollback state is written to .deploy-state (KEY=VALUE lines) for scripts/rollback.sh.
#
# Environment (all optional)
#   NAMESPACE               target namespace (default: $K8S_NAMESPACE or scalebotai)
#   ROLLOUT_TIMEOUT         kubectl rollout status timeout (default 300s)
#   DEPLOY_STATE_FILE       state file path (default <repo>/.deploy-state)
#   SKIP_SMOKE=1            skip the in-cluster smoke tests (canary / blue-green)
#   CANARY_MANIFEST         canary manifest (default k8s/strategies/canary/deployment-canary.yaml)
#   CANARY_REPLICAS         replicas if the manifest declares 0 (default 1)
#   CANARY_BAKE_SECONDS     time the canary serves live traffic before analysis (default 120)
#   CANARY_URL              smoke-test this URL instead of a port-forward to the canary pod
#   PROMETHEUS_URL          e.g. http://prometheus.monitoring.svc:9090 - enables metrics analysis
#   CANARY_MAX_ERROR_RATE   max ratio of 5xx responses on canary pods (default 0.01 = 1 %)
#   CANARY_MAX_P95_SECONDS  max p95 request latency on canary pods (default 0.5)
#   CANARY_METRICS_WINDOW   PromQL rate() window (default 2m)
#   CANARY_ERROR_QUERY / CANARY_P95_QUERY   override the PromQL queries
#   CANARY_REQUIRE_METRICS=1  fail the canary when Prometheus is unreachable / has no data
#   CANARY_CLEANUP          delete | scale  (what to do with the canary after promotion, default delete)
#   BLUE_GREEN_DIR          blue-green manifests (default k8s/strategies/blue-green)
#   PREVIEW_URL             smoke-test this URL instead of a port-forward to the preview Service
#   BG_DEFAULT_REPLICAS     replicas for the idle colour if the live one reports 0 (default 3)
#   INSECURE / SMOKE_*      passed through to scripts/smoke_test.sh
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ----------------------------------------------------------------------------- configuration
NAMESPACE="${NAMESPACE:-${K8S_NAMESPACE:-scalebotai}}"
STABLE_DEPLOYMENT="scalebotai-api"
CANARY_DEPLOYMENT="scalebotai-api-canary"
CANARY_INGRESS="scalebotai-ingress-canary"
ACTIVE_SERVICE="scalebotai-api-active"
PREVIEW_SERVICE="scalebotai-api-preview"
CONTAINER_NAME="${CONTAINER_NAME:-api}"
CONTAINER_PORT="${CONTAINER_PORT:-8000}"
ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-300s}"
DEPLOY_STATE_FILE="${DEPLOY_STATE_FILE:-${REPO_ROOT}/.deploy-state}"
SKIP_SMOKE="${SKIP_SMOKE:-0}"

CANARY_MANIFEST="${CANARY_MANIFEST:-${REPO_ROOT}/k8s/strategies/canary/deployment-canary.yaml}"
CANARY_REPLICAS="${CANARY_REPLICAS:-1}"
CANARY_BAKE_SECONDS="${CANARY_BAKE_SECONDS:-120}"
CANARY_URL="${CANARY_URL:-}"
PROMETHEUS_URL="${PROMETHEUS_URL:-}"
CANARY_MAX_ERROR_RATE="${CANARY_MAX_ERROR_RATE:-0.01}"
CANARY_MAX_P95_SECONDS="${CANARY_MAX_P95_SECONDS:-0.5}"
CANARY_METRICS_WINDOW="${CANARY_METRICS_WINDOW:-2m}"
CANARY_REQUIRE_METRICS="${CANARY_REQUIRE_METRICS:-0}"
CANARY_CLEANUP="${CANARY_CLEANUP:-delete}"

BLUE_GREEN_DIR="${BLUE_GREEN_DIR:-${REPO_ROOT}/k8s/strategies/blue-green}"
PREVIEW_URL="${PREVIEW_URL:-}"
BG_DEFAULT_REPLICAS="${BG_DEFAULT_REPLICAS:-3}"

# PromQL defaults: canary pods are matched by pod name (Prometheus kubernetes_sd relabelling
# must attach `namespace` and `pod` labels to the scraped series).
CANARY_SELECTOR_PROMQL="namespace=\"${NAMESPACE}\",pod=~\"${CANARY_DEPLOYMENT}-.*\""
if [[ -z "${CANARY_ERROR_QUERY:-}" ]]; then
  CANARY_ERROR_QUERY="(sum(rate(scalebot_http_requests_total{${CANARY_SELECTOR_PROMQL},status=~\"5..\"}[${CANARY_METRICS_WINDOW}])) or vector(0)) / clamp_min(sum(rate(scalebot_http_requests_total{${CANARY_SELECTOR_PROMQL}}[${CANARY_METRICS_WINDOW}])), 1e-9)"
fi
if [[ -z "${CANARY_P95_QUERY:-}" ]]; then
  CANARY_P95_QUERY="histogram_quantile(0.95, sum by (le) (rate(scalebot_http_request_duration_seconds_bucket{${CANARY_SELECTOR_PROMQL}}[${CANARY_METRICS_WINDOW}])))"
fi

STRATEGY=""
IMAGE=""
PF_PID=""
TMP_DIR="$(mktemp -d 2>/dev/null || mktemp -d -t scalebotai-deploy)"

# ----------------------------------------------------------------------------- logging
ts()      { date -u +%Y-%m-%dT%H:%M:%SZ; }
log()     { printf '%s [deploy] %s\n' "$(ts)" "$*"; }
ok()      { printf '%s [deploy] OK    %s\n' "$(ts)" "$*"; }
warn()    { printf '%s [deploy] WARN  %s\n' "$(ts)" "$*" >&2; }
error()   { printf '%s [deploy] ERROR %s\n' "$(ts)" "$*" >&2; }
die()     { error "$*"; exit 1; }
section() { printf '\n%s [deploy] ===== %s =====\n' "$(ts)" "$*"; }

usage() {
  awk 'NR > 2 && /^# =+$/ { exit } NR > 2 && /^#/ { sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"
}

cleanup() {
  if [[ -n "${PF_PID}" ]] && kill -0 "${PF_PID}" 2>/dev/null; then
    kill "${PF_PID}" 2>/dev/null || true
    wait "${PF_PID}" 2>/dev/null || true
  fi
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

# ----------------------------------------------------------------------------- kubectl helpers
k() { kubectl -n "${NAMESPACE}" "$@"; }

current_image() {
  k get deployment "$1" \
    -o jsonpath="{.spec.template.spec.containers[?(@.name==\"${CONTAINER_NAME}\")].image}" 2>/dev/null || true
}

require_deployment() {
  k get deployment "$1" >/dev/null 2>&1 \
    || die "deployment/$1 not found in namespace ${NAMESPACE} (apply k8s/base first: kubectl apply -k k8s/base)"
}

annotate_change_cause() {
  local deployment="$1"
  local cause="${STRATEGY} deploy of ${IMAGE} (${BUILD_TAG:-manual} by ${BUILD_USER_ID:-${USER:-unknown}})"
  k annotate "deployment/${deployment}" "kubernetes.io/change-cause=${cause}" --overwrite >/dev/null \
    || warn "could not annotate change-cause on deployment/${deployment}"
}

diagnose() {
  local deployment="$1"
  warn "---- diagnostics for deployment/${deployment} ----"
  k get deployment "${deployment}" -o wide || true
  k describe deployment "${deployment}" 2>/dev/null | tail -n 25 || true
  k get pods -o wide 2>/dev/null | grep -E "^(NAME|${deployment}-)" || true
  k get events --sort-by=.lastTimestamp 2>/dev/null | tail -n 20 || true
}

wait_rollout() {
  local deployment="$1"
  log "Waiting for deployment/${deployment} rollout (timeout ${ROLLOUT_TIMEOUT})"
  if ! k rollout status "deployment/${deployment}" --timeout="${ROLLOUT_TIMEOUT}"; then
    error "Rollout of deployment/${deployment} did not complete within ${ROLLOUT_TIMEOUT}"
    diagnose "${deployment}"
    return 1
  fi
  ok "deployment/${deployment} ready: $(k get deployment "${deployment}" -o jsonpath='{.status.readyReplicas}/{.spec.replicas}') replicas, image $(current_image "${deployment}")"
}

# Label selector string ("k1=v1,k2=v2") built from a Deployment's spec.selector.matchLabels.
selector_of() {
  local sel
  sel="$(k get deployment "$1" \
    -o go-template='{{range $k, $v := .spec.selector.matchLabels}}{{$k}}={{$v}},{{end}}' 2>/dev/null || true)"
  printf '%s' "${sel%,}"
}

apply_manifest_path() {
  local path="$1"
  if [[ -d "${path}" && ( -f "${path}/kustomization.yaml" || -f "${path}/kustomization.yml" ) ]]; then
    k apply -k "${path}"
  else
    k apply -f "${path}"
  fi
}

# ----------------------------------------------------------------------------- state file
state_init() {
  : >"${DEPLOY_STATE_FILE}"
  state_set STRATEGY "${STRATEGY}"
  state_set NAMESPACE "${NAMESPACE}"
  state_set IMAGE "${IMAGE}"
  state_set BUILD "${BUILD_TAG:-manual}"
  state_set STARTED_AT "$(ts)"
  state_set STATUS in-progress
}

state_set() {
  local key="$1" value="$2" tmp="${DEPLOY_STATE_FILE}.tmp.$$"
  {
    grep -v "^${key}=" "${DEPLOY_STATE_FILE}" 2>/dev/null || true
    printf '%s=%s\n' "${key}" "${value}"
  } >"${tmp}"
  mv -f "${tmp}" "${DEPLOY_STATE_FILE}"
}

# ----------------------------------------------------------------------------- smoke tests
run_smoke() {
  local url="$1"
  if [[ "${SKIP_SMOKE}" == "1" ]]; then
    warn "SKIP_SMOKE=1 - skipping smoke test against ${url}"
    return 0
  fi
  log "Running smoke test against ${url}"
  bash "${SCRIPT_DIR}/smoke_test.sh" "${url}"
}

# smoke_via_port_forward <resource> <remote-port>
# Opens `kubectl port-forward` on a random local port, waits for it, and runs smoke_test.sh.
smoke_via_port_forward() {
  local resource="$1" remote_port="$2" pf_log="${TMP_DIR}/port-forward.log" local_port="" url i rc=0
  if [[ "${SKIP_SMOKE}" == "1" ]]; then
    warn "SKIP_SMOKE=1 - skipping smoke test against ${resource}"
    return 0
  fi
  log "Opening kubectl port-forward to ${resource}:${remote_port}"
  # Call kubectl directly (not via the k() function) so that $! is the kubectl PID itself.
  kubectl -n "${NAMESPACE}" port-forward --address 127.0.0.1 "${resource}" ":${remote_port}" >"${pf_log}" 2>&1 &
  PF_PID=$!
  for i in $(seq 1 30); do
    local_port="$(sed -n 's/^Forwarding from 127\.0\.0\.1:\([0-9][0-9]*\).*/\1/p' "${pf_log}" 2>/dev/null || true)"
    local_port="${local_port%%$'\n'*}"
    if [[ -n "${local_port}" ]]; then
      break
    fi
    if ! kill -0 "${PF_PID}" 2>/dev/null; then
      error "port-forward to ${resource} exited: $(cat "${pf_log}")"
      PF_PID=""
      return 1
    fi
    sleep 1
  done
  if [[ -z "${local_port}" ]]; then
    error "port-forward to ${resource} not ready after 30s: $(cat "${pf_log}")"
    return 1
  fi
  url="http://127.0.0.1:${local_port}"
  run_smoke "${url}" || rc=$?
  kill "${PF_PID}" 2>/dev/null || true
  wait "${PF_PID}" 2>/dev/null || true
  PF_PID=""
  return "${rc}"
}

# ----------------------------------------------------------------------------- numeric / JSON helpers
is_number() { [[ "${1:-}" =~ ^[0-9]+([.][0-9]+)?([eE][-+]?[0-9]+)?$ ]]; }
float_gt()  { awk -v a="$1" -v b="$2" 'BEGIN { exit !((a + 0) > (b + 0)) }'; }

# Reads a Prometheus /api/v1/query response on stdin, prints result[0].value[1] (or nothing).
prom_first_value() {
  if command -v jq >/dev/null 2>&1; then
    jq -r '.data.result[0].value[1] // empty' 2>/dev/null || true
  else
    local py
    py="$(command -v python3 || command -v python || true)"
    [[ -n "${py}" ]] || { warn "jq/python3 not available to parse Prometheus response"; return 0; }
    "${py}" -c 'import json,sys
try:
    r = json.load(sys.stdin)["data"]["result"]
    print(r[0]["value"][1] if r else "")
except Exception:
    pass' 2>/dev/null || true
  fi
}

# prom_query <promql> - prints the scalar value (may be empty / NaN). Returns 1 if Prometheus is unreachable.
prom_query() {
  local body
  body="$(curl -sS --fail --max-time 15 -G --data-urlencode "query=$1" "${PROMETHEUS_URL%/}/api/v1/query")" || return 1
  printf '%s' "${body}" | prom_first_value
}

# =============================================================================
# Strategy: rolling
# =============================================================================
deploy_rolling() {
  section "Rolling update of deployment/${STABLE_DEPLOYMENT} -> ${IMAGE}"
  require_deployment "${STABLE_DEPLOYMENT}"

  local previous_image previous_revision
  previous_image="$(current_image "${STABLE_DEPLOYMENT}")"
  previous_revision="$(k get deployment "${STABLE_DEPLOYMENT}" \
    -o jsonpath='{.metadata.annotations.deployment\.kubernetes\.io/revision}' 2>/dev/null || true)"
  state_init
  state_set PREVIOUS_IMAGE "${previous_image}"
  state_set PREVIOUS_REVISION "${previous_revision}"
  log "Current image: ${previous_image:-<none>} (revision ${previous_revision:-?})"

  if [[ "${previous_image}" == "${IMAGE}" ]]; then
    warn "deployment/${STABLE_DEPLOYMENT} already runs ${IMAGE} - nothing to change (verifying health only)"
  fi

  if ! k set image "deployment/${STABLE_DEPLOYMENT}" "${CONTAINER_NAME}=${IMAGE}"; then
    state_set STATUS failed
    die "kubectl set image on deployment/${STABLE_DEPLOYMENT} failed (nothing was changed)"
  fi
  annotate_change_cause "${STABLE_DEPLOYMENT}"
  if ! wait_rollout "${STABLE_DEPLOYMENT}"; then
    state_set STATUS failed
    die "Rolling update failed - run: scripts/rollback.sh rolling (Jenkins does this automatically)"
  fi
  k rollout history "deployment/${STABLE_DEPLOYMENT}" 2>/dev/null | tail -n 4 || true
}

# =============================================================================
# Strategy: canary
# =============================================================================
# Jenkins uses "Variant A" of k8s/strategies/canary (traffic split by replica ratio through Service
# scalebotai-api). If someone left the ingress-nginx "Variant B" canary Ingress behind, it would point
# at a Service without endpoints once the canary is gone, so it is removed together with the canary.
remove_canary() {
  k delete deployment "${CANARY_DEPLOYMENT}" --ignore-not-found --wait=false || true
  k delete ingress "${CANARY_INGRESS}" --ignore-not-found >/dev/null 2>&1 || true
}

canary_abort() {
  error "Canary FAILED: $1"
  warn "Deleting deployment/${CANARY_DEPLOYMENT}; deployment/${STABLE_DEPLOYMENT} keeps serving $(current_image "${STABLE_DEPLOYMENT}")"
  remove_canary
  state_set STATUS failed
  state_set FAILURE_REASON "$1"
  exit 1
}

canary_bake() {
  local elapsed=0 interval=15 selector restarts ready
  if (( CANARY_BAKE_SECONDS <= 0 )); then
    log "CANARY_BAKE_SECONDS=0 - no bake period"
    return 0
  fi
  selector="$(selector_of "${CANARY_DEPLOYMENT}")"
  selector="${selector:-app=scalebotai-api,track=canary}"
  log "Baking canary for ${CANARY_BAKE_SECONDS}s (pods: ${selector}) - watching readiness and restarts"
  while (( elapsed < CANARY_BAKE_SECONDS )); do
    sleep "${interval}"
    elapsed=$((elapsed + interval))
    restarts="$(k get pods -l "${selector}" \
      -o jsonpath='{range .items[*]}{range .status.containerStatuses[*]}{.restartCount}{"\n"}{end}{end}' 2>/dev/null \
      | awk '{ s += $1 } END { print s + 0 }' || true)"
    ready="$(k get deployment "${CANARY_DEPLOYMENT}" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || true)"
    log "  bake ${elapsed}/${CANARY_BAKE_SECONDS}s: ready=${ready:-0} restarts=${restarts}"
    if (( restarts > 0 )); then
      error "canary containers restarted ${restarts} time(s) during the bake period"
      return 1
    fi
    if [[ -z "${ready}" || "${ready}" == "0" ]]; then
      error "canary has no ready pods during the bake period"
      return 1
    fi
  done
}

# check_metric <name> <query> <max>  -> 0 within threshold / no data tolerated, 1 breach
check_metric() {
  local name="$1" query="$2" max="$3" value
  if ! value="$(prom_query "${query}")"; then
    if [[ "${CANARY_REQUIRE_METRICS}" == "1" ]]; then
      error "Prometheus query for ${name} failed (${PROMETHEUS_URL}) and CANARY_REQUIRE_METRICS=1"
      return 1
    fi
    warn "Prometheus unreachable at ${PROMETHEUS_URL} - skipping ${name} gate"
    return 0
  fi
  if ! is_number "${value}"; then
    if [[ "${CANARY_REQUIRE_METRICS}" == "1" ]]; then
      error "no ${name} data for the canary (value='${value}') and CANARY_REQUIRE_METRICS=1"
      return 1
    fi
    warn "no ${name} data for the canary yet (value='${value:-empty}', little or no traffic) - gate skipped"
    return 0
  fi
  if float_gt "${value}" "${max}"; then
    error "canary ${name} = ${value} exceeds threshold ${max}"
    return 1
  fi
  ok "canary ${name} = ${value} (threshold ${max})"
}

canary_analyze() {
  if [[ -z "${PROMETHEUS_URL}" ]]; then
    warn "PROMETHEUS_URL not set - skipping metrics analysis (smoke test + bake only)"
    return 0
  fi
  log "Querying Prometheus at ${PROMETHEUS_URL} (window ${CANARY_METRICS_WINDOW})"
  check_metric "5xx error rate" "${CANARY_ERROR_QUERY}" "${CANARY_MAX_ERROR_RATE}" || return 1
  check_metric "p95 latency (s)" "${CANARY_P95_QUERY}" "${CANARY_MAX_P95_SECONDS}" || return 1
}

deploy_canary() {
  section "Canary 1/5: deploy ${CANARY_DEPLOYMENT} with ${IMAGE}"
  require_deployment "${STABLE_DEPLOYMENT}"
  [[ -f "${CANARY_MANIFEST}" ]] || die "canary manifest not found: ${CANARY_MANIFEST}"

  local stable_image replicas
  stable_image="$(current_image "${STABLE_DEPLOYMENT}")"
  state_init
  state_set PREVIOUS_IMAGE "${stable_image}"
  state_set CANARY_PROMOTED false
  log "Stable deployment/${STABLE_DEPLOYMENT} currently runs ${stable_image:-<none>}"

  k apply -f "${CANARY_MANIFEST}" || canary_abort "kubectl apply -f ${CANARY_MANIFEST} failed"
  k set image "deployment/${CANARY_DEPLOYMENT}" "${CONTAINER_NAME}=${IMAGE}" \
    || canary_abort "kubectl set image on ${CANARY_DEPLOYMENT} failed"
  annotate_change_cause "${CANARY_DEPLOYMENT}"
  replicas="$(k get deployment "${CANARY_DEPLOYMENT}" -o jsonpath='{.spec.replicas}' 2>/dev/null || true)"
  if [[ -z "${replicas}" || "${replicas}" == "0" ]]; then
    k scale "deployment/${CANARY_DEPLOYMENT}" --replicas="${CANARY_REPLICAS}" \
      || canary_abort "could not scale ${CANARY_DEPLOYMENT}"
  fi
  wait_rollout "${CANARY_DEPLOYMENT}" || canary_abort "canary pods did not become ready"

  section "Canary 2/5: smoke test the canary pods"
  if [[ -n "${CANARY_URL}" ]]; then
    run_smoke "${CANARY_URL}" || canary_abort "smoke test against ${CANARY_URL} failed"
  else
    smoke_via_port_forward "deployment/${CANARY_DEPLOYMENT}" "${CONTAINER_PORT}" \
      || canary_abort "smoke test against deployment/${CANARY_DEPLOYMENT} failed"
  fi

  section "Canary 3/5: bake under live traffic"
  canary_bake || canary_abort "canary unhealthy during bake period"

  section "Canary 4/5: metrics analysis"
  canary_analyze || canary_abort "canary metrics breached thresholds"

  section "Canary 5/5: promote ${IMAGE} to deployment/${STABLE_DEPLOYMENT}"
  state_set CANARY_PROMOTED true
  if ! k set image "deployment/${STABLE_DEPLOYMENT}" "${CONTAINER_NAME}=${IMAGE}"; then
    remove_canary
    state_set STATUS failed
    die "promotion failed (kubectl set image on ${STABLE_DEPLOYMENT}) - run: scripts/rollback.sh canary"
  fi
  annotate_change_cause "${STABLE_DEPLOYMENT}"
  if ! wait_rollout "${STABLE_DEPLOYMENT}"; then
    remove_canary
    state_set STATUS failed
    die "stable rollout failed after promotion - run: scripts/rollback.sh canary (Jenkins does this automatically)"
  fi

  if [[ "${CANARY_CLEANUP}" == "scale" ]]; then
    log "Scaling deployment/${CANARY_DEPLOYMENT} to 0 (kept for the next canary)"
    k scale "deployment/${CANARY_DEPLOYMENT}" --replicas=0 || warn "could not scale canary to 0"
    k delete ingress "${CANARY_INGRESS}" --ignore-not-found >/dev/null 2>&1 || true
  else
    log "Deleting deployment/${CANARY_DEPLOYMENT} (promotion complete)"
    k delete deployment "${CANARY_DEPLOYMENT}" --ignore-not-found || warn "could not delete canary"
    k delete ingress "${CANARY_INGRESS}" --ignore-not-found >/dev/null 2>&1 || true
  fi
}

# =============================================================================
# Strategy: blue-green
# =============================================================================
bg_active_color() {
  k get service "${ACTIVE_SERVICE}" -o jsonpath='{.spec.selector.color}' 2>/dev/null || true
}

opposite_color() {
  case "$1" in
    blue)  echo green ;;
    green) echo blue ;;
    *)     return 1 ;;
  esac
}

patch_selector_color() {
  local service="$1" color="$2"
  k patch service "${service}" --type=merge -p "{\"spec\":{\"selector\":{\"color\":\"${color}\"}}}" >/dev/null
}

# Ready pod IPs behind a Service: EndpointSlices first (v1 Endpoints is deprecated since k8s 1.33),
# falling back to the core Endpoints object on older clusters.
ready_endpoints() {
  local ips
  ips="$(k get endpointslices -l "kubernetes.io/service-name=$1" \
    -o jsonpath='{.items[*].endpoints[?(@.conditions.ready==true)].addresses[*]}' 2>/dev/null || true)"
  if [[ -z "${ips}" ]]; then
    ips="$(k get endpoints "$1" -o jsonpath='{.subsets[*].addresses[*].ip}' 2>/dev/null || true)"
  fi
  printf '%s' "${ips}"
}

bg_resources_present() {
  k get service "${ACTIVE_SERVICE}" >/dev/null 2>&1 \
    && k get service "${PREVIEW_SERVICE}" >/dev/null 2>&1 \
    && k get deployment scalebotai-api-blue >/dev/null 2>&1 \
    && k get deployment scalebotai-api-green >/dev/null 2>&1
}

bg_abort() {
  error "Blue-green FAILED before the traffic switch: $1"
  warn "Live traffic is still served by colour $(bg_active_color) - no switch was performed"
  state_set STATUS failed
  state_set FAILURE_REASON "$1"
  exit 1
}

deploy_blue_green() {
  section "Blue-green 1/6: resolve the live colour"
  local saved_color active idle active_deploy idle_deploy previous_image replicas port endpoints i
  saved_color="$(bg_active_color)"
  if ! bg_resources_present; then
    warn "blue-green resources incomplete in ${NAMESPACE} - applying ${BLUE_GREEN_DIR}"
    [[ -d "${BLUE_GREEN_DIR}" ]] || die "blue-green manifest directory not found: ${BLUE_GREEN_DIR}"
    apply_manifest_path "${BLUE_GREEN_DIR}"
    if [[ -n "${saved_color}" ]]; then
      # Re-applying the manifests must not silently flip live traffic.
      patch_selector_color "${ACTIVE_SERVICE}" "${saved_color}"
    fi
  fi

  local base_replicas
  base_replicas="$(k get deployment "${STABLE_DEPLOYMENT}" -o jsonpath='{.spec.replicas}' 2>/dev/null || true)"
  if [[ -n "${base_replicas}" && "${base_replicas}" != "0" ]]; then
    warn "deployment/${STABLE_DEPLOYMENT} still runs ${base_replicas} replicas next to blue/green;"
    warn "blue-green replaces the base Deployment - see k8s/strategies/blue-green/README.md (switching modes)"
  fi

  active="$(bg_active_color)"
  if [[ -z "${active}" ]]; then
    active="blue"
    warn "service/${ACTIVE_SERVICE} has no 'color' selector - assuming blue is live"
  fi
  idle="$(opposite_color "${active}")" || die "unexpected colour '${active}' on service/${ACTIVE_SERVICE}"
  active_deploy="scalebotai-api-${active}"
  idle_deploy="scalebotai-api-${idle}"

  previous_image="$(current_image "${active_deploy}")"
  replicas="$(k get deployment "${active_deploy}" -o jsonpath='{.spec.replicas}' 2>/dev/null || true)"
  if [[ -z "${replicas}" || "${replicas}" == "0" ]]; then
    replicas="${BG_DEFAULT_REPLICAS}"
  fi
  state_init
  state_set PREVIOUS_COLOR "${active}"
  state_set NEW_COLOR "${idle}"
  state_set PREVIOUS_IMAGE "${previous_image}"
  state_set REPLICAS "${replicas}"
  state_set SWITCHED false
  log "Live colour: ${active} (${previous_image:-<none>}), idle colour: ${idle}, replicas: ${replicas}"

  section "Blue-green 2/6: deploy ${IMAGE} to idle colour ${idle}"
  k set image "deployment/${idle_deploy}" "${CONTAINER_NAME}=${IMAGE}" || bg_abort "set image on ${idle_deploy} failed"
  annotate_change_cause "${idle_deploy}"
  k scale "deployment/${idle_deploy}" --replicas="${replicas}" || bg_abort "could not scale ${idle_deploy}"
  wait_rollout "${idle_deploy}" || bg_abort "idle colour ${idle} did not become ready"

  section "Blue-green 3/6: smoke test ${idle} through service/${PREVIEW_SERVICE}"
  patch_selector_color "${PREVIEW_SERVICE}" "${idle}" || bg_abort "could not point ${PREVIEW_SERVICE} at ${idle}"
  if [[ -n "${PREVIEW_URL}" ]]; then
    run_smoke "${PREVIEW_URL}" || bg_abort "smoke test against ${PREVIEW_URL} failed"
  else
    port="$(k get service "${PREVIEW_SERVICE}" -o jsonpath='{.spec.ports[0].port}' 2>/dev/null || true)"
    smoke_via_port_forward "service/${PREVIEW_SERVICE}" "${port:-80}" \
      || bg_abort "smoke test against service/${PREVIEW_SERVICE} (${idle}) failed"
  fi

  section "Blue-green 4/6: switch live traffic ${active} -> ${idle}"
  state_set SWITCHED true
  if ! patch_selector_color "${ACTIVE_SERVICE}" "${idle}"; then
    state_set STATUS failed
    die "could not patch service/${ACTIVE_SERVICE} - run: scripts/rollback.sh blue-green"
  fi
  ok "service/${ACTIVE_SERVICE} now selects color=${idle}"

  section "Blue-green 5/6: verify endpoints of service/${ACTIVE_SERVICE}"
  endpoints=""
  for i in $(seq 1 30); do
    endpoints="$(ready_endpoints "${ACTIVE_SERVICE}")"
    if [[ -n "${endpoints}" ]]; then
      break
    fi
    sleep 2
  done
  if [[ -z "${endpoints}" ]]; then
    state_set STATUS failed
    die "service/${ACTIVE_SERVICE} has no ready endpoints after the switch - run: scripts/rollback.sh blue-green"
  fi
  ok "ready endpoints: ${endpoints}"

  section "Blue-green 6/6: keep ${active} as hot standby"
  patch_selector_color "${PREVIEW_SERVICE}" "${active}" || warn "could not point ${PREVIEW_SERVICE} at standby ${active}"
  log "deployment/${active_deploy} keeps running ${previous_image:-<none>} for instant rollback (scripts/rollback.sh blue-green)."
  log "Free its resources once the release is trusted: kubectl -n ${NAMESPACE} scale deployment/${active_deploy} --replicas=0"
}

# =============================================================================
# main
# =============================================================================
preflight() {
  command -v kubectl >/dev/null 2>&1 || die "kubectl not found in PATH"
  command -v curl >/dev/null 2>&1 || die "curl not found in PATH"
  log "kubectl context: $(kubectl config current-context 2>/dev/null || echo '<unknown>'), namespace: ${NAMESPACE}"
  k get deployments >/dev/null || die "cannot list deployments in namespace ${NAMESPACE} (check KUBECONFIG / RBAC)"
  if [[ "$(k auth can-i patch deployments 2>/dev/null || true)" != "yes" ]]; then
    die "current identity may not patch deployments in ${NAMESPACE} (check Role/RoleBinding for jenkins-deployer)"
  fi
}

main() {
  if [[ $# -ne 2 ]]; then
    usage
    exit 2
  fi
  STRATEGY="$1"
  IMAGE="$2"
  case "${STRATEGY}" in
    rolling|canary|blue-green) ;;
    bluegreen|blue_green) STRATEGY="blue-green" ;;
    *) die "unknown strategy '${STRATEGY}' (expected rolling | canary | blue-green)" ;;
  esac
  if [[ "${IMAGE}" != *:* && "${IMAGE}" != *@sha256:* ]]; then
    die "image must be <repository>:<tag> (got '${IMAGE}')"
  fi

  section "ScaleBotAI deploy: strategy=${STRATEGY} image=${IMAGE} namespace=${NAMESPACE}"
  preflight

  case "${STRATEGY}" in
    rolling)    deploy_rolling ;;
    canary)     deploy_canary ;;
    blue-green) deploy_blue_green ;;
  esac

  state_set STATUS succeeded
  state_set FINISHED_AT "$(ts)"
  section "Deployment of ${IMAGE} (${STRATEGY}) completed successfully"
}

main "$@"
