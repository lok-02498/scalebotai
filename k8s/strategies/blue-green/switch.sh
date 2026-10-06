#!/usr/bin/env bash
# =============================================================================
# k8s/strategies/blue-green/switch.sh — flip user traffic between blue/green
# =============================================================================
# Usage:
#   ./switch.sh            # toggle: make the currently IDLE colour active
#   ./switch.sh green      # make green active (no-op if it already is)
#   ./switch.sh blue       # ... or blue. Rolling back = switching back.
#
# Environment (all optional):
#   NAMESPACE        default scalebotai
#   ROLLOUT_TIMEOUT  default 300s   (wait for the target Deployment)
#   VERIFY_URL       e.g. https://api.scalebotai.local/health/ready — checked
#                    AFTER the switch; on failure the switch is reverted
#   CURL_OPTS        default "-k" (self-signed lab certificate)
#   VERIFY_ATTEMPTS  default 10 (2 s apart)
#
# Steps:
#   1. read the current colour from Service scalebotai-api-active
#   2. pre-flight: target Deployment fully rolled out, all replicas Ready
#   3. patch scalebotai-api-active selector -> target colour (atomic cut-over)
#      patch scalebotai-api-preview selector -> previous colour
#   4. verify: selector reads back, EndpointSlices contain only target pods,
#      optional HTTP check of VERIFY_URL
#   5. any verification failure -> automatic rollback to the previous colour
# Exit code 0 = switched (or nothing to do), non-zero = not switched.
# =============================================================================
set -euo pipefail

NAMESPACE="${NAMESPACE:-scalebotai}"
APP_LABEL="${APP_LABEL:-scalebotai-api}"
ACTIVE_SVC="${ACTIVE_SVC:-scalebotai-api-active}"
PREVIEW_SVC="${PREVIEW_SVC:-scalebotai-api-preview}"
ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-300s}"
VERIFY_URL="${VERIFY_URL:-}"
CURL_OPTS="${CURL_OPTS:--k}"
VERIFY_ATTEMPTS="${VERIFY_ATTEMPTS:-10}"

log() { printf '[blue-green] %s\n' "$*"; }
die() { printf '[blue-green] ERROR: %s\n' "$*" >&2; exit 1; }
k()   { kubectl -n "$NAMESPACE" "$@"; }

other_colour() { if [[ "$1" == "blue" ]]; then echo green; else echo blue; fi; }

set_selector() {  # <service> <colour>
  k patch service "$1" --type merge \
    -p "{\"spec\":{\"selector\":{\"app\":\"${APP_LABEL}\",\"color\":\"$2\"}}}" >/dev/null
}

ready_pod_count() {  # <colour>
  k get pods -l "app=${APP_LABEL},color=$1" \
    -o jsonpath='{range .items[*]}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' \
    | grep -c '^True$' || true
}

pod_ips() {  # <colour>
  k get pods -l "app=${APP_LABEL},color=$1" \
    -o jsonpath='{range .items[*]}{.status.podIP}{"\n"}{end}' | sed '/^$/d' | sort -u
}

ready_endpoint_ips() {  # <service>
  k get endpointslices -l "kubernetes.io/service-name=$1" \
    -o jsonpath='{range .items[*].endpoints[*]}{.addresses[0]} {.conditions.ready}{"\n"}{end}' \
    | awk '$2 == "true" { print $1 }' | sort -u
}

command -v kubectl >/dev/null 2>&1 || die "kubectl not found"

# ---- 1. current state ---------------------------------------------------------
current="$(k get service "$ACTIVE_SVC" -o jsonpath='{.spec.selector.color}' 2>/dev/null)" \
  || die "service ${ACTIVE_SVC} not found - apply service-active.yaml first"
case "$current" in
  blue|green) ;;
  *) die "service ${ACTIVE_SVC} has no blue/green colour selector (got '${current}')" ;;
esac

target="${1:-$(other_colour "$current")}"
case "$target" in
  blue|green) ;;
  *) die "usage: $0 [blue|green]" ;;
esac

if [[ "$target" == "$current" ]]; then
  log "${target} is already active - nothing to do"
  exit 0
