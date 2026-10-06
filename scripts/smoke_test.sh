#!/usr/bin/env bash
# =============================================================================
# smoke_test.sh - post-deployment smoke test for the ScaleBotAI API
#
# Usage:    scripts/smoke_test.sh <BASE_URL>
# Examples: scripts/smoke_test.sh http://127.0.0.1:18080
#           INSECURE=1 scripts/smoke_test.sh https://api.scalebotai.local
#
# Checks (each prints PASS / FAIL):
#   1. GET    /health                 -> 200 and status "ok" or "degraded" (retried with backoff)
#   2. POST   /api/v1/auth/register   -> 201 for a random smoke_* user
#   3. POST   /api/v1/auth/login      -> 200 and a non-empty access_token
#   4. POST   /api/v1/chat            -> 200 and a non-empty bot response
#   5. GET    /api/v1/chat/history    -> 200 and at least one item
#   6. POST   /api/v1/auth/logout     -> 200 / 204
#   Non-fatal (WARN only): old token rejected after logout, smoke user clean-up.
#
# Environment (all optional):
#   INSECURE=1               accept self-signed TLS certificates (curl --insecure)
#   SMOKE_HEALTH_RETRIES     health-check attempts before giving up   (default 10)
#   SMOKE_MAX_BACKOFF        max seconds between health attempts      (default 30)
#   SMOKE_TIMEOUT            per-request timeout in seconds           (default 15)
#   SMOKE_RESOLVE            curl --resolve entry, e.g. "api.scalebotai.local:443:192.168.49.2"
#                            (lets CI hit the Ingress without a hosts-file entry)
#   SMOKE_CHAT_MESSAGE       chat message to send          (default "What is Kubernetes?")
#   SMOKE_CLEANUP=1          delete the smoke user at the end (default 1; 0 keeps it)
#
# JSON parsing uses jq when available, otherwise falls back to python3/python.
# Exit codes: 0 = all checks passed, 1 = at least one check failed, 2 = usage error.
# =============================================================================
set -euo pipefail

usage() {
  awk 'NR > 2 && /^# =+$/ { exit } NR > 2 && /^#/ { sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"
}

BASE_URL="${1:-${BASE_URL:-}}"
if [[ -z "${BASE_URL}" || "${BASE_URL}" == "-h" || "${BASE_URL}" == "--help" ]]; then
  usage
  exit 2
fi
BASE_URL="${BASE_URL%/}"

INSECURE="${INSECURE:-0}"
HEALTH_RETRIES="${SMOKE_HEALTH_RETRIES:-10}"
MAX_BACKOFF="${SMOKE_MAX_BACKOFF:-30}"
REQ_TIMEOUT="${SMOKE_TIMEOUT:-15}"
RESOLVE="${SMOKE_RESOLVE:-}"
CHAT_MESSAGE="${SMOKE_CHAT_MESSAGE:-What is Kubernetes?}"
CLEANUP="${SMOKE_CLEANUP:-1}"
RUN_ID="$(date +%s)-$$-${RANDOM}"

PASSED=0
FAILED=0
WARNED=0
REQ_SEQ=0
HTTP_STATUS="000"
HTTP_BODY=""
CURL_ERROR=""

WORK_DIR="$(mktemp -d 2>/dev/null || mktemp -d -t scalebotai-smoke)"
BODY_FILE="${WORK_DIR}/body.json"
ERR_FILE="${WORK_DIR}/curl.err"
trap 'rm -rf "${WORK_DIR}"' EXIT

# ----------------------------------------------------------------------------- output helpers
if [[ -t 1 ]]; then
  C_GREEN=$'\033[32m'; C_RED=$'\033[31m'; C_YELLOW=$'\033[33m'; C_BOLD=$'\033[1m'; C_RESET=$'\033[0m'
else
  C_GREEN=""; C_RED=""; C_YELLOW=""; C_BOLD=""; C_RESET=""
fi

