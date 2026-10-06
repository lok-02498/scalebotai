#!/usr/bin/env bash
# =============================================================================
# rollback.sh - roll the ScaleBotAI API back to the last known-good release
#
# Usage:    scripts/rollback.sh <rolling|canary|blue-green>
# Example:  scripts/rollback.sh rolling
#
# Called automatically by Jenkins (Rollback stage + post { failure } safety net) and usable
# manually. Uses the .deploy-state file written by scripts/deploy.sh when it exists, and falls
# back to safe cluster-side inference when it does not (e.g. a manual rollback hours later).
#
#   rolling     kubectl rollout undo deployment/scalebotai-api + rollout status.
#               Skipped when the Deployment already runs the recorded previous image, so a
#               deploy that failed before changing anything never reverts a good release.
#   canary      delete deployment/scalebotai-api-canary; if the new image had already been
#               promoted to scalebotai-api, also rollout undo the stable Deployment.
#   blue-green  point Service scalebotai-api-active back at the previous colour (from
#               .deploy-state, otherwise the opposite of the current colour). Instant, because
#               the previous colour is kept running by deploy.sh.
#
# Environment (all optional)
#   NAMESPACE              target namespace (default: $K8S_NAMESPACE or scalebotai)
#   ROLLOUT_TIMEOUT        kubectl rollout status timeout (default 300s)
#   DEPLOY_STATE_FILE      state file (default <repo>/.deploy-state)
#   PREVIOUS_IMAGE         known-good image (fallback when the state file lacks it; set by Jenkins)
#   TO_REVISION            rolling/canary: undo to this specific Deployment revision
#   BG_DEFAULT_REPLICAS    replicas used when the standby colour was scaled to 0 (default 3)
#   BG_SCALE_DOWN_FAILED   1 = scale the failed colour to 0 after switching back (default 1)
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

NAMESPACE="${NAMESPACE:-${K8S_NAMESPACE:-scalebotai}}"
STABLE_DEPLOYMENT="scalebotai-api"
CANARY_DEPLOYMENT="scalebotai-api-canary"
ACTIVE_SERVICE="scalebotai-api-active"
PREVIEW_SERVICE="scalebotai-api-preview"
CONTAINER_NAME="${CONTAINER_NAME:-api}"
ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-300s}"
DEPLOY_STATE_FILE="${DEPLOY_STATE_FILE:-${REPO_ROOT}/.deploy-state}"
TO_REVISION="${TO_REVISION:-}"
BG_DEFAULT_REPLICAS="${BG_DEFAULT_REPLICAS:-3}"
BG_SCALE_DOWN_FAILED="${BG_SCALE_DOWN_FAILED:-1}"

STRATEGY=""

# ----------------------------------------------------------------------------- logging
ts()      { date -u +%Y-%m-%dT%H:%M:%SZ; }
log()     { printf '%s [rollback] %s\n' "$(ts)" "$*"; }
ok()      { printf '%s [rollback] OK    %s\n' "$(ts)" "$*"; }
warn()    { printf '%s [rollback] WARN  %s\n' "$(ts)" "$*" >&2; }
error()   { printf '%s [rollback] ERROR %s\n' "$(ts)" "$*" >&2; }
die()     { error "$*"; exit 1; }
section() { printf '\n%s [rollback] ===== %s =====\n' "$(ts)" "$*"; }

usage() {
  awk 'NR > 2 && /^# =+$/ { exit } NR > 2 && /^#/ { sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"
}

# ----------------------------------------------------------------------------- helpers
k() { kubectl -n "${NAMESPACE}" "$@"; }

current_image() {
  k get deployment "$1" \
    -o jsonpath="{.spec.template.spec.containers[?(@.name==\"${CONTAINER_NAME}\")].image}" 2>/dev/null || true
}

require_deployment() {
  k get deployment "$1" >/dev/null 2>&1 || die "deployment/$1 not found in namespace ${NAMESPACE}"
}

wait_rollout() {
  local deployment="$1"
  log "Waiting for deployment/${deployment} rollout (timeout ${ROLLOUT_TIMEOUT})"
  if ! k rollout status "deployment/${deployment}" --timeout="${ROLLOUT_TIMEOUT}"; then
    error "deployment/${deployment} did not become ready within ${ROLLOUT_TIMEOUT}"
    k get pods -o wide 2>/dev/null | grep -E "^(NAME|${deployment}-)" || true
    k get events --sort-by=.lastTimestamp 2>/dev/null | tail -n 15 || true
    return 1
  fi
  ok "deployment/${deployment} ready, image $(current_image "${deployment}")"
}

state_get() {
  if [[ -f "${DEPLOY_STATE_FILE}" ]]; then
    sed -n "s/^${1}=//p" "${DEPLOY_STATE_FILE}" | tail -n 1
  fi
}

state_set() {
  local key="$1" value="$2" tmp="${DEPLOY_STATE_FILE}.tmp.$$"
  {
    grep -v "^${key}=" "${DEPLOY_STATE_FILE}" 2>/dev/null || true
    printf '%s=%s\n' "${key}" "${value}"
  } >"${tmp}"
  mv -f "${tmp}" "${DEPLOY_STATE_FILE}"
}

