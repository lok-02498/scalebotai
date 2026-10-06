# JMeter load test: ScaleBotAI

| File | Purpose |
|---|---|
| [`scalebotai-load-test.jmx`](scalebotai-load-test.jmx) | JMeter 5.6 test plan (no plugins required) |
| [`questions.csv`](questions.csv) | 30 DevOps/cloud questions. Header `question`, delimiter `\|`, read by the CSV Data Set Config |

## What the plan does

```text
Test Plan  (User Defined Variables <- -J properties)
├── HTTP Request Defaults     ${PROTOCOL}://${HOST}:${PORT}, HttpClient4, timeouts 5 s connect / 30 s response
├── HTTP Header Manager       Content-Type + Accept: application/json
├── Thread Group              ${USERS} threads, ${RAMP_UP} s ramp-up, runs for ${DURATION} s, loops forever
│   ├── Once Only Controller  (first iteration of each thread)
│   │   ├── POST /api/v1/auth/register   JSR223 PreProcessor sets USERNAME=jm_<16 hex>; assert 201
│   │   └── POST /api/v1/auth/login      JSON Extractor access_token=$.access_token; assert 200
│   └── Loop Controller "Chat Loop"      (${__P(LOOPS,-1)} = until the scheduler stops the thread)
│       ├── CSV Data Set Config          QUESTION <- questions.csv (next line every loop iteration)
│       ├── HTTP Header Manager          Authorization: Bearer ${access_token}
│       ├── If Controller                re-login when the token is older than TOKEN_REFRESH_MS
│       ├── POST /api/v1/chat            {"message":"${QUESTION}"}; think time; assert 200 + $.response
│       └── GET /api/v1/chat/history     ?page=1&size=20; think time; assert 200
├── Summary Report    (disabled: GUI only)
└── Aggregate Report  (disabled: GUI only)
```

- **Generated users:** username `jm_<16 hex>`, email `<username>@scalebotai.dev`, password
  `${PASSWORD}`. Reserved TLDs such as `.local` and `.test` are rejected by the API's email
  validation, so they are not used for emails.
- **Think time:** each request is preceded by a Uniform Random Timer of `THINK_MIN_MS` plus
  0 to `THINK_RANGE_MS`, which averages 2 s. So one thread sends about **0.5 req/s**, and
  1,000 threads ≈ 500 req/s, the platform's design headroom.
- **Token expiry:** access tokens expire after 30 min. The If Controller logs in again after
  25 min (`TOKEN_REFRESH_MS`), so the 24 h soak run does not turn into a wall of 401s.

## 1. Install

JMeter 5.6.3 runs on Java 8 or newer. Use a current LTS JDK (17 or 21).

**Windows**

```powershell
winget install EclipseAdoptium.Temurin.17.JDK      # or any JDK 17+
choco install jmeter                                # Chocolatey package
# or download apache-jmeter-5.6.3.zip from https://jmeter.apache.org/download_jmeter.cgi,
# unzip to C:\tools\apache-jmeter-5.6.3 and add C:\tools\apache-jmeter-5.6.3\bin to PATH
jmeter -v
```

**Linux / macOS**

```bash
# Debian/Ubuntu JDK: sudo apt install openjdk-17-jre-headless ; macOS: brew install jmeter
curl -LO https://archive.apache.org/dist/jmeter/binaries/apache-jmeter-5.6.3.tgz
tar xzf apache-jmeter-5.6.3.tgz && export PATH="$PWD/apache-jmeter-5.6.3/bin:$PATH"
jmeter -v
```

`api.scalebotai.local` must resolve on the load generator. Add a hosts-file entry pointing
to `127.0.0.1` for docker compose, or to the ingress IP for Kubernetes.

## 2. Prepare the target environment

- **Rate limits.** The API allows `RATE_LIMIT_PER_MINUTE=120` per user, or per client IP for
  anonymous routes like register and login. Every thread registers and logs in from a few
  generator IPs, so the limit is hit within seconds and the results fill with 429s.
  - For the test environment, set `RATE_LIMIT_PER_MINUTE=100000`, or `RATE_LIMIT_ENABLED=false`.
  - Raise or allow-list the NGINX/ingress `limit_req` zone for the generator IPs as well.
  - Never change these in production.
- **Self-signed TLS.** No action is needed. JMeter's HttpClient4 sampler trusts every
  certificate by default (it does not validate the chain), so NGINX's self-signed certificate
  works as is.
- **bcrypt.** Each new thread triggers two bcrypt hashes on the server: one for register, one
  for login. A short `RAMP_UP` with many `USERS` is therefore also an authentication storm.
  Ramp up gradually.

## 3. Edit in the GUI, run without it

Only use the GUI to view or edit the plan:

```bash
jmeter -t tests/load/jmeter/scalebotai-load-test.jmx
```

