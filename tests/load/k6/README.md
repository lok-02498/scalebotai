# k6 load tests (primary tool)

k6 is the primary load-testing tool for ScaleBotAI: test scripts are JavaScript, the engine is
Go (thousands of VUs per machine), thresholds turn SLOs into pass/fail exit codes for CI, and
results can be streamed into Prometheus/Grafana. Strategy, maths and pass/fail criteria:
[docs/11-load-testing-strategy.md](../../../docs/11-load-testing-strategy.md).

```
tests/load/k6/
  lib/common.js               shared helpers: config, register/login, chat, history, checks, metrics, summary
  smoke.js                    100 VUs, 2 min       - does it work at all?
  baseline.js                 1,000 VUs, 10 min    - reference numbers, per-pod capacity
  load.js                     -> 10,000 VUs, 30 min - SLO at high load while the HPA scales
  stress.js                   steps to 25,000 VUs  - breaking point and first bottleneck
  spike.js                    1,000 -> 50,000 -> 1,000 VUs - graceful degradation + recovery
  soak.js                     500 VUs, 24 h        - leaks and drift over time
  k6-operator-testrun.yaml    distributed runs in Kubernetes (25k-50k VUs)
  results/                    handleSummary output (JSON + text), one file per run
```

## 1. Install k6

| OS | Command |
|---|---|
| Windows | `winget install k6 --source winget` or `choco install k6` |
| macOS | `brew install k6` |
| Debian/Ubuntu | `sudo gpg -k && sudo gpg --no-default-keyring --keyring /usr/share/keyrings/k6-archive-keyring.gpg --keyserver hkp://keyserver.ubuntu.com:80 --recv-keys C5AD17C747E3415A3642D57D77C6C491D6AC1D69 && echo "deb [signed-by=/usr/share/keyrings/k6-archive-keyring.gpg] https://dl.k6.io/deb stable main" \| sudo tee /etc/apt/sources.list.d/k6.list && sudo apt-get update && sudo apt-get install k6` |
| Docker | `docker run --rm -i -v "$PWD:/work" -w /work grafana/k6 run tests/load/k6/smoke.js` |

Check with `k6 version` (v0.45 or newer is required for per-phase tags; v0.54+ recommended).
`lib/common.js` imports `textSummary` from `https://jslib.k6.io` - the generator needs Internet
access (or use `k6 archive` once and run the archive offline).

## 2. Prepare the system under test

1. **Rate limits.** The API allows `RATE_LIMIT_PER_MINUTE=120` per user/IP and NGINX applies
   `limit_req`. All VUs from one generator share one source IP for register/login, so even the
   smoke test trips the limit. For the test environment set `RATE_LIMIT_PER_MINUTE=100000`
   (or `RATE_LIMIT_ENABLED=false`) and relax/allow-list the NGINX/Ingress limits for the
   generator IPs - or distribute generators across many source IPs. Never do this in production.
2. **Hostname / TLS.** Add `127.0.0.1 api.scalebotai.local` (compose) or the Ingress IP (k8s) to
   the hosts file (`C:\Windows\System32\drivers\etc\hosts` on Windows). The certificate is
   self-signed, so every script sets `insecureSkipTLSVerify: true`.
3. **Monitoring.** Open Grafana -> "ScaleBotAI - Load Testing War Room" before starting.
4. Results directory: run k6 from the repository root (handleSummary writes to
   `tests/load/k6/results/`, override with `-e RESULTS_DIR=...`).

## 3. Run

