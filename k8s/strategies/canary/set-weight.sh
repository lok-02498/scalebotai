#!/usr/bin/env bash
# =============================================================================
# k8s/strategies/canary/set-weight.sh <percent> — exact-weight canary (Variant B)
# =============================================================================
# Usage:  ./set-weight.sh 0      # PREPARE: route users to stable only; the canary
#                                #   is reachable only with header X-Canary: always.
#                                #   Run this BEFORE creating the canary Deployment
#                                #   so it never receives unplanned traffic.
#         ./set-weight.sh 10     # 10% of requests -> canary
#         ./set-weight.sh 25
#         ./set-weight.sh 50
#
# Steps (idempotent, safe to re-run):
#   1. weight > 0: require deployment/scalebotai-api-canary to be rolled out
#   2. apply service-canary.yaml (scalebotai-api-canary + scalebotai-api-stable)
#   3. point the primary Ingress at scalebotai-api-stable (exact percentages;
#      see service-canary.yaml for the math)
#   4. create the canary Ingress if missing and set canary-weight
# Gate every increase on the PromQL checks in README.md.
# =============================================================================
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

weight="${1:-}"
[[ "$weight" =~ ^[0-9]+$ ]] && (( weight >= 0 && weight <= 100 )) \
  || die "usage: $0 <weight 0-100>"

if k get deployment "$CANARY_DEPLOYMENT" >/dev/null 2>&1; then
  k rollout status "deployment/${CANARY_DEPLOYMENT}" --timeout="$ROLLOUT_TIMEOUT" \
    || die "canary is not healthy - run ./abort.sh"
  log "canary image: $(canary_image)"
elif (( weight > 0 )); then
  die "deployment/${CANARY_DEPLOYMENT} not found - apply deployment-canary.yaml and set its image first"
else
  log "no canary Deployment yet - preparing routing only (weight 0)"
fi

kubectl apply -f "${CANARY_DIR}/service-canary.yaml" >/dev/null
log "services ${CANARY_SERVICE} and ${STABLE_SERVICE} applied"

# Stable-only backend first: for a moment the canary gets LESS traffic, never more.
retarget_primary "$BASE_SERVICE" "$STABLE_SERVICE"

if ! k get ingress "$CANARY_INGRESS" >/dev/null 2>&1; then
  kubectl apply -f "${CANARY_DIR}/ingress-canary.yaml" >/dev/null
  log "canary ingress ${CANARY_INGRESS} created"
fi
k annotate ingress "$CANARY_INGRESS" --overwrite \
  "nginx.ingress.kubernetes.io/canary-weight=${weight}" >/dev/null

log "canary weight is now ${weight}% (header 'X-Canary: always' forces the canary)"
log "next: watch the PromQL gates for >= 10 min, then raise the weight, ./promote.sh or ./abort.sh"