fi

target_deploy="${APP_LABEL}-${target}"
log "active=${current}  ->  target=${target}  (deployment/${target_deploy})"

# ---- 2. pre-flight checks -----------------------------------------------------
k get deployment "$target_deploy" >/dev/null 2>&1 \
  || die "deployment/${target_deploy} does not exist - apply deployment-${target}.yaml first"

log "waiting for deployment/${target_deploy} to be fully rolled out ..."
k rollout status "deployment/${target_deploy}" --timeout="$ROLLOUT_TIMEOUT" \
  || die "deployment/${target_deploy} is not healthy - refusing to switch"

desired="$(k get deployment "$target_deploy" -o jsonpath='{.spec.replicas}')"
ready="$(ready_pod_count "$target")"
log "target pods Ready: ${ready}/${desired}"
if [[ -z "$desired" || "$desired" -lt 1 || "$ready" -lt "$desired" ]]; then
  die "target colour is not fully Ready (${ready}/${desired}) - refusing to switch"
fi

target_image="$(k get deployment "$target_deploy" -o jsonpath='{.spec.template.spec.containers[?(@.name=="api")].image}')"
log "target image: ${target_image}"

# ---- 3. switch ------------------------------------------------------------------
rollback() {
  log "ROLLBACK: restoring ${ACTIVE_SVC} -> ${current}"
  set_selector "$ACTIVE_SVC" "$current" || true
  set_selector "$PREVIEW_SVC" "$target" || true
  die "$1 (traffic restored to ${current})"
}

log "switching ${ACTIVE_SVC} selector to color=${target}"
set_selector "$ACTIVE_SVC" "$target"
if ! set_selector "$PREVIEW_SVC" "$current"; then
  log "WARN: could not update ${PREVIEW_SVC} (does it exist?)"
fi
k annotate service "$ACTIVE_SVC" --overwrite \
  "scalebotai.io/previous-color=${current}" \
  "scalebotai.io/switched-at=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  "scalebotai.io/active-image=${target_image}" >/dev/null || true

# ---- 4. verify ------------------------------------------------------------------
now="$(k get service "$ACTIVE_SVC" -o jsonpath='{.spec.selector.color}')"
[[ "$now" == "$target" ]] || rollback "selector verification failed (got '${now}')"

# EndpointSlices are updated asynchronously (usually < 1-2 s). Wait until the
# active Service's Ready endpoints are exactly target-colour pods.
target_ips="$(pod_ips "$target")"
endpoints_ok=false
for _ in $(seq 1 15); do
  ep_ips="$(ready_endpoint_ips "$ACTIVE_SVC")"
  if [[ -n "$ep_ips" ]] && [[ -z "$(comm -23 <(printf '%s\n' "$ep_ips") <(printf '%s\n' "$target_ips"))" ]]; then
    endpoints_ok=true
    break
  fi
  sleep 1
done
$endpoints_ok || rollback "endpoints of ${ACTIVE_SVC} did not converge to ${target} pods"
log "endpoints OK: $(printf '%s\n' "$ep_ips" | wc -l | tr -d ' ') ${target} pod(s) behind ${ACTIVE_SVC}"

if [[ -n "$VERIFY_URL" ]]; then
  command -v curl >/dev/null 2>&1 || rollback "curl not found for VERIFY_URL check"
  log "verifying ${VERIFY_URL} ..."
  ok=false
  for i in $(seq 1 "$VERIFY_ATTEMPTS"); do
    # shellcheck disable=SC2086  # CURL_OPTS is intentionally word-split
    code="$(curl $CURL_OPTS -s -o /dev/null -w '%{http_code}' --max-time 5 "$VERIFY_URL" || true)"
    if [[ "$code" == "200" ]]; then ok=true; break; fi
    log "  attempt ${i}/${VERIFY_ATTEMPTS}: HTTP ${code:-none}"
    sleep 2
  done
  $ok || rollback "health check ${VERIFY_URL} failed after switch"
  log "health check passed"
fi

log "SUCCESS: ${target} is live (${target_image}); ${current} is now idle/preview."
log "Instant rollback if needed:  $0 ${current}"