undo_deployment() {
  local deployment="$1"
  if [[ -n "${TO_REVISION}" ]]; then
    log "kubectl rollout undo deployment/${deployment} --to-revision=${TO_REVISION}"
    k rollout undo "deployment/${deployment}" --to-revision="${TO_REVISION}"
  else
    log "kubectl rollout undo deployment/${deployment}"
    k rollout undo "deployment/${deployment}"
  fi
  wait_rollout "${deployment}"
}

# =============================================================================
# rolling
# =============================================================================
rollback_rolling() {
  section "Rolling rollback of deployment/${STABLE_DEPLOYMENT}"
  require_deployment "${STABLE_DEPLOYMENT}"
  local current previous after
  current="$(current_image "${STABLE_DEPLOYMENT}")"
  previous="$(state_get PREVIOUS_IMAGE)"
  previous="${previous:-${PREVIOUS_IMAGE:-}}"
  log "Current image: ${current:-<none>}; recorded known-good image: ${previous:-<unknown>}"

  if [[ -z "${TO_REVISION}" && -n "${previous}" && "${current}" == "${previous}" ]]; then
    ok "deployment/${STABLE_DEPLOYMENT} already runs the known-good image - no undo needed"
    wait_rollout "${STABLE_DEPLOYMENT}"
    return 0
  fi

  undo_deployment "${STABLE_DEPLOYMENT}"
  after="$(current_image "${STABLE_DEPLOYMENT}")"
  if [[ -n "${previous}" && "${after}" != "${previous}" ]]; then
    warn "image after undo (${after}) differs from the recorded known-good image (${previous})"
    warn "inspect: kubectl -n ${NAMESPACE} rollout history deployment/${STABLE_DEPLOYMENT}"
  fi
  k rollout history "deployment/${STABLE_DEPLOYMENT}" 2>/dev/null | tail -n 4 || true
}

# =============================================================================
# canary
# =============================================================================
rollback_canary() {
  section "Canary rollback"
  require_deployment "${STABLE_DEPLOYMENT}"
  local canary_image current previous promoted
  canary_image="$(current_image "${CANARY_DEPLOYMENT}")"
  current="$(current_image "${STABLE_DEPLOYMENT}")"
  previous="$(state_get PREVIOUS_IMAGE)"
  previous="${previous:-${PREVIOUS_IMAGE:-}}"
  promoted="$(state_get CANARY_PROMOTED)"

  log "Deleting deployment/${CANARY_DEPLOYMENT} (image ${canary_image:-<absent>})"
  k delete deployment "${CANARY_DEPLOYMENT}" --ignore-not-found --wait=true --timeout=120s \
    || warn "could not delete deployment/${CANARY_DEPLOYMENT} cleanly"
  # A leftover ingress-nginx canary Ingress (Variant B) would route to a Service without endpoints.
  k delete ingress scalebotai-ingress-canary --ignore-not-found >/dev/null 2>&1 || true

  if [[ -z "${promoted}" ]]; then
    # No state file: infer whether the canary image reached the stable Deployment.
    promoted="false"
    if [[ -n "${canary_image}" && "${current}" == "${canary_image}" ]]; then
      promoted="true"
    elif [[ -n "${previous}" && "${current}" != "${previous}" ]]; then
      promoted="true"
    fi
    log "No CANARY_PROMOTED in state - inferred promoted=${promoted}"
  fi

  if [[ "${promoted}" != "true" ]]; then
    ok "canary was never promoted - deployment/${STABLE_DEPLOYMENT} untouched (${current})"
    return 0
  fi
  if [[ -z "${TO_REVISION}" && -n "${previous}" && "${current}" == "${previous}" ]]; then
    ok "deployment/${STABLE_DEPLOYMENT} already runs the known-good image ${previous}"
    return 0
  fi
  log "Canary image was promoted to deployment/${STABLE_DEPLOYMENT} - undoing"
  undo_deployment "${STABLE_DEPLOYMENT}"
}

# =============================================================================
# blue-green
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
  k patch service "$1" --type=merge -p "{\"spec\":{\"selector\":{\"color\":\"$2\"}}}" >/dev/null
}

ready_endpoints() {
  local ips
  ips="$(k get endpointslices -l "kubernetes.io/service-name=$1" \
    -o jsonpath='{.items[*].endpoints[?(@.conditions.ready==true)].addresses[*]}' 2>/dev/null || true)"
  if [[ -z "${ips}" ]]; then
    ips="$(k get endpoints "$1" -o jsonpath='{.subsets[*].addresses[*].ip}' 2>/dev/null || true)"
  fi
  printf '%s' "${ips}"
}

scale_down_failed() {
  local color="$1"
  if [[ "${BG_SCALE_DOWN_FAILED}" == "1" && -n "${color}" ]]; then
    log "Scaling failed colour deployment/scalebotai-api-${color} to 0 (BG_SCALE_DOWN_FAILED=1)"
    k scale "deployment/scalebotai-api-${color}" --replicas=0 >/dev/null \
      || warn "could not scale deployment/scalebotai-api-${color} to 0"
  else
    log "Keeping deployment/scalebotai-api-${color} running for investigation (BG_SCALE_DOWN_FAILED=0)"
  fi
}

