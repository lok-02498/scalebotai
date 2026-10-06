#!/usr/bin/env bash
# =============================================================================
# k8s/strategies/canary/promote.sh [image] — canary passed: roll it out to 100%
# =============================================================================
# Usage:  ./promote.sh                      # promote the image the canary runs
#         ./promote.sh repo/scalebotai-api:43-def5678
#
# Steps:
#   1. determine the image (argument, or read from deployment/scalebotai-api-canary)
#   2. check the canary is healthy
#   3. rolling update of deployment/scalebotai-api to that image
#      (maxSurge 25% / maxUnavailable 0 -> zero downtime); undo on failure
#   4. remove the weighted canary routing (canary Ingress), point the primary
#      Ingress back at scalebotai-api
#   5. delete the canary Deployment and the helper Services
# Works for both variants (replica-ratio and ingress-weighted).
# =============================================================================
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

image="${1:-}"
canary_exists=false
if k get deployment "$CANARY_DEPLOYMENT" >/dev/null 2>&1; then
  canary_exists=true
fi

if [[ -z "$image" ]]; then
  $canary_exists || die "no ${CANARY_DEPLOYMENT} deployment and no image argument"
  image="$(canary_image)"
fi
[[ -n "$image" ]] || die "could not determine the image to promote"

if $canary_exists; then
  log "checking canary health ..."
  k rollout status "deployment/${CANARY_DEPLOYMENT}" --timeout=120s \
    || die "canary is not healthy - investigate or run ./abort.sh"
fi

# ---- roll the stable Deployment ---------------------------------------------------
log "promoting ${image} -> deployment/${STABLE_DEPLOYMENT}"
k set image "deployment/${STABLE_DEPLOYMENT}" "${CONTAINER}=${image}"
k annotate "deployment/${STABLE_DEPLOYMENT}" --overwrite \
  "kubernetes.io/change-cause=canary promoted: ${image}" >/dev/null
if ! k rollout status "deployment/${STABLE_DEPLOYMENT}" --timeout="$ROLLOUT_TIMEOUT"; then
  log "stable rollout failed - rolling back deployment/${STABLE_DEPLOYMENT}"
  k rollout undo "deployment/${STABLE_DEPLOYMENT}" || true
  die "promotion failed; stable restored. Canary left in place for investigation (./abort.sh to remove)."
fi

# ---- clean up canary routing and resources ------------------------------------------
# 1) no more weighted/header canary routing (all traffic -> primary Ingress)
k delete ingress "$CANARY_INGRESS" --ignore-not-found >/dev/null
# 2) primary back to the normal Service (only if set-weight.sh had changed it)
retarget_primary "$STABLE_SERVICE" "$BASE_SERVICE"
# 3) canary pods are now redundant (same image as stable)
k delete deployment "$CANARY_DEPLOYMENT" --ignore-not-found >/dev/null
k delete service "$CANARY_SERVICE" "$STABLE_SERVICE" --ignore-not-found >/dev/null

log "SUCCESS: ${image} serves 100% of traffic via deployment/${STABLE_DEPLOYMENT}"
k get deployment "$STABLE_DEPLOYMENT" -o wide
