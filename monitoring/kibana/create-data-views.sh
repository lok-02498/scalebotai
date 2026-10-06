#!/usr/bin/env bash
# =============================================================================
# ScaleBotAI - Kibana / Elasticsearch bootstrap
# -----------------------------------------------------------------------------
# 1. (optional) creates an ILM policy + index template for scalebot-*-logs-*
#    in Elasticsearch (retention + correct field types)
# 2. creates the Kibana data views used by the team:
#      scalebot-api-logs-*     API access logs
#      scalebot-user-logs-*    user/auth events
#      scalebot-error-logs-*   errors (level >= ERROR, 5xx)
#      scalebot-system-logs-*  startup/shutdown/dependency events
#      scalebot-*-logs-*       everything (set as default data view)
#
# Usage (from the repo root, with the compose stack running):
#   bash monitoring/kibana/create-data-views.sh
#   KIBANA_URL=http://localhost:5601 ES_URL=http://localhost:9200 bash monitoring/kibana/create-data-views.sh
#   SETUP_ILM=false bash monitoring/kibana/create-data-views.sh      # data views only
# Kubernetes: kubectl -n logging port-forward svc/kibana 5601:5601 (and
#   svc/elasticsearch 9200:9200), then run the same command.
#
# Safe to re-run: data views are created with override=true, PUT calls are idempotent.
# Local dev only: Elasticsearch/Kibana security is disabled in docker-compose. If
# security is enabled, export KIBANA_AUTH="elastic:<password>".
# =============================================================================
set -euo pipefail

KIBANA_URL="${KIBANA_URL:-http://localhost:5601}"
ES_URL="${ES_URL:-http://localhost:9200}"
SETUP_ILM="${SETUP_ILM:-true}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"          # delete log indices after N days
WAIT_SECONDS="${WAIT_SECONDS:-180}"
KIBANA_AUTH="${KIBANA_AUTH:-}"                    # user:password, empty = no auth

CURL_AUTH=()
if [[ -n "${KIBANA_AUTH}" ]]; then
  CURL_AUTH=(-u "${KIBANA_AUTH}")
fi

log()  { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
fail() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }

command -v curl >/dev/null 2>&1 || fail "curl is required"

# --- wait until a URL answers 200 ---------------------------------------------
wait_for() {
  local name="$1" url="$2" waited=0
  log "Waiting for ${name} at ${url} ..."
  until [[ "$(curl -s -o /dev/null -w '%{http_code}' ${CURL_AUTH[@]+"${CURL_AUTH[@]}"} "${url}" || true)" == "200" ]]; do
    if (( waited >= WAIT_SECONDS )); then
      fail "${name} not ready after ${WAIT_SECONDS}s (${url})"
    fi
    sleep 5
    waited=$(( waited + 5 ))
  done
  log "${name} is ready."
}

# --- generic JSON request; prints HTTP code, fails on >= 400 --------------------
request() {
  local method="$1" url="$2" body="$3"
  shift 3
  local tmp code
  tmp="$(mktemp)"
  code="$(curl -s -o "${tmp}" -w '%{http_code}' -X "${method}" ${CURL_AUTH[@]+"${CURL_AUTH[@]}"} \
            -H 'Content-Type: application/json' "$@" --data "${body}" "${url}" || true)"
  if [[ ! "${code}" =~ ^[0-9]{3}$ || "${code}" == "000" || "${code}" -ge 400 ]]; then
    printf '  -> HTTP %s from %s %s\n' "${code}" "${method}" "${url}" >&2
    cat "${tmp}" >&2 || true
    printf '\n' >&2
    rm -f "${tmp}"
    return 1
  fi
  rm -f "${tmp}"
  printf '  -> HTTP %s\n' "${code}"
}

