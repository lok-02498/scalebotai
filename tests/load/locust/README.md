# Locust load tests: ScaleBotAI

[`locustfile.py`](locustfile.py) simulates learners using the DevOps assistant:

1. **on_start**: each simulated user registers a new account (`POST /api/v1/auth/register`,
   username `lt_<8 hex>_<n>`, email `<username>@scalebotai.dev`) and logs in
   (`POST /api/v1/auth/login`). The `access_token` is sent as `Authorization: Bearer ...`
   on every later request. If registration or login fails, the failure is recorded and only
   that user stops (`StopUser`). The test keeps running.
2. **Tasks** (weighted, 1–3 s think time between tasks):

   | Weight | Request | Locust name | Checks |
   |---:|---|---|---|
   | 6 | `POST /api/v1/chat` | `/api/v1/chat` | 200 and JSON has `response` + `conversation_id`; ~70 % of messages continue the previous conversation |
   | 2 | `GET /api/v1/chat/history?page=1&size=20` | `/api/v1/chat/history` | 200 and `items` |
   | 1 | `GET /api/v1/chat/conversations?page=1&size=20` | `/api/v1/chat/conversations` | 200 and `items` |
   | 1 | `GET /api/v1/users/me` | `/api/v1/users/me` | 200 and `id`, `username` |

   - **401:** recorded as a failure. The user logs in again and retries once.
   - **429:** recorded as a failure with the message `rate limited (raise RATE_LIMIT_PER_MINUTE for load tests)`.
   - **Token expiry:** the access token is renewed through `POST /api/v1/auth/refresh` about 60 s before it expires, which matters for the 24 h soak test.
3. **Questions:** a pool of 30 realistic DevOps/cloud questions, including greetings and `help`.
   About 20 % of chats repeat a pool question verbatim, so they hit the Redis cache
   (`bot:resp:*`) after warm-up. The rest have a random context sentence appended, so they
   are cache misses. The resulting cache hit ratio is about `LOCUST_REPEAT_RATIO`.

On average one user sends about 1 request per (2 s think time + latency), or **≈0.5 req/s**.
So 1,000 users ≈ 500 req/s, which is the platform's design headroom. The design target of
1,000,000 req/day works out to an average of 11.6 req/s and a 10× peak of about 116 req/s.

## 1. Install

Use a separate virtual environment, not the API's one:

```bash
python -m venv .venv-locust
source .venv-locust/bin/activate          # Windows PowerShell: .venv-locust\Scripts\Activate.ps1
pip install locust
locust -V
```

The default target `https://api.scalebotai.local` must resolve. Add a hosts entry
(`/etc/hosts` or `C:\Windows\System32\drivers\etc\hosts`):

```text
127.0.0.1      api.scalebotai.local   # docker compose (NGINX on 443)
<ingress-ip>   api.scalebotai.local   # Kubernetes ingress-nginx
```

## 2. Prepare the target environment (important)

- **Application rate limit.** The API allows `RATE_LIMIT_PER_MINUTE=120` requests per minute
  per user, or per client IP for anonymous routes.
  - Chat traffic stays under that: one simulated user makes about 30 req/min.
  - Register and login are anonymous, though. A few load-generator IPs therefore hit the limit within seconds and get 429.
  - For the test environment, set `RATE_LIMIT_PER_MINUTE=100000` or `RATE_LIMIT_ENABLED=false`. Never change this in production.
- **NGINX `limit_req`.** The NGINX/ingress limit also applies per source IP. Raise it, or
  allow-list the generator IPs, for the duration of the test. Otherwise NGINX returns 429/503
  before the API sees the request.
- **Self-signed TLS.** NGINX generates a self-signed certificate. `LOCUST_INSECURE=true` is the
  default: certificate verification is off and urllib3 warnings are silenced. Set it to
  `false` against a trusted certificate.
- **bcrypt cost.** Every new user costs two bcrypt hashes on the server: one for register,
  one for login. With `BCRYPT_ROUNDS=12` that is roughly 0.25 s of CPU each. The spawn rate
  is therefore also an authentication storm.
  - Keep spawn rates modest.
  - Or, in a disposable test environment only, lower `BCRYPT_ROUNDS` when you want to measure the chat path rather than authentication.
- **Watch the platform while testing.** Use the Grafana dashboards: latency, error rate,
  HPA replicas, CPU and memory below 80 %, DB pool, Redis hit ratio. Use Kibana for
  `scalebot-error-logs-*`.

## 3. Configuration

