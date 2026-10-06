#!/usr/bin/env bash
# =============================================================================
# Shared helpers for set-weight.sh, promote.sh and abort.sh (sourced, not run).
# All names can be overridden through environment variables.
# =============================================================================

NAMESPACE="${NAMESPACE:-scalebotai}"
STABLE_DEPLOYMENT="${STABLE_DEPLOYMENT:-scalebotai-api}"
CANARY_DEPLOYMENT="${CANARY_DEPLOYMENT:-scalebotai-api-canary}"
CONTAINER="${CONTAINER:-api}"
BASE_SERVICE="${BASE_SERVICE:-scalebotai-api}"            # selects app=scalebotai-api (stable + canary)
STABLE_SERVICE="${STABLE_SERVICE:-scalebotai-api-stable}" # selects track=stable only
CANARY_SERVICE="${CANARY_SERVICE:-scalebotai-api-canary}" # selects track=canary only
PRIMARY_INGRESS="${PRIMARY_INGRESS:-scalebotai-ingress}"
CANARY_INGRESS="${CANARY_INGRESS:-scalebotai-ingress-canary}"
ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-300s}"

CANARY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() { printf '[canary] %s\n' "$*"; }
die() { printf '[canary] ERROR: %s\n' "$*" >&2; exit 1; }
k()   { kubectl -n "$NAMESPACE" "$@"; }

command -v kubectl >/dev/null 2>&1 || die "kubectl not found"

# retarget_primary <from-service> <to-service>
# Rewrites every backend of the primary Ingress that points at <from> so it
# points at <to> (JSON patch per path index). No-op if nothing matches, and
# never touches other backends (e.g. scalebotai-api-active in blue-green mode).
retarget_primary() {
  local from="$1" to="$2" i=0 name ops=""
  if ! k get ingress "$PRIMARY_INGRESS" >/dev/null 2>&1; then
    log "primary ingress ${PRIMARY_INGRESS} not found - skipping backend change"
    return 0
  fi
  while IFS= read -r name; do
    name="${name%$'\r'}"   # tolerate CRLF output (Windows tooling)
    if [[ "$name" == "$from" ]]; then
      ops="${ops:+${ops},}{\"op\":\"replace\",\"path\":\"/spec/rules/0/http/paths/${i}/backend/service/name\",\"value\":\"${to}\"}"
    fi
    i=$((i + 1))
  done < <(k get ingress "$PRIMARY_INGRESS" \
             -o jsonpath='{range .spec.rules[0].http.paths[*]}{.backend.service.name}{"\n"}{end}')
  if [[ -n "$ops" ]]; then
    k patch ingress "$PRIMARY_INGRESS" --type=json -p "[${ops}]" >/dev/null
    log "primary ingress ${PRIMARY_INGRESS}: ${from} -> ${to}"
  fi
}

canary_image() {
  k get deployment "$CANARY_DEPLOYMENT" \
    -o jsonpath="{.spec.template.spec.containers[?(@.name==\"${CONTAINER}\")].image}" 2>/dev/null
}