```bash
# from the repository root
k6 run tests/load/k6/smoke.js
k6 run tests/load/k6/baseline.js
k6 run tests/load/k6/load.js
k6 run tests/load/k6/stress.js
k6 run tests/load/k6/spike.js
k6 run tests/load/k6/soak.js

# another target
k6 run -e BASE_URL=https://api.scalebotai.local tests/load/k6/smoke.js
k6 run -e BASE_URL=http://localhost:8000 tests/load/k6/smoke.js       # straight to a uvicorn dev server

# same profile shape with fewer VUs (laptop rehearsal): SCALE multiplies every VU target
k6 run -e SCALE=0.1 tests/load/k6/load.js       # 10,000 -> 1,000 VUs
k6 run -e SCALE=0.04 tests/load/k6/stress.js    # 25,000 -> 1,000 VUs
k6 run -e DURATION=2h tests/load/k6/soak.js     # shorter soak rehearsal
```

PowerShell: `k6 run -e BASE_URL=https://api.scalebotai.local tests/load/k6/smoke.js` works the
same way (the `-e` flags are k6 flags, not shell variables).

### Environment variables (`-e NAME=value`)

| Variable | Default | Meaning |
|---|---|---|
| `BASE_URL` | `https://api.scalebotai.local` | Target (NGINX in compose, Ingress in k8s) |
| `VUS` | per script | Peak VUs (spike: spike VUs; `BASE_VUS` = 1,000 baseline) |
| `SCALE` | `1` | Multiplier applied to every VU target |
| `DURATION` | per script | Hold duration (baseline, soak) |
| `AUTH_MODE` | `per-vu` | `per-vu`: every VU registers + logs in its own user; `pool`: `setup()` creates `POOL_SIZE` users and VUs share their tokens (no login storm) |
| `POOL_SIZE` | `200` | Accounts created in pool mode |
| `THINK_MIN` / `THINK_MAX` | `1` / `3` s (soak: `3` / `10`) | Think time between actions |
| `UNIQUE_QUESTION_RATIO` | `0.2` | Share of chat questions made unique (cache misses) |
| `TOKEN_REFRESH_SECONDS` | `1500` | Refresh access tokens after 25 min (they expire after 30) |
| `ABORT_ON_BREAK` | `true` | stress.js: abort when > 10 % of requests fail |
| `EMAIL_DOMAIN` | `scalebotai.dev` | Domain of generated e-mails (reserved TLDs like `.local` are rejected by the API) |
| `RESULTS_DIR` | `tests/load/k6/results` | Where JSON/text summaries are written |

### What a VU does

`setup()` checks `GET /health` (the run aborts if it is not 200). Each VU then registers a unique
user (`loadtest_<VU>_<ITER>_<random>` / `...@scalebotai.dev`), logs in once, and loops:
`POST /api/v1/chat` 60 % (continuing the current conversation 70 % of the time),
`GET /api/v1/chat/history` 20 %, `GET /api/v1/chat/conversations` 10 %, `GET /api/v1/users/me`
10 %, each followed by 1-3 s think time. Tokens are refreshed before they expire and a 401 triggers
a new login.

### Thresholds (pass/fail)

| Metric | Threshold | Source |
|---|---|---|
| `http_req_duration` | `avg<300`, `p(95)<500` | success criterion: avg < 300 ms |
| `http_req_failed` | `rate<0.01` | error rate < 1 % |
| `checks` | `rate>0.99` | functional correctness under load |
| `chat_duration`, `chat_success` | `avg<300`, `p(95)<500` / `rate>0.99` | the core user action |
| stress: `http_req_duration{phase:step-N}` | per step | the first failing step = breaking point |
| spike: `{phase:baseline}`, `{phase:recovery}` | SLO; `{phase:spike}` errors < 10 % | graceful degradation and recovery |

k6 exits with code 99 when a threshold fails - CI can gate on it.

Custom metrics: `chat_duration`, `history_duration`, `login_duration`, `register_duration`
(Trends), `chat_success`, `chat_cached` (Rates), `auth_failures`, `rate_limited_responses`,
`server_errors` (Counters).

## 4. Results

Each run writes `results/<test>-<timestamp>.json` (full metrics), `results/<test>-<timestamp>.txt`
(the end-of-test summary) and `results/<test>-latest.json`. Copy the key numbers into the report
template in `docs/11-load-testing-strategy.md`. For a raw time series use
`k6 run --out json=tests/load/k6/results/raw.json ...` (large files) or the Prometheus output below.