Always run real load in **non-GUI mode**. The GUI listeners are disabled in the plan, so
they cost nothing. Results go to the `-l` file and the HTML dashboard:

```bash
jmeter -n -t tests/load/jmeter/scalebotai-load-test.jmx \
  -JHOST=api.scalebotai.local -JPORT=443 -JPROTOCOL=https \
  -JUSERS=1000 -JRAMP_UP=120 -JDURATION=600 \
  -l tests/load/jmeter/results/results.jtl \
  -e -o tests/load/jmeter/results/report/
```

PowerShell uses a backtick for line continuation:

```powershell
jmeter -n -t tests/load/jmeter/scalebotai-load-test.jmx `
  -JHOST=api.scalebotai.local -JPORT=443 -JPROTOCOL=https `
  -JUSERS=1000 -JRAMP_UP=120 -JDURATION=600 `
  -l tests/load/jmeter/results/results.jtl `
  -e -o tests/load/jmeter/results/report/
```

- **Output folder.** The `-o` report folder must be **empty or not exist**, or JMeter
  refuses to start. Use a new folder per run, or add `-f` to delete the old `.jtl` and report
  first. JMeter creates the folders itself.
- **Rebuild a report from an existing results file:**
  `jmeter -g tests/load/jmeter/results/results.jtl -o tests/load/jmeter/results/report2/`.
- **Console progress.** JMeter prints `summary +` lines every 30 s with throughput, average,
  min/max and error %.

### Properties (`-JNAME=value`)

| Property | Default | Meaning |
|---|---|---|
| `HOST` | `api.scalebotai.local` | Target host name |
| `PORT` | `443` | Target port |
| `PROTOCOL` | `https` | `http` or `https` |
| `USERS` | `100` | Threads (per load generator) |
| `RAMP_UP` | `60` | Seconds to start all threads |
| `DURATION` | `600` | Test length in seconds, including ramp-up |
| `START_DELAY` | `0` | Seconds to wait before starting threads |
| `THINK_MIN_MS` | `1000` | Minimum think time before each request |
| `THINK_RANGE_MS` | `2000` | Extra random think time (0..range) |
| `PASSWORD` | `LoadTest123!` | Test-only password for generated users |
| `EMAIL_DOMAIN` | `scalebotai.dev` | Email domain for generated users |
| `TOKEN_REFRESH_MS` | `1500000` | Re-login when the token is older than this (25 min) |
| `LOOPS` | `-1` | Chat-loop iterations per thread (`-1` = until `DURATION` ends) |

### The six test phases

The phases come from the load-testing plan (`docs/11-load-testing-strategy.md`). Peak req/s assumes about 0.5 req/s per thread.

| Phase | `-JUSERS` | `-JRAMP_UP` | `-JDURATION` | ≈ Peak req/s | Notes |
|---|---:|---:|---:|---:|---|
| Smoke | 100 | 30 | 120 | 50 | Does everything work? |
| Baseline | 1000 | 120 | 600 | 500 | Reference numbers for the SLOs |
| Load | 10000 | 600 | 1800 | 5,000 | Ramp to 10,000 over 10 min, hold to 30 min |
| Stress | 25000 | 1800 | 2100 | 12,500 | Linear ramp approximates the step profile; find the breaking point |
| Spike | 50000 | 60 | 300 | 25,000 | See the two-process recipe below |
| Soak | 500 | 120 | 86400 | 250 | 24 h. Watch memory, DB pool and Redis for slow leaks |

Example for the soak test:

```bash
jmeter -n -t tests/load/jmeter/scalebotai-load-test.jmx -JUSERS=500 -JRAMP_UP=120 -JDURATION=86400 \
  -l tests/load/jmeter/results/soak.jtl -e -o tests/load/jmeter/results/soak-report/
```

**Spike (1,000 → 50,000 → 1,000) with stock JMeter.** The standard Thread Group only ramps
linearly, so run two processes. Ideally run the spike process from other generator machines.

```bash
# Process 1: steady 1,000-user baseline for 13 minutes
jmeter -n -t tests/load/jmeter/scalebotai-load-test.jmx -JUSERS=1000 -JRAMP_UP=60 -JDURATION=780 \
  -l tests/load/jmeter/results/spike-base.jtl
# Process 2: +49,000 threads after 5 minutes, ramp in 60 s, gone after 3 minutes
jmeter -n -t tests/load/jmeter/scalebotai-load-test.jmx -JUSERS=49000 -JRAMP_UP=60 -JDURATION=180 \
  -JSTART_DELAY=300 -l tests/load/jmeter/results/spike-burst.jtl
```

To report on both runs together, merge the two `.jtl` files (same CSV header, so keep the
header line once), then run `jmeter -g merged.jtl -o report/`. If you prefer true step and
spike shapes inside a single plan, install the *Custom Thread Groups* plugin and swap in a
Concurrency Thread Group or Ultimate Thread Group. Locust's `LOCUST_SHAPE=step|spike`
profiles also do this.

