#!/usr/bin/env bash
# =============================================================================
# k8s/strategies/canary/abort.sh — canary failed: send 100% back to stable
# =============================================================================
# Usage:  ./abort.sh
#
# Order matters: first stop the traffic, then delete the pods, then tidy up.
#   1. delete the canary Ingress     -> weighted/header traffic stops at once
#   2. delete the canary Deployment  -> its pod leaves the scalebotai-api
#                                       Service endpoints (preStop drains it)
#   3. point the primary Ingress back at scalebotai-api (if set-weight.sh
#      changed it) and delete the helper Services
#   4. confirm the stable Deployment is healthy
# The stable Deployment was never modified during the canary, so there is
# nothing to roll back there. Idempotent: safe to run more than once.
# =============================================================================
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

img="$(canary_image || true)"
log "aborting canary ${img:+(${img})}"

k delete ingress "$CANARY_INGRESS" --ignore-not-found >/dev/null
log "canary ingress removed (no more weighted/header routing)"

k delete deployment "$CANARY_DEPLOYMENT" --ignore-not-found --wait=true >/dev/null
log "canary deployment removed"

retarget_primary "$STABLE_SERVICE" "$BASE_SERVICE"
k delete service "$CANARY_SERVICE" "$STABLE_SERVICE" --ignore-not-found >/dev/null

k rollout status "deployment/${STABLE_DEPLOYMENT}" --timeout=120s \
  || die "stable deployment is not healthy - investigate immediately"

log "DONE: 100% of traffic is on deployment/${STABLE_DEPLOYMENT} ($(k get deployment "$STABLE_DEPLOYMENT" \
  -o jsonpath="{.spec.template.spec.containers[?(@.name==\"${CONTAINER}\")].image}"))"