rollback_blue_green() {
  section "Blue-green rollback"
  local active previous switched new_color target target_deploy ready replicas endpoints i
  k get service "${ACTIVE_SERVICE}" >/dev/null 2>&1 || die "service/${ACTIVE_SERVICE} not found in ${NAMESPACE}"
  active="$(bg_active_color)"
  [[ -n "${active}" ]] || die "service/${ACTIVE_SERVICE} has no 'color' selector - cannot determine live colour"
  previous="$(state_get PREVIOUS_COLOR)"
  switched="$(state_get SWITCHED)"
  new_color="$(state_get NEW_COLOR)"
  log "Live colour: ${active}; recorded previous colour: ${previous:-<unknown>}; switched: ${switched:-<unknown>}"

  # Deploy failed before the traffic switch: users never saw the new version.
  if [[ -n "${previous}" && "${switched}" == "false" && "${active}" == "${previous}" ]]; then
    ok "traffic was never switched - colour ${active} is still live, nothing to revert"
    patch_selector_color "${PREVIEW_SERVICE}" "${active}" 2>/dev/null || true
    scale_down_failed "${new_color}"
    return 0
  fi

  if [[ -n "${previous}" ]]; then
    target="${previous}"
  else
    target="$(opposite_color "${active}")" || die "unexpected colour '${active}' on service/${ACTIVE_SERVICE}"
    log "No state file - rolling back to the opposite colour ${target}"
  fi
  if [[ "${target}" == "${active}" ]]; then
    ok "colour ${target} is already live - nothing to switch"
    return 0
  fi

  target_deploy="scalebotai-api-${target}"
  require_deployment "${target_deploy}"
  ready="$(k get deployment "${target_deploy}" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || true)"
  if [[ -z "${ready}" || "${ready}" == "0" ]]; then
    replicas="$(state_get REPLICAS)"
    replicas="${replicas:-${BG_DEFAULT_REPLICAS}}"
    warn "standby colour ${target} has no ready pods - scaling to ${replicas} before switching"
    k scale "deployment/${target_deploy}" --replicas="${replicas}"
    wait_rollout "${target_deploy}" || die "standby colour ${target} did not become ready - manual intervention required"
  fi

  log "Switching service/${ACTIVE_SERVICE} selector color: ${active} -> ${target} (image $(current_image "${target_deploy}"))"
  patch_selector_color "${ACTIVE_SERVICE}" "${target}"

  endpoints=""
  for i in $(seq 1 30); do
    endpoints="$(ready_endpoints "${ACTIVE_SERVICE}")"
    if [[ -n "${endpoints}" ]]; then
      break
    fi
    sleep 2
  done
  [[ -n "${endpoints}" ]] || die "service/${ACTIVE_SERVICE} has no ready endpoints after switching back to ${target}"
  ok "service/${ACTIVE_SERVICE} serves colour ${target} (endpoints: ${endpoints})"

  # Preview now points at the failed colour so it can be inspected before it is scaled down.
  patch_selector_color "${PREVIEW_SERVICE}" "${active}" 2>/dev/null || true
  state_set SWITCHED false
  scale_down_failed "${active}"
}

# =============================================================================
# main
# =============================================================================
main() {
  if [[ $# -ne 1 ]]; then
    usage
    exit 2
  fi
  STRATEGY="$1"
  case "${STRATEGY}" in
    rolling|canary|blue-green) ;;
    bluegreen|blue_green) STRATEGY="blue-green" ;;
    *) die "unknown strategy '${STRATEGY}' (expected rolling | canary | blue-green)" ;;
  esac
  command -v kubectl >/dev/null 2>&1 || die "kubectl not found in PATH"

  local state_strategy
  state_strategy="$(state_get STRATEGY)"
  if [[ -n "${state_strategy}" && "${state_strategy}" != "${STRATEGY}" ]]; then
    warn "state file records strategy '${state_strategy}' but rollback requested for '${STRATEGY}'"
  fi
  if [[ -f "${DEPLOY_STATE_FILE}" ]]; then
    log "Using deploy state from ${DEPLOY_STATE_FILE}:"
    sed 's/^/    /' "${DEPLOY_STATE_FILE}"
  else
    warn "no deploy state file at ${DEPLOY_STATE_FILE} - using cluster-side inference"
  fi

  section "ScaleBotAI rollback: strategy=${STRATEGY} namespace=${NAMESPACE} context=$(kubectl config current-context 2>/dev/null || echo '<unknown>')"
  case "${STRATEGY}" in
    rolling)    rollback_rolling ;;
    canary)     rollback_canary ;;
    blue-green) rollback_blue_green ;;
  esac

  if [[ -f "${DEPLOY_STATE_FILE}" ]]; then
    state_set STATUS rolled-back
    state_set ROLLED_BACK_AT "$(ts)"
  fi
  section "Rollback (${STRATEGY}) completed"
  k get deployments -o wide 2>/dev/null || true
}

main "$@"