# --- 1. Elasticsearch: ILM policy + index template -----------------------------
setup_ilm() {
  wait_for "Elasticsearch" "${ES_URL}/_cluster/health"

  log "Creating ILM policy scalebot-logs-policy (hot -> delete after ${RETENTION_DAYS}d)"
  request PUT "${ES_URL}/_ilm/policy/scalebot-logs-policy" "$(cat <<JSON
{
  "policy": {
    "phases": {
      "hot":    { "min_age": "0ms", "actions": { "set_priority": { "priority": 100 } } },
      "warm":   { "min_age": "3d",  "actions": { "forcemerge": { "max_num_segments": 1 }, "set_priority": { "priority": 50 } } },
      "delete": { "min_age": "${RETENTION_DAYS}d", "actions": { "delete": {} } }
    }
  }
}
JSON
)"

  log "Creating index template scalebot-logs (pattern scalebot-*-logs-*)"
  request PUT "${ES_URL}/_index_template/scalebot-logs" "$(cat <<'JSON'
{
  "index_patterns": ["scalebot-*-logs-*"],
  "priority": 200,
  "template": {
    "settings": {
      "number_of_shards": 1,
      "number_of_replicas": 0,
      "index.lifecycle.name": "scalebot-logs-policy",
      "index.refresh_interval": "5s"
    },
    "mappings": {
      "dynamic": true,
      "properties": {
        "@timestamp":  { "type": "date" },
        "level":       { "type": "keyword" },
        "logger":      { "type": "keyword" },
        "message":     { "type": "text", "fields": { "keyword": { "type": "keyword", "ignore_above": 512 } } },
        "service":     { "type": "keyword" },
        "environment": { "type": "keyword" },
        "hostname":    { "type": "keyword" },
        "log_type":    { "type": "keyword" },
        "request_id":  { "type": "keyword" },
        "user_id":     { "type": "keyword" },
        "method":      { "type": "keyword" },
        "path":        { "type": "keyword" },
        "route":       { "type": "keyword" },
        "status_code": { "type": "integer" },
        "latency_ms":  { "type": "float" },
        "client_ip":   { "type": "ip", "ignore_malformed": true },
        "user_agent":  { "type": "text", "fields": { "keyword": { "type": "keyword", "ignore_above": 256 } } },
        "event":       { "type": "keyword" },
        "exc_info":    { "type": "text" },
        "original_log_type": { "type": "keyword" },
        "tags":        { "type": "keyword" }
      }
    }
  },
  "_meta": { "description": "ScaleBotAI application logs written by Logstash" }
}
JSON
)"
  log "Note: the template applies to indices created from now on (tomorrow's daily index at the latest)."
}

# --- 2. Kibana data views ------------------------------------------------------
create_data_view() {
  local id="$1" title="$2" name="$3"
  log "Data view '${name}' (${title})"
  request POST "${KIBANA_URL}/api/data_views/data_view" "$(cat <<JSON
{
  "override": true,
  "data_view": {
    "id": "${id}",
    "title": "${title}",
    "name": "${name}",
    "timeFieldName": "@timestamp"
  }
}
JSON
)" -H 'kbn-xsrf: true'
}

main() {
  if [[ "${SETUP_ILM}" == "true" ]]; then
    setup_ilm
  else
    log "SETUP_ILM=false - skipping ILM policy and index template"
  fi

  wait_for "Kibana" "${KIBANA_URL}/api/status"

  create_data_view "scalebot-api-logs"    "scalebot-api-logs-*"    "ScaleBotAI - API logs"
  create_data_view "scalebot-user-logs"   "scalebot-user-logs-*"   "ScaleBotAI - User logs"
  create_data_view "scalebot-error-logs"  "scalebot-error-logs-*"  "ScaleBotAI - Error logs"
  create_data_view "scalebot-system-logs" "scalebot-system-logs-*" "ScaleBotAI - System logs"
  create_data_view "scalebot-all-logs"    "scalebot-*-logs-*"      "ScaleBotAI - All logs"

  log "Setting 'ScaleBotAI - All logs' as the default data view"
  request POST "${KIBANA_URL}/api/data_views/default" \
    '{"data_view_id": "scalebot-all-logs", "force": true}' -H 'kbn-xsrf: true'

  log "Done. Open ${KIBANA_URL}/app/discover and pick a ScaleBotAI data view."
}

main "$@"