info() { printf '%s\n' "$*"; }
pass() { PASSED=$((PASSED + 1)); printf '%s[PASS]%s %s\n' "${C_GREEN}" "${C_RESET}" "$1"; }
warn() { WARNED=$((WARNED + 1)); printf '%s[WARN]%s %s\n' "${C_YELLOW}" "${C_RESET}" "$1"; }
fail() {
  FAILED=$((FAILED + 1))
  printf '%s[FAIL]%s %s\n' "${C_RED}" "${C_RESET}" "$1"
  if [[ -n "${2:-}" ]]; then
    printf '       %s\n' "$2"
  fi
}

# Short, single-line excerpt of the last response body / curl error for diagnostics.
detail() {
  local body="${HTTP_BODY//$'\n'/ }"
  if [[ "${HTTP_STATUS}" == "000" ]]; then
    printf 'connection error: %s' "${CURL_ERROR:-no response}"
  else
    printf 'HTTP %s body: %s' "${HTTP_STATUS}" "${body:0:300}"
  fi
}

finish() {
  info "-------------------------------------------------------------------------------"
  info "Smoke test summary: ${PASSED} passed, ${FAILED} failed, ${WARNED} warnings (target ${BASE_URL})"
  if (( FAILED > 0 )); then
    info "${C_RED}${C_BOLD}RESULT: FAIL${C_RESET}"
    exit 1
  fi
  info "${C_GREEN}${C_BOLD}RESULT: PASS${C_RESET}"
  exit 0
}

# ----------------------------------------------------------------------------- JSON helpers
PY_JSON='
import json, sys
mode, path = sys.argv[1], sys.argv[2]
try:
    data = json.load(sys.stdin)
except Exception:
    data = None
for part in [p for p in path.split(".") if p]:
    if isinstance(data, list) and part.isdigit():
        idx = int(part)
        data = data[idx] if idx < len(data) else None
    elif isinstance(data, dict):
        data = data.get(part)
    else:
        data = None
if mode == "len":
    print(len(data) if isinstance(data, list) else 0)
elif data is None:
    pass
elif isinstance(data, bool):
    print("true" if data else "false")
elif isinstance(data, (dict, list)):
    print(json.dumps(data))
else:
    print(data)
'

if command -v jq >/dev/null 2>&1; then
  JSON_TOOL="jq"
elif python3 -c 'import json' >/dev/null 2>&1; then
  JSON_TOOL="python3"
elif python -c 'import json' >/dev/null 2>&1; then
  JSON_TOOL="python"
else
  echo "ERROR: jq or python3 is required to parse JSON responses" >&2
  exit 2
fi

# json_get <dotted.path>  - reads JSON on stdin, prints the value (objects/arrays as JSON), empty if absent.
json_get() {
  if [[ "${JSON_TOOL}" == "jq" ]]; then
    jq -r --arg p "$1" '
      try (getpath($p | split(".") | map(if test("^[0-9]+$") then tonumber else . end))
           | if . == null then empty
             elif (type == "object" or type == "array") then tojson
             else tostring end)
      catch empty' 2>/dev/null || true
  else
    "${JSON_TOOL}" -c "${PY_JSON}" get "$1" 2>/dev/null || true
  fi
}

# json_len <dotted.path>  - prints the length of the array at path (0 if absent / not an array).
json_len() {
  local out
  if [[ "${JSON_TOOL}" == "jq" ]]; then
    out="$(jq -r --arg p "$1" '
      try (getpath($p | split(".") | map(if test("^[0-9]+$") then tonumber else . end))
           | if type == "array" then length else 0 end)
      catch 0' 2>/dev/null || true)"
  else
    out="$("${JSON_TOOL}" -c "${PY_JSON}" len "$1" 2>/dev/null || true)"
  fi
  printf '%s' "${out:-0}"
}

body_get() { printf '%s' "${HTTP_BODY}" | json_get "$1"; }
body_len() { printf '%s' "${HTTP_BODY}" | json_len "$1"; }

# ----------------------------------------------------------------------------- HTTP helper
CURL_OPTS=(--silent --show-error --max-time "${REQ_TIMEOUT}" --connect-timeout 5
           -H "Accept: application/json" -H "User-Agent: scalebotai-smoke-test/1.0")
if [[ "${INSECURE}" == "1" ]]; then
  CURL_OPTS+=(--insecure)