`stress` and `spike` are meant to exceed the 500 req/s design headroom. Judge them on graceful
degradation (HPA to 20 pods, bounded 429/503s, recovery afterwards), not on the < 300 ms
SLO.

## 4. JVM heap

The default heap is 1 GB, which is too small for thousands of threads. Set `HEAP` before
starting JMeter. Each thread needs roughly 1–2 MB, plus result buffers.

```bash
HEAP="-Xms2g -Xmx8g -XX:MaxMetaspaceSize=256m" jmeter -n -t ...      # Linux/macOS
```

```bat
rem Windows cmd
set HEAP=-Xms2g -Xmx8g -XX:MaxMetaspaceSize=256m
```

```powershell
$env:HEAP = "-Xms2g -Xmx8g -XX:MaxMetaspaceSize=256m"                # Windows PowerShell
```

To keep generators efficient:
- Keep the default CSV `.jtl` format.
- Never save response data on success.
- Leave the GUI listeners disabled.

## 5. Distributed mode (many load generators)

One JMeter engine realistically drives **about 1,000–2,000 threads** of this plan. With
HTTPS and JSON assertions the generator CPU, not the target, quickly becomes the limit.
So 10,000 threads need about 5–10 engines, and 50,000 need 25–50. Keep generator CPU below
~80 % and do not co-locate generators with the system under test.

1. **On every engine** install the same JMeter and Java versions, and copy
   `questions.csv`. On remote engines a relative CSV path is resolved against the directory
   the JMeter server was started from, so put it there or use an absolute path.
2. **Lab-only RMI setup.** Add `server.rmi.ssl.disable=true` to `bin/user.properties` on the
   controller **and** every engine. For anything beyond a lab, create the RMI keystore with
   `bin/create-rmi-keystore.sh` instead. Open TCP 1099 plus a fixed `server.rmi.localport`
   in the firewall.
3. Start the engines: `jmeter-server` (Linux) or `jmeter-server.bat` (Windows).
4. From the controller, pass the properties with **`-G`** (global, sent to the engines)
   instead of `-J`. `-J` only sets them on the controller.

```bash
jmeter -n -t tests/load/jmeter/scalebotai-load-test.jmx -R gen1,gen2,gen3,gen4,gen5 \
  -GHOST=api.scalebotai.local -GUSERS=2000 -GRAMP_UP=600 -GDURATION=1800 \
  -l tests/load/jmeter/results/load.jtl -e -o tests/load/jmeter/results/load-report/
```

`USERS` is **per engine**: in the example above the total is 5 × 2,000 = 10,000 threads.
The controller collects all samples, so it needs network bandwidth and heap too.

## 6. Reading the HTML dashboard

Open `results/report/index.html`.

- **APDEX** (Application Performance Index, from 0 to 1): the share of satisfied requests
  plus half of the tolerated ones. The JMeter defaults are satisfied ≤ 500 ms and tolerated
  ≤ 1,500 ms. To align with the < 300 ms target, generate the report with
  `-Jjmeter.reportgenerator.apdex_satisfied_threshold=300 -Jjmeter.reportgenerator.apdex_tolerated_threshold=1200`.
  Aim for APDEX ≥ 0.94 per request.
- **Statistics table** (per sampler and in total):
  - Compare `Average` against the < 300 ms target, and `Error %` against < 1 %.
  - Use the `90th/95th/99th pct` columns to see tail latency hidden by the average.
  - `Throughput` is in requests/s. Compare it with the 116 req/s peak and 500 req/s headroom targets.
- **Errors / Top 5 errors by sampler:**
  - `429` means the rate limits were not raised for the test.
  - `401` points to a token problem, such as differing `JWT_SECRET_KEY` values across pods.
  - `502/503/504` or `Non HTTP response code` means saturation or connection errors at the ingress or pods.
- **Charts:**
  - *Response Times Over Time* together with *Active Threads Over Time* shows the latency knee, which is the saturation point.
  - *Transactions Per Second* shows whether throughput plateaus while threads keep rising.
  - *Response Time Percentiles* shows the shape of the tail.
- **Correlate with the platform:** Grafana (latency, error rate, CPU and memory below 80 %,
  HPA replica count, DB pool, Redis hit ratio), `kubectl -n scalebotai get hpa -w`, and
  Kibana `scalebot-error-logs-*`.

## 7. Clean-up

Generated accounts stay in the test database. To remove them, including their
conversations, chats and feedback (`ON DELETE CASCADE`), in a **test** database only:

```sql
DELETE FROM users WHERE username LIKE 'jm\_%' AND email LIKE '%@scalebotai.dev';
```
