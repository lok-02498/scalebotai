# Kibana - ScaleBotAI log analysis

Kibana (http://localhost:5601 in Docker Compose, `kubectl -n logging port-forward svc/kibana 5601:5601`
in Kubernetes) is where the JSON logs that Logstash writes to Elasticsearch get searched.

## 1. Bootstrap (run once per environment)

```bash
# from the repository root, with the stack up (docker compose up -d)
bash monitoring/kibana/create-data-views.sh
```

The script:

1. creates the ILM policy `scalebot-logs-policy` (hot -> warm after 3 d -> delete after
   `RETENTION_DAYS`, default 14) and the index template `scalebot-logs` for `scalebot-*-logs-*`
   (keyword/integer/float/ip mappings for the API's log fields). Skip with `SETUP_ILM=false`.
2. creates these data views (time field `@timestamp`) and makes "All logs" the default:

| Data view id | Index pattern | Contents |
|---|---|---|
| `scalebot-api-logs` | `scalebot-api-logs-*` | one access-log line per HTTP request (method, route, status_code, latency_ms, client_ip, user_agent, request_id) |
| `scalebot-user-logs` | `scalebot-user-logs-*` | register, login (success/failure), logout, refresh, password change, account deletion |
| `scalebot-error-logs` | `scalebot-error-logs-*` | level >= ERROR, 5xx responses, exceptions (`exc_info`); copies of ERROR events logged under other types are tagged `error_copy` |
| `scalebot-system-logs` | `scalebot-system-logs-*` | startup/shutdown, DB/Redis/Ollama connectivity, table creation |
| `scalebot-all-logs` | `scalebot-*-logs-*` | everything |

Environment overrides: `KIBANA_URL` (default `http://localhost:5601`), `ES_URL`
(default `http://localhost:9200`), `KIBANA_AUTH=user:password` if security is enabled.

## 2. Useful KQL queries (Discover -> search bar)

| Goal | Data view | KQL |
|---|---|---|
| All errors in the last 15 minutes (set time picker to "Last 15 minutes") | All logs | `log_type:error or level:(ERROR or CRITICAL)` |
| Server errors only | API logs | `status_code >= 500` |
| 5xx for one route | API logs | `status_code >= 500 and route:"/api/v1/chat"` |
| Slow requests (above the 300 ms SLO) | API logs | `latency_ms > 300` |
| Very slow chat calls | API logs | `route:"/api/v1/chat" and method:POST and latency_ms > 1000` |
| Failed logins | User logs | `event:login_failure` |
| Brute force from one IP | User logs | `event:login_failure and client_ip:"172.18.0.5"` |
| Everything one user did | All logs | `user_id:"3f1c2a9e-0b7d-4c1e-9a55-2f0e8d1b6c11"` |
| Per-user activity on chat routes | API logs | `user_id:* and route:/api/v1/chat*` |
| Follow one request end-to-end | All logs | `request_id:"b8f1e3d2-6a4c-4c9e-8f0a-1d2e3f4a5b6c"` |
| Rate-limited clients | API logs | `status_code:429` |
| Auth failures (expired/invalid tokens) | API logs | `status_code:401 and not route:"/api/v1/auth/login"` |
| Exceptions with stack traces | Error logs | `exc_info:*` |
| Errors promoted from another log type | Error logs | `tags:error_copy` |
| LLM fallbacks (Ollama timeout, `CHATBOT_MODE=llm`) | All logs | `message:*fallback* or message:*ollama*` |
| Dependency problems at startup | System logs | `level:(WARNING or ERROR) and (message:*redis* or message:*database*)` |
| One replica only | All logs | `hostname:"scalebotai-api-7c9f8d6b5-x2k4p"` |
| Kubernetes pod (Filebeat metadata) | All logs | `kubernetes.pod.name:scalebotai-api-*` |

Tips: add the columns `level`, `log_type`, `route`, `status_code`, `latency_ms`, `user_id`,
`message` in Discover and save the search (e.g. "API - slow requests"); use the
`request_id` value from an `X-Request-ID` response header to jump straight to a request.

## 3. Suggested visualizations / dashboard ("ScaleBotAI - Logs")

Build them with **Lens** on the data views above:

| Visualization | Type | Configuration |
|---|---|---|
| Log volume by type | Stacked bar over time | Data view All logs, X = `@timestamp`, break down by `log_type` |
| Errors over time | Line | Error logs, count, break down by `route` (top 5) |
| Status code distribution | Donut | API logs, slice by `status_code` (top 10) |
| Latency percentiles | Line | API logs, P50/P95/P99 of `latency_ms` over `@timestamp`, reference line at 300 |
| Slowest routes | Table | API logs, rows `route`, metrics avg + P95 `latency_ms`, count; sort by P95 |
| 5xx by route | Horizontal bar | API logs, filter `status_code >= 500`, rows `route` |
| Login success vs failure | Bar | User logs, count by `event` (`login_success`, `login_failure`) |
| Top failing client IPs | Table | User logs, filter `event:login_failure`, rows `client_ip` |
| Most active users | Table | API logs, rows `user_id` (top 20), count |
| Requests per replica | Bar | API logs, rows `hostname` - checks NGINX/Service load balancing |
| Recent errors | Saved search panel | Error logs, columns `@timestamp level route message request_id` |

## 4. Retention

Daily indices + ILM (`scalebot-logs-policy`): delete after 14 days by default. API logs
are by far the largest index (one document per request: ~1,000,000 documents/day at the
design load, roughly 0.5-1 GB/day). Keep error/user logs longer if audit requirements demand
it by giving those patterns their own policy. See `docs/09-monitoring-logging.md`.