| Variable | Default | Meaning |
|---|---|---|
| `BASE_URL` | `https://api.scalebotai.local` | Target when `--host` is not given. `--host` wins. |
| `LOCUST_INSECURE` | `true` | Skip TLS certificate verification (self-signed certs). |
| `LOCUST_SHAPE` | *(unset)* = `none` | Load profile: `smoke`, `baseline`, `load`, `step` (alias `stress`), `spike`, `soak`. Unset or `none` = use `-u/-r/-t`. An unknown value aborts. |
| `LOCUST_REPEAT_RATIO` | `0.2` | Share of chat messages that repeat a pool question verbatim (cache hits). |
| `LOCUST_CONTINUE_RATIO` | `0.7` | Share of chat messages that continue the previous conversation. |
| `LOCUST_WAIT_MIN` / `LOCUST_WAIT_MAX` | `1` / `3` | Think time between tasks (seconds). Use `3` / `10` for the soak profile so it runs at realistic (~75 req/s), not peak, load - see docs/11-load-testing-strategy.md. |
| `LOADTEST_EMAIL_DOMAIN` | `scalebotai.dev` | Email domain for generated users. Reserved TLDs such as `.local`/`.test` are rejected by the API's `EmailStr`. |
| `LOADTEST_PASSWORD` | `LoadTest123!` | Password for generated users. Must have ≥8 characters, a letter and a digit. Test-only. |
| `SLO_MAX_FAIL_RATIO` | `0.01` | Exit code 1 when the failure ratio is above this (success criterion: < 1 %). |
| `SLO_MAX_AVG_MS` | `300` | Exit code 1 when the average response time is above this (success criterion: < 300 ms). |

When Locust quits, it logs `SLO passed` or `SLO FAILED` with the request count, failure
ratio, average and p95. The process exits with code 1 on failure, so it can gate a CI or
Jenkins stage. The averages include the register/login requests.

## 4. Run headless (CI-friendly)

The results directory must exist before the run:

```bash
mkdir -p tests/load/locust/results        # PowerShell: New-Item -ItemType Directory -Force tests/load/locust/results
```

Smoke test (100 users, 2 minutes):

```bash
locust -f tests/load/locust/locustfile.py --headless -u 100 -r 10 -t 2m \
  --host https://api.scalebotai.local \
  --csv tests/load/locust/results/smoke --html tests/load/locust/results/smoke.html
```

Baseline (1,000 users, 10 minutes):

```bash
locust -f tests/load/locust/locustfile.py --headless -u 1000 -r 50 -t 10m \
  --host https://api.scalebotai.local \
  --csv tests/load/locust/results/baseline --html tests/load/locust/results/baseline.html
```

PowerShell uses a backtick for line continuation, or you can put everything on one line:

```powershell
locust -f tests/load/locust/locustfile.py --headless -u 1000 -r 50 -t 10m `
  --host https://api.scalebotai.local `
  --csv tests/load/locust/results/baseline --html tests/load/locust/results/baseline.html
```

The `--csv <prefix>` option writes `<prefix>_stats.csv`, `<prefix>_stats_history.csv`,
`<prefix>_failures.csv` and `<prefix>_exceptions.csv`. The `--html` option writes a
self-contained report with charts.

## 5. Web UI (interactive)

```bash
locust -f tests/load/locust/locustfile.py --host https://api.scalebotai.local
```

Open <http://localhost:8089>, enter the number of users and the ramp-up, then start. The UI
shows live RPS, percentiles and failures, and lets you download the CSV and HTML reports.

## 6. Load profiles (`LOCUST_SHAPE`)

The phases come from the load-testing plan (`docs/11-load-testing-strategy.md`). Each profile is a stage table of
`(end_time_seconds, users, spawn_rate)`. When a profile is active, `-u`, `-r` and `-t` are
ignored and the test stops by itself after the last stage.

| `LOCUST_SHAPE` | Stages | Duration | Peak users | ≈ Peak req/s |
|---|---|---|---:|---:|
| `smoke` | 100 users at 10/s | 2 min | 100 | 50 |
| `baseline` | 1,000 users at 10/s, then hold | 10 min | 1,000 | 500 |
| `load` | ramp to 10,000 at 17/s (~10 min), hold | 30 min | 10,000 | 5,000 |
| `step` / `stress` | +2,500 users every 3 min (50/s) up to 25,000 at 30 min, hold 5 min | 35 min | 25,000 | 12,500 |
| `spike` | 1,000 for 5 min → jump to 50,000 at 1,000/s (~50 s surge + ~2 min at peak) → back to 1,000 for 5 min | 13 min | 50,000 | 25,000 |
| `soak` | 500 users at 5/s, hold | 24 h | 500 | 250 |

`stress` and `spike` push far past the 500 req/s design headroom on purpose. They measure
how the platform degrades and recovers:
- Does the HPA scale to 20 pods?
- Does the error rate stay bounded?
- Does latency return to baseline afterwards?

An SLO exit code of 1 is expected for those two profiles.

Bash or macOS:

```bash
LOCUST_SHAPE=step locust -f tests/load/locust/locustfile.py --headless \
  --host https://api.scalebotai.local \
  --csv tests/load/locust/results/stress --html tests/load/locust/results/stress.html
```

Windows PowerShell:

```powershell
$env:LOCUST_SHAPE = "step"
locust -f tests/load/locust/locustfile.py --headless --host https://api.scalebotai.local `
  --csv tests/load/locust/results/stress --html tests/load/locust/results/stress.html