## 5. k6 metrics in Prometheus / Grafana (remote write)

1. Start Prometheus with the remote-write receiver enabled:
   `--web.enable-remote-write-receiver` (compose: add it to the prometheus `command:`; k8s: to the
   container args).
2. Run k6 with the experimental Prometheus remote-write output:

```bash
# bash
K6_PROMETHEUS_RW_SERVER_URL=http://localhost:9090/api/v1/write \
K6_PROMETHEUS_RW_TREND_STATS="p(95),p(99),avg" \
k6 run -o experimental-prometheus-rw --tag testid=baseline-001 tests/load/k6/baseline.js
```

```powershell
# PowerShell
$env:K6_PROMETHEUS_RW_SERVER_URL = "http://localhost:9090/api/v1/write"
$env:K6_PROMETHEUS_RW_TREND_STATS = "p(95),p(99),avg"
k6 run -o experimental-prometheus-rw --tag testid=baseline-001 tests/load/k6/baseline.js
```

The war-room dashboard then shows `k6_vus`, `k6_http_reqs_total`, `k6_http_req_duration_p95/p99`
next to the server-side metrics. The `testid` tag lets you filter one run.

## 6. Distributed runs (25,000-50,000 VUs)

A single k6 process handles roughly 5,000-10,000 VUs of this workload (about 1-3 MB RAM per VU,
CPU for TLS and JSON). Stress (25k) and spike (50k) therefore need several generators:

- **Kubernetes - k6-operator (recommended).** `k6-operator-testrun.yaml` contains two `TestRun`
  resources: `scalebotai-stress` (`parallelism: 25`) and `scalebotai-spike` (`parallelism: 50`).
  The operator splits the VUs between runner pods and starts them simultaneously. Steps:

  ```bash
  kubectl apply --server-side -f https://raw.githubusercontent.com/grafana/k6-operator/main/bundle.yaml
  k6 archive tests/load/k6/stress.js -O stress.tar
  k6 archive tests/load/k6/spike.js  -O spike.tar
  kubectl create namespace loadtest
  kubectl -n loadtest create configmap scalebotai-k6-scripts --from-file=stress.tar --from-file=spike.tar
  kubectl apply -f tests/load/k6/k6-operator-testrun.yaml
  kubectl -n loadtest get testrun,pods -w
  kubectl -n loadtest logs -l k6_cr=scalebotai-stress --tail=50     # end-of-test summaries
  ```

  Run the generators on nodes (or a cluster) separate from the system under test, otherwise
  they compete for the same CPU and distort the result. Depending on the operator version
  `setup()` runs once or once per runner (pool mode then creates one pool per runner).
- **Manual execution segments.** On N machines run the same script with
  `--execution-segment "0:1/4" --execution-segment-sequence "0,1/4,2/4,3/4,1"` (then `1/4:2/4`, ...)
  and start them at the same time; aggregate via the Prometheus output.
- **Grafana Cloud k6** (`k6 cloud run ...`) is the managed alternative.

## 7. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Many `429` (`rate_limited_responses` > 0) | Rate limits still active - see section 2 |
| `register: status 201` check fails with 422 | E-mail domain rejected / username rules - keep `EMAIL_DOMAIN` a normal domain |
| `x509: certificate signed by unknown authority` | Run with the provided scripts (they set `insecureSkipTLSVerify`) |
| `dial: i/o timeout`, `connection reset` on the generator | Generator out of ports/CPU: `ulimit -n 250000`, more generators, `sysctl -w net.ipv4.ip_local_port_range="1024 65535"` |
| Latency high only at the start | bcrypt login storm (2 hashes per new VU) - ramp slower or `AUTH_MODE=pool` |
| Phase thresholds show no data | k6 older than v0.45 (no `exec.vu.metrics.tags`) - upgrade |