fi
if [[ -n "${RESOLVE}" ]]; then
  CURL_OPTS+=(--resolve "${RESOLVE}")
fi

# http_call <METHOD> <PATH> [JSON_BODY] [BEARER_TOKEN]
# Sets HTTP_STATUS / HTTP_BODY / CURL_ERROR. Never aborts the script (network error => 000).
http_call() {
  local method="$1" path="$2" data="${3:-}" token="${4:-}"
  REQ_SEQ=$((REQ_SEQ + 1))
  local -a args=("${CURL_OPTS[@]}" -X "${method}" -o "${BODY_FILE}" -w '%{http_code}'
                 -H "X-Request-ID: smoke-${RUN_ID}-${REQ_SEQ}")
  if [[ -n "${data}" ]]; then
    args+=(-H "Content-Type: application/json" --data "${data}")
  fi
  if [[ -n "${token}" ]]; then
    args+=(-H "Authorization: Bearer ${token}")
  fi
  : >"${BODY_FILE}"
  HTTP_STATUS="$(curl "${args[@]}" "${BASE_URL}${path}" 2>"${ERR_FILE}")" || true
  HTTP_STATUS="${HTTP_STATUS:-000}"
  HTTP_BODY="$(cat "${BODY_FILE}" 2>/dev/null || true)"
  CURL_ERROR="$(tr '\n' ' ' <"${ERR_FILE}" 2>/dev/null || true)"
}

# ----------------------------------------------------------------------------- checks
check_health() {
  local attempt=1 status="" version=""
  local delay=$(( MAX_BACKOFF < 2 ? MAX_BACKOFF : 2 ))
  while (( attempt <= HEALTH_RETRIES )); do
    http_call GET /health
    status=""
    if [[ "${HTTP_STATUS}" == "200" ]]; then
      status="$(body_get status)"
      if [[ "${status}" == "ok" || "${status}" == "degraded" ]]; then
        version="$(body_get version)"
        pass "GET /health -> 200, status=${status}, version=${version:-unknown} (attempt ${attempt}/${HEALTH_RETRIES})"
        if [[ "${status}" == "degraded" ]]; then
          warn "API reports DEGRADED health - checks: $(body_get checks)"
        fi
        return 0
      fi
    fi
    if (( attempt < HEALTH_RETRIES )); then
      info "       health attempt ${attempt}/${HEALTH_RETRIES}: HTTP ${HTTP_STATUS} status='${status}' - retrying in ${delay}s"
      sleep "${delay}"
      delay=$(( delay * 2 > MAX_BACKOFF ? MAX_BACKOFF : delay * 2 ))
    fi
    attempt=$((attempt + 1))
  done
  fail "GET /health did not return 200 with status ok|degraded after ${HEALTH_RETRIES} attempts" "$(detail)"
  return 1
}

# ----------------------------------------------------------------------------- main
info "==> ScaleBotAI smoke test against ${BASE_URL} (run ${RUN_ID}, json via ${JSON_TOOL})"

if ! check_health; then
  finish
fi

# Username must match ^[a-zA-Z0-9_.-]+$ (3-50 chars). The e-mail domain must NOT be a reserved /
# special-use TLD (.local, .test, .example, ...) because Pydantic EmailStr rejects those.
USERNAME="smoke_$(date +%s)_${RANDOM}"
EMAIL="${USERNAME}@scalebotai.dev"
PASSWORD="SmokeTest${RANDOM}${RANDOM}x9!"
TOKEN=""

# 2. Register ------------------------------------------------------------------
http_call POST /api/v1/auth/register \
  "{\"username\":\"${USERNAME}\",\"email\":\"${EMAIL}\",\"password\":\"${PASSWORD}\",\"full_name\":\"Smoke Test\"}"
if [[ "${HTTP_STATUS}" == "201" || "${HTTP_STATUS}" == "200" ]]; then
  pass "POST /api/v1/auth/register -> ${HTTP_STATUS} (user ${USERNAME}, id $(body_get id))"
else
  fail "POST /api/v1/auth/register expected 201, got ${HTTP_STATUS}" "$(detail)"
  finish
fi