Remove-Item Env:LOCUST_SHAPE      # back to plain -u/-r/-t runs
```

Windows cmd: `set LOCUST_SHAPE=step` and then the same `locust ...` command.

## 7. Distributed mode (master + workers)

One Locust worker process runs on gevent, so it is single-threaded and saturates **one CPU
core**. In practice one worker handles **a few thousand users** of this script, at about
0.5 req/s per user. Run **one worker per CPU core**, keep generator CPU below ~80 %, and never
run generators on the same machines as the system under test. The master only coordinates
and aggregates. Set `LOCUST_SHAPE` on the master.

| Phase | Users | Workers (≈2,000 users each) |
|---|---:|---|
| smoke / soak | 100 / 500 | 1 (standalone, no master needed) |
| baseline | 1,000 | 1–2 |
| load | 10,000 | 5–8 |
| stress | 25,000 | 12–20, spread over several machines |
| spike | 50,000 | 25–40, spread over several machines |

On generator hosts:
- Raise the open file limit (`ulimit -n 65535`). Every user keeps its own keep-alive
  connection.
- Watch for ephemeral-port exhaustion.

If the workers are the bottleneck, `FastHttpUser` (geventhttpclient) is several times cheaper
per request. Switching to it needs small changes: TLS verification is set by the class
attribute `insecure`.

### 7.1 Plain CLI

```bash
# Master (web UI on :8089; add --headless -u/-r/-t or LOCUST_SHAPE for unattended runs)
locust -f tests/load/locust/locustfile.py --master --host https://api.scalebotai.local

# Headless master that waits for 8 workers before starting
LOCUST_SHAPE=load locust -f tests/load/locust/locustfile.py --master --headless \
  --expect-workers 8 --host https://api.scalebotai.local \
  --csv tests/load/locust/results/load --html tests/load/locust/results/load.html

# On each worker machine (same locustfile and Locust version), one process per core
locust -f tests/load/locust/locustfile.py --worker --master-host <master-ip>
```

On Linux or macOS, `locust ... --processes -1` forks one worker per core on a single machine.
That option is not available on Windows.

### 7.2 Docker Compose (master + scaled workers)

Save the file below as `tests/load/locust/locust-compose.yml`. It is a local file, not
committed. Run it from that folder:

```yaml
# locust-compose.yml: distributed Locust against ScaleBotAI
services:
  master:
    image: locustio/locust:latest      # pin the version you use locally (locust -V)
    ports:
      - "8089:8089"                    # web UI
    volumes:
      - ./:/mnt/locust
    environment:
      LOCUST_SHAPE: ${LOCUST_SHAPE:-none}
      LOCUST_INSECURE: "true"
    command: -f /mnt/locust/locustfile.py --master --host https://api.scalebotai.local
    # Headless alternative (results land in ./results, which must exist and be writable):
    # command: >
    #   -f /mnt/locust/locustfile.py --master --headless --expect-workers 8
    #   -u 10000 -r 20 -t 30m --host https://api.scalebotai.local
    #   --csv /mnt/locust/results/load --html /mnt/locust/results/load.html
    extra_hosts:
      - "api.scalebotai.local:host-gateway"   # target = ScaleBotAI stack on this Docker host

  worker:
    image: locustio/locust:latest
    volumes:
      - ./:/mnt/locust
    environment:
      LOCUST_INSECURE: "true"
    command: -f /mnt/locust/locustfile.py --worker --master-host master
    depends_on:
      - master
    extra_hosts:
      - "api.scalebotai.local:host-gateway"
```

```bash
cd tests/load/locust
docker compose -f locust-compose.yml up --scale worker=8
# Bash: LOCUST_SHAPE=spike docker compose -f locust-compose.yml up --scale worker=24
# PowerShell: $env:LOCUST_SHAPE="spike"; docker compose -f locust-compose.yml up --scale worker=24
docker compose -f locust-compose.yml down
```

For a Kubernetes cluster, replace `extra_hosts` with the ingress IP, or run the same
master/worker pair as Deployments in a separate namespace.

## 8. Reading the results

- **Statistics** (`*_stats.csv` / HTML): per endpoint, the request count, failures, average,
  median and p95/p99, and RPS. Compare `Aggregated` against the success criteria: average < 300 ms
  and failures < 1 %. Also look at p95/p99. A healthy average can hide a slow tail.
- **Failures** (`*_failures.csv`):
  - `rate limited ...` means the test environment's rate limits were not raised.
  - `401` points to a token problem, such as differing `JWT_SECRET_KEY` values across pods.
  - `connection error` or HTTP 502/503/504 points to saturation at the ingress or pods.
- **Charts** (HTML / web UI): RPS, response times and user count over time. A latency knee
  while users are still rising marks the saturation point. Correlate it with HPA events
  (`kubectl -n scalebotai get hpa -w`) and Grafana CPU and memory.

## 9. Clean-up

Generated accounts are real rows in the test database: username prefix `lt_`, email domain
`@scalebotai.dev`. To remove them with their conversations, chats and feedback (via
`ON DELETE CASCADE`) in a **test** database:

```sql
DELETE FROM users WHERE username LIKE 'lt\_%' AND email LIKE '%@scalebotai.dev';
```
