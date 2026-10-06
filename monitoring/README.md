# monitoring/ - metrics, dashboards and log shipping

Configuration for the observability stack of ScaleBotAI. Full explanation:
[docs/09-monitoring-logging.md](../docs/09-monitoring-logging.md). Load-test dashboards and
procedure: [docs/11-load-testing-strategy.md](../docs/11-load-testing-strategy.md).

| Path | What it is | Used by |
|---|---|---|
| `prometheus/prometheus.yml` | Scrape config: Prometheus itself, every `chatbot-api` replica via `dns_sd_configs` (type A, port 8000), `postgres-exporter:9187`, `redis-exporter:9121`, `nginx-exporter:9113` | compose -> `/etc/prometheus/prometheus.yml` |
| `prometheus/alert-rules.yml` | Recording rules (RPS, error ratio, P50/P95/P99, cache hit ratio) + alerts (API down, error rate > 1%, P95/avg > 300 ms, CPU, memory, cache, DB pool, rate limiting, Postgres/Redis/NGINX down, too many PG connections) | compose -> `/etc/prometheus/alert-rules.yml`; k8s copies the rules into its Prometheus ConfigMap |
| `grafana/provisioning/datasources/datasources.yml` | Prometheus (uid `prometheus`, default) and Elasticsearch (uid `elasticsearch`, index `scalebot-*-logs-*`) | compose -> `/etc/grafana/provisioning/datasources/` |
| `grafana/provisioning/dashboards/dashboards.yml` | File provider: loads `/var/lib/grafana/dashboards/*.json` into folder "ScaleBotAI" | compose -> `/etc/grafana/provisioning/dashboards/` |
| `grafana/dashboards/scalebotai-api-overview.json` | Golden signals of the API (RPS, P50/P95/P99, errors, CPU, memory, chat/cache/auth/DB pool) | Grafana |
| `grafana/dashboards/scalebotai-load-testing.json` | Load-test war room (k6 VUs, RPS, latency vs 300 ms, errors, saturation, PG, Redis, NGINX, HPA) | Grafana |
| `grafana/dashboards/scalebotai-infrastructure.json` | PostgreSQL, Redis, NGINX exporters | Grafana |
| `logstash/config/logstash.yml` | Logstash settings (HTTP API on 9600, 2 workers, monitoring off) | compose -> `/usr/share/logstash/config/logstash.yml` |
| `logstash/pipeline/logstash.conf` | Inputs `file` (`/var/log/scalebotai/*.log`, compose) + `beats` (5044, k8s) -> normalise -> `scalebot-{log_type}-logs-YYYY.MM.dd` | compose -> `/usr/share/logstash/pipeline/`; k8s ConfigMap |
| `filebeat/filebeat.yml` | Reference DaemonSet config: tails `/var/log/containers/*scalebotai-api*.log`, adds k8s metadata, decodes JSON, ships to `logstash.logging.svc:5044` | k8s only |
| `kibana/create-data-views.sh` | Creates ILM policy, index template and Kibana data views | run manually once |
| `kibana/README.md` | KQL queries and suggested Kibana visualizations | humans |

## Docker Compose

```bash
docker compose up -d                       # whole stack
docker compose up -d --scale chatbot-api=3 # Prometheus finds all 3 replicas via DNS
bash monitoring/kibana/create-data-views.sh
```

| UI | URL | Notes |
|---|---|---|
| Grafana | http://localhost:3000 | user `admin`, password = `GRAFANA_ADMIN_PASSWORD` from `.env`; folder "ScaleBotAI" |
| Prometheus | http://localhost:9090 | `/targets`, `/alerts`, `/rules` |
| Kibana | http://localhost:5601 | data views `scalebot-*-logs-*` |
| Elasticsearch | http://127.0.0.1:9200 | bound to localhost only; security disabled for local dev |

Validate configs after editing:

```bash
docker run --rm -v "$PWD/monitoring/prometheus:/etc/prometheus" --entrypoint promtool \
  prom/prometheus:latest check config /etc/prometheus/prometheus.yml
docker compose exec logstash bin/logstash --config.test_and_exit \
  -f /usr/share/logstash/pipeline/logstash.conf --path.data /tmp/lstest
python -m json.tool monitoring/grafana/dashboards/scalebotai-api-overview.json > /dev/null
```

## Kubernetes

- Prometheus (namespace `monitoring`) scrapes pods annotated `prometheus.io/scrape: "true"`
  (`prometheus.io/port: "8000"`, `prometheus.io/path: "/metrics"`). Keep the job label
  `scalebotai-api` so the rules and dashboards above work unchanged (the dashboards also
  offer a `$job` variable).
- The same dashboards are loaded into Grafana via ConfigMaps; panels that need
  kube-state-metrics (HPA replicas, restarts) or cAdvisor (network, CPU throttling) are
  marked as such and stay empty in Docker Compose.
- Filebeat DaemonSet (namespace `logging`) -> Logstash `beats` input :5044 -> Elasticsearch.
- k6 metrics: run Prometheus with `--web.enable-remote-write-receiver` and k6 with
  `-o experimental-prometheus-rw` (see `tests/load/k6/README.md`).