# 3. Login ---------------------------------------------------------------------
http_call POST /api/v1/auth/login "{\"username\":\"${USERNAME}\",\"password\":\"${PASSWORD}\"}"
TOKEN="$(body_get access_token)"
if [[ "${HTTP_STATUS}" == "200" && -n "${TOKEN}" ]]; then
  pass "POST /api/v1/auth/login -> 200, token_type=$(body_get token_type), expires_in=$(body_get expires_in)s"
else
  fail "POST /api/v1/auth/login expected 200 with access_token, got ${HTTP_STATUS}" "$(detail)"
  finish
fi

# 4. Chat ----------------------------------------------------------------------
http_call POST /api/v1/chat "{\"message\":\"${CHAT_MESSAGE//\"/\\\"}\"}" "${TOKEN}"
BOT_RESPONSE="$(body_get response)"
BOT_PREVIEW="${BOT_RESPONSE//$'\n'/ }"
if (( ${#BOT_PREVIEW} > 60 )); then
  BOT_PREVIEW="${BOT_PREVIEW:0:60}..."
fi
# The API returns 200; 201 is tolerated in case the API reports message creation.
if [[ ( "${HTTP_STATUS}" == "200" || "${HTTP_STATUS}" == "201" ) && -n "${BOT_RESPONSE}" ]]; then
  pass "POST /api/v1/chat -> ${HTTP_STATUS}, bot_mode=$(body_get bot_mode), cached=$(body_get cached), latency_ms=$(body_get latency_ms), response='${BOT_PREVIEW}'"
else
  fail "POST /api/v1/chat expected 200 with a non-empty response, got ${HTTP_STATUS}" "$(detail)"
fi

# 5. History -------------------------------------------------------------------
http_call GET "/api/v1/chat/history?page=1&size=10" "" "${TOKEN}"
ITEMS="$(body_len items)"
TOTAL="$(body_get total)"
if [[ "${HTTP_STATUS}" == "200" ]] && { (( ITEMS >= 1 )) || [[ "${TOTAL:-0}" =~ ^[1-9][0-9]*$ ]]; }; then
  pass "GET /api/v1/chat/history -> 200, items=${ITEMS}, total=${TOTAL:-n/a}"
else
  fail "GET /api/v1/chat/history expected 200 with >= 1 item, got HTTP ${HTTP_STATUS} items=${ITEMS}" "$(detail)"
fi

# 6. Logout --------------------------------------------------------------------
http_call POST /api/v1/auth/logout "{}" "${TOKEN}"
if [[ "${HTTP_STATUS}" == "200" || "${HTTP_STATUS}" == "204" ]]; then
  pass "POST /api/v1/auth/logout -> ${HTTP_STATUS}"
  # Non-fatal: the revoked token should now be rejected (JWT denylist in Redis; fails open if Redis is down).
  http_call GET /api/v1/users/me "" "${TOKEN}"
  if [[ "${HTTP_STATUS}" == "401" ]]; then
    info "       revoked token correctly rejected by GET /api/v1/users/me (401)"
  else
    warn "revoked token still accepted by GET /api/v1/users/me (HTTP ${HTTP_STATUS}) - is Redis reachable?"
  fi
else
  fail "POST /api/v1/auth/logout expected 200/204, got ${HTTP_STATUS}" "$(detail)"
fi

# Clean-up (non-fatal): log in again and delete the smoke account (cascades chats/feedback).
if [[ "${CLEANUP}" == "1" ]]; then
  http_call POST /api/v1/auth/login "{\"username\":\"${USERNAME}\",\"password\":\"${PASSWORD}\"}"
  CLEANUP_TOKEN="$(body_get access_token)"
  if [[ -n "${CLEANUP_TOKEN}" ]]; then
    http_call DELETE /api/v1/users/me "" "${CLEANUP_TOKEN}"
  fi
  if [[ "${HTTP_STATUS}" == "204" || "${HTTP_STATUS}" == "200" ]]; then
    info "       clean-up: smoke user ${USERNAME} deleted"
  else
    warn "clean-up: could not delete smoke user ${USERNAME} (HTTP ${HTTP_STATUS}) - purge smoke_* users later"
  fi
fi

finish
