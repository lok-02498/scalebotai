# 15 — Viva Questions and Answers

> 81 questions with model answers grounded in ScaleBotAI's actual design: names, numbers and files from the
> repository. Answers are written to be *spoken* in 30–90 seconds. Read the "Key numbers" box first; a
> quick-revision cheat sheet closes the document.

## Key numbers to remember

| Item | Value |
|---|---|
| Load target | 1,000,000 req/day ≈ **11.6 req/s** average, **~116 req/s** peak (10×), headroom **500 req/s** |
| Latency / errors / availability | avg **< 300 ms**, errors **< 1 %**, **99.9 %** (≈ 43 min downtime per 30-day month) |
| Deployment | `scalebotai-api`, container `api`, port `http` 8000, **3 replicas**, `maxSurge 25%`, `maxUnavailable 0`, `minReadySeconds 10` |
| Resources | requests **250m / 256Mi**, limits **1 CPU / 512Mi** (QoS Burstable) |
| HPA | **3 → 20** pods, CPU **70 %** (=175m), memory **75 %** (=192Mi); scale-up 100 % or 4 pods per 15 s; scale-down 10 %/min after a 300 s window |
| PDB | `minAvailable: 2` |
| Probes | startup + liveness `GET /health/live`; readiness `GET /health/ready` |
| Tokens | access **30 min**, refresh **7 days**, HS256, `jti` denylist in Redis |
| Rate limit | **120 req/min** per user (or per IP for register/login/refresh), fixed window in Redis |
| Cache TTLs | bot answers **3600 s**, user profile **300 s**, analytics **30 s** |
| DB pool | per pod: **5 + 3** overflow in Kubernetes (ConfigMap), 10 + 20 in Compose (`.env`); timeout 10 s; PostgreSQL `max_connections` = **300** (K8s) / 200 (Compose) |
| bcrypt | cost **12** (≈ 250 ms CPU per hash), run in a threadpool |
| Image tag | `<BUILD_NUMBER>-<git short sha>` (+ `latest` on main), user **10001** |

---

## A. Architecture discussion

**1. Explain the overall architecture and the life of one request.**
A client calls `https://api.scalebotai.local`. In Kubernetes, ingress-nginx terminates TLS, applies per-IP
rate limiting and routes to the ClusterIP Service `scalebotai-api` (port 80 → named port `http`, 8000), which
load-balances to one of 3–20 ready pods; in Docker Compose a custom NGINX container plays the same role. Inside
the pod a single uvicorn worker runs FastAPI: the outermost `RequestContextMiddleware` assigns an
`X-Request-ID`, starts a timer and tracks Prometheus metrics; the router then resolves dependencies — JWT
decoding, a Redis denylist check, the user profile (Redis cache, else PostgreSQL) and the per-user rate limit.
The service layer calls the bot engine (rule-based or Ollama) through a Redis response cache and writes the
exchange to PostgreSQL in one transaction. The response carries `X-Request-ID` and `X-Process-Time-ms`; a JSON
access-log line goes to stdout (Filebeat → Logstash → Elasticsearch) and Prometheus later scrapes the metrics.
No state lives in the pod, so any pod can serve any request.

**2. Why is the API stateless, and why does that matter?**
All durable state is in PostgreSQL (users, conversations, chats, feedback) and all shared ephemeral state is in
Redis (cache, rate-limit windows, token denylist, counters); authentication is a self-contained JWT. Because
nothing lives in process memory between requests, pods are interchangeable: the HPA can add or remove them at
any time, a crashed pod loses nothing, a rolling update can replace them one by one, and the load balancer needs
no sticky sessions. Statelessness is the precondition for horizontal scaling and zero-downtime deployment; it is
also why the in-process caches you might add naively (e.g. a Python dict) were deliberately avoided.

**3. Why a modular monolith instead of microservices?**
ScaleBotAI is one deployable FastAPI application with clear internal layers — `api/routes`, `services`, `db`,
`chatbot`, `core` — rather than separate auth, chat and admin services. At this scale microservices would add
network hops, distributed transactions (e.g. account deletion across services), service discovery, versioned
inter-service contracts and one pipeline per service, with no benefit: the load is uniform and scales
horizontally as a whole. The seams for later extraction already exist — the chatbot sits behind the `BotEngine`
interface (Strategy pattern), and LLM inference already runs out of process in Ollama.

**4. Why FastAPI rather than Flask or Django?**
The workload is I/O-bound (PostgreSQL, Redis, HTTP to Ollama), so an ASGI framework with native async lets
one process keep hundreds of requests in flight while they wait on I/O; Flask's WSGI model ties up a thread per
request. FastAPI also gives Pydantic v2 validation with structured 422 errors, response models that cannot leak
fields such as `password_hash`, dependency injection for auth/DB/rate limiting, and an automatically generated
OpenAPI spec with Swagger UI at `/docs` and ReDoc at `/redoc`. Django would bring an admin site and migrations,
but it is heavier and its ORM is still largely synchronous. The trade-off is discipline: any blocking call in
an `async` handler stalls the event loop (see Q40). Details are in ADR-001 (`docs/13-design-decisions-and-future.md`).

**5. Why PostgreSQL and not MongoDB?**
The data is relational — users own conversations, conversations contain exchanges, an exchange has at most one
rating — and the system relies on database-enforced invariants: unique usernames/emails, one rating per answer
(`uq_feedback_chat_id`), and `ON DELETE CASCADE` so deleting an account erases everything atomically. Admin
analytics need aggregates (`count(*) FILTER (WHERE …)`, `avg`, `GROUP BY`). MongoDB would model a conversation as
a document with an unbounded message array (16 MB limit) and push uniqueness and cascades into application code.
PostgreSQL also offers partial/covering/BRIN indexes, partitioning, and `pgvector` for future RAG.

**6. What is Redis used for, and what happens if it fails?**
Six key families, all with TTLs: `bot:resp:{mode}:{sha256}` (answer cache, 3600 s), `user:{id}` (profile cache,
300 s), `analytics:summary` (admin dashboard, 30 s), `ratelimit:{identity}:{epoch_minute}` (fixed-window
limiter, 60 s), `jwt:denylist:{jti}` (revoked tokens until expiry) and `stats:requests:{YYYY-MM-DD}` (daily
counters, 35 days). If Redis fails, the app fails **open**: every helper in `src/app/services/cache.py` catches
the error, logs it (throttled to once per 10 s) and continues as if the key were missing. The API stays up but
loses caching, application-level rate limiting and token revocation until Redis returns; readiness deliberately
gates only on the database so pods are not pulled from the Service.

**7. How does the chatbot engine work, and what happens if the LLM is slow?**
`CHATBOT_MODE` selects an implementation of the `BotEngine` interface at startup (`src/app/chatbot/factory.py`).
The default `rule` engine normalises the message and scores ~34 intents (greetings, help and DevOps topics such
as Docker, Kubernetes, Jenkins, Redis, Prometheus) by keyword/regex matches; the answer is chosen
deterministically from the message hash, so it can be cached. In `llm` mode the `OllamaBot` sends the system
prompt plus the last 5 exchanges to Ollama's `/api/chat` with model `tinyllama`; any timeout
(`OLLAMA_TIMEOUT_SECONDS=20`), HTTP error or empty answer falls back to the rule engine and the exchange is stored
with `bot_mode = "rule-fallback"`. After 3 consecutive failures a circuit breaker skips Ollama for 30 s so users
stop waiting on a dead model server.

**8. How did you design for 99.9 % availability?**
99.9 % allows about 43 minutes of downtime per 30-day month, so no single pod failure, deploy or node drain may
cause an outage. The Deployment runs at least 3 replicas spread across nodes (topology spread constraints and
preferred pod anti-affinity), a PodDisruptionBudget keeps at least 2 available during voluntary disruptions, and
rolling updates use `maxUnavailable: 0` with readiness gating and automatic rollback. NGINX retries another
upstream on connection errors/502/503, Redis failures degrade instead of failing, and the HPA adds capacity before
overload. The honest gap is the data tier: PostgreSQL and Redis are single instances, so a database node failure
costs minutes — the documented next step is CloudNativePG or a managed database with failover.

**9. Why `/api/v1` versioning and a single error format?**
URL versioning makes breaking changes explicit: a future `/api/v2` (for example cursor pagination) can run side
by side while clients migrate. Operational endpoints (`/health*`, `/metrics`) stay unversioned at the root
because probes and Prometheus depend on fixed paths. Every error — validation, auth, not-found, rate limiting,
even NGINX-generated 429/503 — has the shape `{"error": {"code", "message", "details", "request_id"}}`, so
clients branch on a stable `code` instead of parsing messages, and the `request_id` lets support find the exact
log line in Kibana.

---

## B. Docker

**10. What is a container technically, and how does it differ from a VM?**
A container is an ordinary Linux process isolated with kernel **namespaces** (PID, network, mount, UTS, IPC,
user) and constrained by **cgroups** (CPU, memory, PIDs), running on a layered union filesystem (overlay2) built
from the image. It shares the host kernel, so it starts in milliseconds and costs megabytes. A VM virtualises
hardware through a hypervisor and boots its own guest kernel — stronger isolation, but seconds to start and
gigabytes of overhead. This is why ScaleBotAI can run a dozen services in Compose on a laptop and why a
container's security depends on not running as root and dropping capabilities.

**11. Why multi-stage builds? Walk through your Dockerfile.**
`docker/api/Dockerfile` has four stages. `builder` installs `build-essential`, creates a virtualenv in
`/opt/venv` and installs `requirements.txt` with a BuildKit pip cache mount. `test` adds dev dependencies, copies
`src/` and `tests/`, and **runs pytest during the build**, so a failing test can never produce an image;
`test-reports` is a `scratch` stage that only exports the JUnit XML. `runtime` starts again from
`python:3.12-slim`, copies only `/opt/venv` and `src/app`, and runs as user 10001. Compilers, pip caches and test
code never reach the shipped image, which makes it smaller, faster to pull and gives Trivy fewer packages to flag.
Builder and runtime must share the same base image because the venv stores absolute interpreter paths.

**12. CMD vs ENTRYPOINT — and exec vs shell form?**
`ENTRYPOINT` defines the executable; `CMD` supplies default arguments (or the whole command if there is no
entrypoint), and `docker run image <args>` replaces `CMD` while `--entrypoint` is needed to replace
`ENTRYPOINT`. The API image uses only an exec-form `CMD ["uvicorn", "app.main:app", ..., "--workers", "1"]`,
which keeps debugging easy (`docker run -it image bash`). The NGINX image uses the official entrypoint pattern:
`/docker-entrypoint.sh` runs our `10-generate-self-signed-cert.sh` from `/docker-entrypoint.d/` and then execs
the `CMD`. Exec form matters because shell form (`CMD uvicorn ...`) runs under `/bin/sh -c`, making the shell PID 1;
it does not forward SIGTERM, so `docker stop` waits 10 s and SIGKILLs — no graceful shutdown.

**13. COPY vs ADD?**
`COPY` copies files from the build context (or another stage with `--from`) — nothing else. `ADD` additionally
downloads remote URLs and auto-extracts local tar archives, which is implicit behaviour that can surprise you
(an archive gets unpacked when you wanted the file) and pulls unverified content into a layer. Best practice is
`COPY` everywhere and `RUN curl … && verify checksum` when a download is really needed. ScaleBotAI only uses
`COPY` — including `COPY --from=builder /opt/venv /opt/venv` and `COPY --chmod=0755` in the NGINX image.

**14. How do image layers and the build cache work, and how did you order the Dockerfile?**
Each filesystem-changing instruction creates a read-only layer; the cache key is the instruction plus its parent
layer plus, for `COPY`, the checksums of the copied files. The first instruction whose key changes invalidates
every later layer. So the Dockerfile installs system packages first, then copies **only** `requirements.txt` and
runs `pip install`, and copies `src/app` last: editing Python code reuses the expensive dependency layer and
rebuilds in seconds. Build-specific labels (`VCS_REF`, `BUILD_DATE`) are declared near the end for the same reason.
Deleting a file in a later layer does not shrink the image (the earlier layer still contains it), which is
another reason to keep build tools in a separate stage; `.dockerignore` keeps `.git`, venvs and secrets out of
the build context.

**15. Why run as a non-root user with a fixed UID 10001? What other hardening is there?**
If the application is compromised, the attacker gets an unprivileged user rather than root inside the
container — and a high, fixed UID does not map to any real user on the host. The numeric `USER 10001:10001` lets
Kubernetes verify `runAsNonRoot: true` without reading `/etc/passwd`. The application code is owned by root and
read-only to `appuser`, so a compromised process cannot rewrite itself. In Kubernetes the pod adds
`readOnlyRootFilesystem: true` (with an `emptyDir` for `/tmp`), `allowPrivilegeEscalation: false`, dropped
capabilities (`ALL`), a `RuntimeDefault` seccomp profile and `automountServiceAccountToken: false`.

**16. Bridge vs host networking — and how are your Compose networks laid out?**
The default `bridge` network connects containers through a virtual bridge with NAT but offers no name
resolution; **user-defined bridge** networks add Docker's embedded DNS (127.0.0.11) so containers reach each
other by service name, plus isolation between networks. `host` networking puts the container in the host's
network namespace — no NAT overhead, but no isolation, port conflicts, and scaled replicas could not share port
8000. ScaleBotAI uses three user-defined bridges: `frontend` (nginx ↔ chatbot-api), `backend` (chatbot-api ↔
postgres/redis/ollama and exporters) and `monitoring` (Prometheus, Grafana, ELK, exporters, chatbot-api for
scraping). NGINX is not on `backend`, so even a compromised proxy cannot reach the database, and only NGINX
publishes ports 80/443.

**17. Volumes vs bind mounts — which did you use where?**
Named **volumes** are managed by Docker, survive `docker compose down` (not `down -v`), and are the right choice
for data: `postgres-data`, `redis-data`, `es-data`, `prometheus-data`, `grafana-data`, `ollama-data`, and
`app-logs`, which is shared — `chatbot-api` writes `/var/log/scalebotai/app.log` and Logstash reads it. When an
empty named volume is first mounted, Docker copies the image's directory contents and ownership into it, which is
why the Dockerfile pre-creates `/var/log/scalebotai` owned by `appuser`. **Bind mounts** map a host path and are
used for configuration we edit on the host — `monitoring/prometheus/prometheus.yml`, Grafana provisioning,
Logstash pipeline — but they depend on host paths and permissions (a common pitfall on Windows).

**18. How does `docker compose up --scale chatbot-api=3` work?**
Compose creates `chatbot-api-1..3`; each replica registers under the service name in Docker DNS, which returns
all their IPs. That only works because `chatbot-api` has **no `container_name`** (names must be unique) and **no
published host port** (three containers cannot all bind host port 8000). NGINX load-balances via
`server chatbot-api:8000 resolve` with `resolver 127.0.0.11 valid=10s`, so new replicas get traffic within about
10 seconds, and Prometheus uses `dns_sd_configs` (type A) to scrape every replica individually. With the default
pool settings, three replicas can open up to 3 × 30 = 90 database connections — well within the limit.

**19. What does the Dockerfile `HEALTHCHECK` do, and does Kubernetes use it?**
It runs a tiny Python stdlib request against `http://127.0.0.1:8000/health/live` every 30 s (timeout 5 s,
start period 20 s, 3 retries) and marks the container healthy or unhealthy; Compose uses that for
`depends_on: condition: service_healthy` and `docker ps` shows it. Using Python's `urllib` avoids adding curl to
the image. Plain Docker does not restart an unhealthy container (restart policies react to exits), and
**Kubernetes ignores `HEALTHCHECK` entirely** — it uses its own startup, liveness and readiness probes defined in
`k8s/base/06-api-deployment.yaml`.

---

## C. Kubernetes

**20. Walk through the Kubernetes objects you created.**
`k8s/base/` holds numbered manifests applied with `kubectl apply -k k8s/base`: namespaces (`scalebotai`,
`monitoring`, `logging`); ConfigMap `scalebotai-config` and Secret `scalebotai-secrets`; storage (PVCs
`postgres-data`, `redis-data`); PostgreSQL StatefulSet + Service `postgres`; Redis Deployment (Recreate
strategy) + Service `redis`; the API Deployment `scalebotai-api`; Service `scalebotai-api` (ClusterIP 80 →
`http`); Ingress `scalebotai-ingress` (class `nginx`, host `api.scalebotai.local`, TLS secret `scalebotai-tls`);
HPA; PodDisruptionBudget; RBAC (ServiceAccount `scalebotai-api` without a token, `jenkins-deployer` with a
namespaced Role); and NetworkPolicies (default deny plus explicit allows).

**21. Liveness vs readiness vs startup probes — and why does liveness not check the database?**
A **startup** probe protects slow starts: until it succeeds, the other probes are disabled. Ours hits
`/health/live` every 5 s with 24 allowed failures (120 s), covering database retries and table creation. A failed
**liveness** probe restarts the container — ours (`/health/live`, every 10 s, 3 failures) only proves the process
and event loop respond. A failed **readiness** probe only removes the pod from Service endpoints, without a
restart — ours (`/health/ready`, every 5 s) returns 503 when PostgreSQL is unreachable. If liveness checked the
database, a database outage would make Kubernetes restart every API pod in a loop — a restart storm that fixes
nothing and slows recovery. Dependency problems belong in readiness; liveness detects only a hung process.

**22. Why `maxUnavailable: 0` and `maxSurge: 25%`? Walk through a rollout.**
With 3 replicas, `maxSurge: 25%` rounds **up** to 1 extra pod and `maxUnavailable: 0` rounds **down** to 0, so
Kubernetes creates one new pod (4 total), waits until it is Ready and has stayed Ready for `minReadySeconds: 10`,
then terminates one old pod, and repeats three times. Serving capacity never drops below 3 ready pods — that is
what makes the deploy zero-downtime. If the new version never becomes ready, the rollout simply stalls with the
old pods still serving; `progressDeadlineSeconds: 600` marks it failed, Jenkins' `rollout status --timeout=300s`
fails first and triggers the rollback. The cost is needing room for one extra pod and a slower rollout. Both
values cannot be 0 at once — the rollout could never make progress.

**23. What happens to in-flight requests when a pod is terminated?**
On deletion the pod is marked Terminating and removed from the Service's EndpointSlices, but ingress-nginx and
kube-proxy learn this asynchronously. In parallel the `preStop` hook runs `sleep 5`, so the pod keeps serving
requests that still arrive during that propagation window. Then uvicorn (PID 1 thanks to the exec-form `CMD`)
receives SIGTERM, stops accepting connections, finishes in-flight requests and runs the FastAPI lifespan shutdown
(closes the Ollama client, Redis and the DB pool). If anything is still running after
`terminationGracePeriodSeconds: 30`, the kubelet sends SIGKILL. Without the `preStop` delay, some requests would
hit a pod that had already stopped listening — a classic source of 502s during deploys.

**24. What happens when a node dies?**
The kubelet stops posting heartbeats; after the node-monitor grace period (≈ 40–50 s depending on version) the
node becomes `NotReady`/`Unknown`, its pods are marked not ready and removed from Service endpoints, so traffic
stops flowing to them quickly. The node gets the `node.kubernetes.io/unreachable` NoExecute taint; pods tolerate it
for 300 s by default, after which they are evicted and the ReplicaSet creates replacements on healthy nodes —
meanwhile the surviving API replicas (spread across nodes) carry the load and the HPA can add more. The
PostgreSQL StatefulSet is different: Kubernetes will not start a second `postgres-0` while the old one might still
be running (at-most-one semantics), so an operator must confirm the node is dead (force-delete the pod or apply
the `node.kubernetes.io/out-of-service` taint) before it moves — the single-instance database is our main
availability risk.

**25. How does the HPA work, and how does it avoid flapping?**
Every 15 s the controller reads pod CPU and memory from metrics-server and computes, per metric,
`desired = ceil(currentReplicas × currentUtilisation / target)`, takes the **largest** value and clamps it to
3–20. Utilisation is measured against **requests**, so the targets mean 175m CPU and 192 Mi memory per pod. To
avoid flapping it ignores changes within a 10 % tolerance, and `k8s/base/09-hpa.yaml` sets asymmetric
`behavior`: scale-up has no stabilisation window and may add 100 % or 4 pods per 15 s (whichever is more), while
scale-down uses the highest recommendation of the last **300 s** and removes at most 10 % of pods per minute. Fast
up, slow down: a brief lull never removes pods that a returning spike needs a minute later.

**26. Requests vs limits — what happens when a container exceeds them?**
**Requests** are what the scheduler reserves on a node and the 100 % reference for HPA utilisation; **limits** are
hard cgroup caps. Exceeding the CPU limit (1 core) only **throttles** the container — latency rises but nothing is
killed (visible as `container_cpu_cfs_throttled_periods_total`). Exceeding the memory limit (512 Mi) gets the
container **OOMKilled** (exit code 137) and restarted. Because requests < limits, the pods are in the
**Burstable** QoS class; under node memory pressure BestEffort pods are evicted first, then Burstable pods using
more than their requests. A 1-CPU limit suits one Python process, which can use roughly one core for Python code
because of the GIL.

**27. ConfigMap vs Secret?**
Both hold key–value configuration injected as environment variables (`envFrom` in our Deployment) or files.
`scalebotai-config` holds non-secret settings (`CHATBOT_MODE`, `RATE_LIMIT_PER_MINUTE`, pool sizes…);
`scalebotai-secrets` holds `POSTGRES_PASSWORD`, `REDIS_PASSWORD`, `JWT_SECRET_KEY`, `ADMIN_PASSWORD` and the like.
Secrets are only **base64-encoded**, not encrypted — their real protections are separate RBAC on `get/list
secrets`, tmpfs-backed volume mounts, hidden values in `kubectl describe`, and etcd encryption at rest when
enabled. The repository's `02-secret.yaml` contains only `change-me-…` placeholders; real values are created out of
band (or via Sealed Secrets/Vault). Environment variables are read at container start, so changing either object
requires `kubectl rollout restart`.

**28. StatefulSet vs Deployment — why is PostgreSQL a StatefulSet?**
A Deployment treats pods as interchangeable cattle: random names, parallel rollout, any pod can be replaced by any
other. A StatefulSet gives each replica a **stable identity** (`postgres-0`), stable DNS through a headless
Service, its own persistent volume that follows it on rescheduling, ordered startup/shutdown and at-most-one
semantics per identity — exactly what a database needs. The stateless API is a Deployment. Redis is a single-replica
Deployment with a PVC and the `Recreate` strategy, so the old pod releases the ReadWriteOnce volume before the new
one starts — acceptable because Redis only holds cache-like data.

**29. Explain Service types and Ingress in your setup.**
A **ClusterIP** Service gives a stable virtual IP and DNS name inside the cluster and load-balances to *ready*
endpoints; **NodePort** opens a port (30000–32767) on every node; **LoadBalancer** asks the cloud for an external
load balancer; a **headless** Service (no cluster IP) returns pod IPs directly for StatefulSets. Our API Service is
ClusterIP, port 80 → `targetPort: http`, so the container port can change without touching the Service. External
HTTP traffic enters through the **Ingress** `scalebotai-ingress`, which the ingress-nginx controller implements:
host `api.scalebotai.local`, TLS from secret `scalebotai-tls`, forced HTTPS redirect and `limit-rps` per client IP.
`/metrics` is never routed publicly; Prometheus scrapes pods directly inside the cluster.

**30. What is a PodDisruptionBudget, and why `minAvailable: 2`?**
A PDB limits **voluntary** disruptions — `kubectl drain`, cluster upgrades, cluster-autoscaler scale-down — by
making the Eviction API refuse any eviction that would leave fewer than 2 ready API pods. With 3 replicas a node
drain therefore proceeds one pod at a time, each replacement becoming ready before the next eviction. It does not
protect against involuntary failures such as a node crash; that is what replica spreading is for.
`unhealthyPodEvictionPolicy: AlwaysAllow` lets already-unhealthy pods be evicted, so a broken pod cannot block
node maintenance forever.

**31. How did you secure the workload inside the cluster?**
`12-network-policies.yaml` starts from **default-deny** in the namespace, then allows DNS egress to `kube-system`,
API ingress only from the `ingress-nginx` and `monitoring` namespaces, API egress only to PostgreSQL, Redis and
Ollama, and database/cache ingress only from the API — so a compromised pod cannot roam (this needs a
policy-enforcing CNI such as Calico or Cilium). The API's ServiceAccount has
`automountServiceAccountToken: false` because the app never calls the Kubernetes API. Jenkins uses the
`jenkins-deployer` ServiceAccount with a namespaced Role limited to what deployments need (deployments,
replicasets, services, configmaps, ingresses, pod read/logs). Pods run non-root with a read-only root filesystem,
no capabilities and the `RuntimeDefault` seccomp profile.

**32. Jenkins uses `kubectl set image`, but the manifests are applied with `kubectl apply -k`. Isn't mixing imperative and declarative a problem?**
Yes, it can cause drift: `kustomization.yaml` pins the image to a placeholder tag (`latest`), so re-running
`kubectl apply -k k8s/base` after a Jenkins deploy would roll the Deployment back to that tag. Similarly, `replicas:
3` in the manifest briefly overrides the HPA's current count on every apply. The pragmatic rule in this project is:
`apply -k` for infrastructure changes, `set image` for releases, and always pass the current tag when applying
(`kustomize edit set image scalebotai/scalebotai-api=<user>/scalebotai-api:<tag>`). The clean long-term fix is
GitOps — the pipeline commits the new tag to Git and Argo CD applies it, so Git is the single source of truth.

---

## D. Jenkins

**33. Explain your pipeline stages.**
`jenkins/Jenkinsfile` runs **Checkout** (SCM, short SHA for the tag) → **Build** (Python environment and
dependencies) → **Unit Test** (pytest on SQLite + fakeredis, JUnit report) → **Lint** (parallel `ruff check`,
`ruff format --check` and a Bandit SAST scan) → **Docker Build** (`docker build -f docker/api/Dockerfile .`, tag
`<BUILD_NUMBER>-<sha>`) → **Security Scan** (Trivy: image vulnerabilities, secrets, misconfigurations) →
**Docker Push** (to Docker Hub with `dockerhub-credentials`) → **Kubernetes Deploy** (`scripts/deploy.sh` with the
chosen `DEPLOY_STRATEGY`, i.e. `kubectl set image` + `rollout status --timeout=300s` for rolling, using
`kubeconfig-scalebotai`; `main` branch only) → **Smoke Test** (`scripts/smoke_test.sh <BASE_URL>`, with retries) →
**Rollback** on failure. Cheap, fast checks come first so most failures are reported within minutes, nothing is
pushed or deployed unless everything before it passed, and `disableConcurrentBuilds()` guarantees two deploys
never overlap.

**34. Declarative vs scripted pipeline — which did you use and why?**
A **declarative** pipeline uses a fixed structure — `pipeline { agent; parameters; environment; stages { stage {
steps } }; post { success/failure/always } }` — that Jenkins validates before running, renders well in Blue
Ocean/stage view and is easy for reviewers to read. A **scripted** pipeline is free-form Groovy inside
`node { }` — maximum flexibility (loops, complex conditionals) but easy to turn into unmaintainable code. ScaleBotAI
uses declarative syntax because the flow is linear and `post { failure { … } }` gives a clean hook for rollback;
the few places that need logic use a `script { }` block or delegate to shell scripts (`scripts/deploy.sh`,
`scripts/rollback.sh`) that can also be run by hand.

**35. How are credentials handled securely in Jenkins?**
Secrets live in the Jenkins credential store (encrypted on the controller), never in the repository: Docker Hub as
a username/password credential `dockerhub-credentials`, and the cluster config as a secret-file credential
`kubeconfig-scalebotai`. The pipeline binds them only for the steps that need them with `withCredentials`, e.g.
`printf '%s' "${DOCKERHUB_TOKEN}" | docker login --username "${DOCKERHUB_USERNAME}" --password-stdin` and
`withCredentials([file(credentialsId: 'kubeconfig-scalebotai', variable: 'KUBECONFIG')])`, which exposes a
temporary file only inside that block; Jenkins masks the values in console output. Shell commands use single-quoted Groovy strings so the secret is
expanded by the shell, not interpolated by Groovy (which Jenkins flags as insecure). The kubeconfig belongs to the
`jenkins-deployer` ServiceAccount, so even a leaked file can only manage resources in the `scalebotai` namespace.

**36. Controller vs agents — where does the build run?**
The Jenkins **controller** schedules jobs, stores configuration, credentials and build history, and serves the UI;
**agents** execute the pipeline steps. Builds should run on agents so heavy Docker builds and untrusted code do not
compete with or compromise the controller. The agent for this pipeline needs Python 3.12, Docker, kubectl, Trivy
and curl (see `jenkins/README.md`), plus network access to Docker Hub and the cluster API. In the lab the Jenkinsfile uses
`agent any` with a controller image (`jenkins/Dockerfile`) that bundles these tools; for production the file notes
that builds should move to a dedicated labelled agent. Agents can be static VMs, Docker containers or ephemeral
Kubernetes pods — ephemeral agents give every build a clean environment.

**37. How are images tagged, and why not just deploy `latest`?**
Each build is tagged `<BUILD_NUMBER>-<git short sha>` (for example `42-a1b2c3d`), with `latest` added only on
`main` for convenience. Deployments always reference the **immutable** tag: it identifies exactly which commit
runs in production, makes `kubectl rollout undo` meaningful (the previous ReplicaSet points to a different, still
existing tag), and avoids nodes running different code under the same name — with `latest`, `IfNotPresent` would
keep stale images and a changed tag would not even trigger a rollout. The OCI labels (`revision`, `created`) add
the same traceability inside the image.

**38. What happens if the smoke test fails after a deploy?**
`scripts/smoke_test.sh <BASE_URL>` checks `/health`, registers and logs in a random user, sends a chat message and
reads the history; any failure exits non-zero, which fails the stage. The Rollback stage (with a
`post { failure }` safety net) then runs `scripts/rollback.sh <strategy>`: for **rolling** it executes
`kubectl -n scalebotai rollout undo deployment/scalebotai-api` and waits for the rollout; for **canary** it deletes
the `scalebotai-api-canary` Deployment (and undoes the stable one if the image was already promoted); for
**blue-green** it points the active Service back to the previous colour, which is still running. The build stays
red so the team investigates, but users are back on the last good version within minutes.

**39. How is the pipeline triggered and parameterised?**
It is triggered by an SCM webhook from the Git host (or SCM polling in a lab without inbound access); as a
multibranch job every branch gets CI, but the Kubernetes Deploy stage runs only on `main`. It can also be started
manually with parameters: `DOCKERHUB_USER` (default `scalebotai`, so forks can push to their own
account), `DEPLOY_STRATEGY` (`rolling` | `canary` | `blue-green`) and `SKIP_DEPLOY` (build, test, scan and push
without touching the cluster — useful for feature branches or when the cluster is down). Parameters make one
Jenkinsfile serve CI-only runs and full CD runs without copies of the pipeline.

---

## E. FastAPI

**40. Async vs sync in FastAPI — and what about blocking calls like bcrypt?**
`async def` endpoints run on the event loop: while one request awaits PostgreSQL (asyncpg) or Redis, the loop
serves others, which is how a single process handles hundreds of concurrent I/O-bound requests. Plain `def`
endpoints are run by FastAPI in a threadpool automatically. The danger is a **blocking call inside an `async def`**:
bcrypt at cost 12 burns about 250 ms of CPU, and calling it directly would freeze every request on that pod —
including readiness probes, so Kubernetes might pull the pod out of rotation. `src/app/core/security.py` therefore
wraps hashing and verification in `run_in_threadpool`; pyca/bcrypt releases the GIL, so hashing runs truly in
parallel with the event loop. The same rule applies to any CPU-heavy or synchronous library call.

**41. How does Pydantic validation work in your API?**
Each request body is a Pydantic v2 model: `RegisterRequest` constrains `username` to 3–50 characters matching
`^[a-zA-Z0-9_.-]+$`, validates `email` as an `EmailStr` and lowercases it, and the shared `Password` type enforces
8–128 characters with an `AfterValidator` requiring at least one letter and one digit. `ChatRequest` strips the
message and rejects blank text, `FeedbackRequest` bounds `rating` to 1–5. FastAPI validates before the handler runs;
failures become `422 VALIDATION_ERROR` with `details` listing `loc`, `msg` and `type` for each field. Response
models work in the other direction — `UserResponse` simply has no `password_hash` field, so it can never leak.

**42. Explain dependency injection in FastAPI with an example from your code.**
A handler declares what it needs as parameters with `Depends(...)` and FastAPI builds the dependency graph per
request, caching each dependency once per request. For an admin endpoint the chain in `src/app/api/deps.py` is:
`HTTPBearer` extracts the token → `get_token_payload` decodes and verifies it and checks the Redis denylist →
`get_current_user` loads the user from the `user:{id}` cache or PostgreSQL (403 if disabled) →
`rate_limited_user` applies the per-user limit → `require_admin` returns 403 unless `role == "admin"`. The DB
session dependency `get_session` is a generator that yields an `AsyncSession` and always closes it. In tests the
same mechanism swaps real resources for SQLite and fakeredis without changing endpoint code.

**43. What does your middleware do, and why is it written as pure ASGI?**
`RequestContextMiddleware` (`src/app/middleware/request_context.py`) is the outermost layer: it accepts a safe
incoming `X-Request-ID` or generates one, stores it in a context variable for the logger, adds `X-Request-ID` and
`X-Process-Time-ms` to the response, increments `scalebot_http_requests_in_progress`, records
`scalebot_http_requests_total` and the duration histogram with the **templated** route, writes the JSON access log
(`log_type: api`) and bumps the daily Redis counter after the response is sent. `/metrics` and `/health*` are
excluded so probe and scrape traffic does not distort business metrics. It is pure ASGI rather than
`BaseHTTPMiddleware` to avoid an extra task per request and to let context variables flow into handlers.

**44. How is the uniform error envelope implemented?**
`src/app/core/errors.py` defines `AppError` subclasses (`UnauthorizedError`, `ForbiddenError`, `NotFoundError`,
`ConflictError`, `RateLimitedError`, …), each carrying a status code and machine-readable `code`. Exception handlers
registered on the app convert them — and FastAPI's `RequestValidationError`, Starlette's `HTTPException` and any
unhandled exception — into `{"error": {"code", "message", "details", "request_id"}}`. Unhandled exceptions are logged
with the traceback (`log_type: error`) but the client only sees `INTERNAL_ERROR` with the request ID, so internals
never leak. Even NGINX's own 429/503/404 pages return the same envelope.

**45. What happens in the application lifespan (startup and shutdown)?**
On startup `create_app()`'s lifespan creates the async engine and pool, the Redis client and the bot engine; logs a
warning if `JWT_SECRET_KEY` is the insecure default or shorter than 32 characters; creates tables under a PostgreSQL
advisory lock when `AUTO_CREATE_TABLES=true` (retrying while the database starts); bootstraps the admin from
`ADMIN_*` settings; and publishes `scalebot_app_info` and the pool-size gauge. On shutdown it closes the Ollama HTTP
client, the Redis connection and disposes of the DB pool so connections are returned cleanly. The startup probe's
120 s budget covers this work.

**46. How does JWT authentication work in your API?**
Login verifies the password (bcrypt in a threadpool) and issues two HS256 tokens with claims `sub` (user UUID),
`role`, `type` (`access`/`refresh`), `jti`, `iss=scalebotai`, `iat` and `exp`: a 30-minute access token and a
7-day refresh token. Clients send `Authorization: Bearer <access_token>`; the server pins the algorithm list to
`HS256` (preventing `alg` confusion), requires all claims, checks the issuer and token type, then checks the
`jti` against the Redis denylist. Refresh rotates: the old refresh `jti` is denylisted and a new pair is returned.
Errors are `401 UNAUTHORIZED` with `WWW-Authenticate: Bearer`; a disabled account gets `403`.

**47. How do you test the API without PostgreSQL and Redis?**
Unit tests run with `pytest` from the repository root (`pythonpath = ["src"]` in `pyproject.toml`). `create_app()`
accepts factories, and `tests/conftest.py` builds the app with `DATABASE_URL` pointing at SQLite via `aiosqlite`
and a `fakeredis` client, then drives it with an HTTP test client — so tests exercise the real routing, validation,
dependencies and SQL without external services. The models are portable (e.g. `BIGINT` maps to SQLite `INTEGER`,
foreign keys enabled with `PRAGMA foreign_keys=ON` so cascades behave). Integration tests in `tests/integration/`
and the post-deploy smoke test cover real PostgreSQL/Redis behaviour.

---

## F. CI/CD

**48. Continuous integration vs continuous delivery vs continuous deployment — which is ScaleBotAI?**
**Continuous integration** merges frequently and verifies every change automatically (build, tests, lint).
**Continuous delivery** keeps every passing build releasable, with a human deciding when to deploy;
**continuous deployment** deploys every passing build automatically. ScaleBotAI's Jenkins pipeline implements
continuous deployment to the lab cluster — every successful build is deployed, smoke-tested and rolled back
automatically on failure — while `SKIP_DEPLOY=true` turns it into continuous delivery (a scanned, pushed,
deployable image without deployment), which is how you would gate production behind an approval.

**49. How does rollback work?**
Every change to a Deployment's pod template creates a new ReplicaSet; Kubernetes keeps the previous ones
(`revisionHistoryLimit: 10`). `kubectl rollout undo deployment/scalebotai-api` re-applies the previous revision's
template, again as a zero-downtime rolling update; `--to-revision=N` targets an older one, and the
`kubernetes.io/change-cause` annotation shows which build each revision was in `kubectl rollout history`.
`scripts/rollback.sh` wraps this per strategy and is called by Jenkins. Rollback only restores the pod template —
not ConfigMaps, Secrets or database schema — which is why schema changes must be backward compatible (Q53).

**50. Blue-green vs canary vs rolling — when would you use each?**
**Rolling** (the default) replaces pods gradually in place: no extra infrastructure, but old and new versions
serve simultaneously and rollback is another rollout. **Blue-green** runs two complete environments (`color: blue`
and `color: green`) and switches traffic at once by changing a Service selector: instant cut-over and instant
rollback, at the cost of double capacity during the release. **Canary** sends a small share of real traffic to the
new version first — in ScaleBotAI a `scalebotai-api-canary` Deployment labelled `track: canary` sits behind the same
Service, so traffic splits roughly by replica ratio — and promotes only if error rate and latency stay healthy;
it limits the blast radius of a bad release. `DEPLOY_STRATEGY` selects one per build; see
`docs/12-deployment-strategies.md`.

**51. What does Trivy check, and how do you handle its findings?**
Trivy scans the built image's OS packages (Debian in `python:3.12-slim`) and language dependencies (the Python
packages in `/opt/venv`) against vulnerability databases, and can also detect secrets and misconfigurations. In the
Security Scan stage it runs before the push, so a vulnerable image never reaches the registry or cluster
("shift-left"): it archives a report of all HIGH and CRITICAL findings, fails the build on any **CRITICAL**
vulnerability that has a fix available (`--severity CRITICAL --ignore-unfixed --exit-code 1`), fails on leaked
secrets in the repository, and reports Dockerfile/Kubernetes misconfigurations (`trivy fs --scanners misconfig`).
Bandit adds Python SAST in the Lint stage. Findings are fixed by
upgrading the base image or the dependency; unfixable or irrelevant ones are documented in an ignore file with a
justification and an expiry, never silently suppressed. The multi-stage build keeps the scanned surface small.

**52. Which tests run where (the test pyramid)?**
The base is fast **unit tests** (pytest with SQLite + fakeredis) on every commit in the Unit Test stage — and also
inside the Dockerfile's `test` target. **Lint** (Ruff) catches style and bug patterns. **Integration tests** exercise
real PostgreSQL/Redis. After deploying, the **smoke test** verifies the live system end to end through the ingress.
**Load tests** (k6 primary, Locust and JMeter as alternatives) run separately — smoke, baseline, load, stress, spike
and soak — because they take minutes to hours and need a stable environment. The higher the level, the fewer and
slower the tests.

**53. How do you change the database schema without breaking a rolling deployment?**
During a rolling update old and new pods run simultaneously against the same database, so every schema change must
work with both versions. The **expand → migrate → contract** pattern does this: first add new structures in a
backward-compatible way (new nullable column, new table), deploy code that writes both/reads new, backfill data,
and only in a later release remove the old column. ScaleBotAI currently creates tables with `create_all`, which
never alters existing tables — so schema changes would require Alembic migrations run as a pre-deploy pipeline step;
this is listed as future work (ADR-014).

**54. What does "build once, deploy many" mean in your pipeline?**
The image is built and tested exactly once, tagged immutably (`<BUILD_NUMBER>-<sha>`), scanned and pushed; every
later step — deployment, smoke test, promotion to another environment, rollback — uses that same artifact rather
than rebuilding. Environment differences come only from configuration (ConfigMap/Secret), never from different
builds, so what was tested is exactly what runs. Running the unit tests inside the Docker build's `test` stage
reinforces this: the image that passed the tests is built from the same layers.

---

## G. Scalability

**55. How does "1 million requests per day" translate into requests per second?**
1,000,000 ÷ 86,400 s ≈ **11.6 req/s** on average. Traffic is not uniform, so we assume a peak factor of 10× ⇒
**~116 req/s**, and design headroom for **500 req/s** (≈ 4× the peak) to survive spikes and a lost node. Per month
that is ~30 million requests. With ~40 % of requests being chat messages, the database receives ~400,000 inserts per
day — about 5 per second on average and 50 per second at peak, trivial for PostgreSQL. The number sounds big, but
the engineering challenge is the peaks, the latency target and failure handling, not raw throughput.

**56. What is Little's law, and how did you use it?**
Little's law states **L = λ × W**: the average number of requests in the system equals the arrival rate times the
average time each spends in it. At the 116 req/s peak with a 0.3 s latency target, about 35 requests are in flight;
at the 500 req/s headroom, about 150 — consistent with the 100–500 concurrent-request requirement. Applied to the
database: 500 req/s × ~10 ms of DB time per request ≈ 5 connections busy at once across the cluster, which shows the
per-pod pool (5 + 3 in Kubernetes) is ample and that pool exhaustion would indicate slow queries, not too little pool.
It also predicts that if latency doubles under load, concurrency doubles — which is why latency SLOs and autoscaling
are linked.

**57. Horizontal vs vertical scaling — how does each part of ScaleBotAI scale?**
Vertical scaling gives one instance more CPU/RAM; horizontal scaling adds instances. The API scales
**horizontally** (HPA 3–20 pods, or `--scale` in Compose) because it is stateless. PostgreSQL scales
**vertically** for writes in this design (one primary), with read replicas, PgBouncer and partitioning as the
horizontal levers for later. Redis is a single instance that could move to Sentinel (HA) or Cluster (sharding). The
LLM scales poorly on CPU, so it is optional and protected by timeouts and a circuit breaker.

**58. Where are the bottlenecks under load?**
First, **bcrypt**: at cost 12 each login or registration costs ~250 ms of CPU, so one 1-CPU pod handles only ~4
logins per second — a login storm is CPU-bound and the HPA's CPU metric is exactly the right signal; load tests
therefore log in once per virtual user and then chat. Second, **database connections**: pods × pool size can exceed
`max_connections` (Q59). Third, the **LLM** in `llm` mode: seconds per answer on CPU. Fourth, Redis is
single-threaded but handles ~100k simple operations per second, far above our needs. Prometheus metrics
(`scalebot_db_pool_checked_out`, CPU throttling, cache hit ratio, bot latency per mode) show which one is active.

**59. How did you size the database connection pool, and why PgBouncer?**
Each pod's SQLAlchemy pool keeps `DB_POOL_SIZE` connections and may burst to `DB_POOL_SIZE + DB_MAX_OVERFLOW`,
waiting at most 10 s for one. The rule is `pods × (pool_size + max_overflow) + ops connections < max_connections`.
The code defaults (10 + 20) would let 20 pods open 600 connections — twice the StatefulSet's
`max_connections=300` — so the Kubernetes ConfigMap sets 5 + 3: 20 × 8 = 160, leaving room for exporters and
admin sessions (Compose keeps 10 + 20 because 3 replicas × 30 = 90 < 200). Little's law shows a pod needs only a
few busy connections, so the smaller pool costs no throughput — and at real scale **PgBouncer** in transaction mode multiplexes thousands of client
connections onto ~50 server connections (with asyncpg's prepared-statement cache disabled). Details are in
`docs/04-database-design.md` §9.1.

**60. How do you handle cache invalidation?**
Each cache uses the strategy that matches its data. **Bot answers** (`bot:resp:{mode}:{sha256(normalised question)}`)
are deterministic for a given rule set, so they simply expire after 3600 s; answers that depend on time, conversation
context or the LLM fallback are never cached. The trade-off is that after a deploy that changes rule answers, old
answers can be served for up to an hour — including the app version in the key would bust it on deploy. **User
profiles** (`user:{id}`, 300 s) are explicitly deleted when the profile, password, role or active flag changes or
the account is deleted (cache-aside with invalidation on write). **Analytics** (30 s) tolerate staleness, so they
are TTL-only.

**61. What consistency does the Redis cache give you — where does CAP come in?**
CAP says that during a network **partition** a distributed system must choose between consistency and availability.
PostgreSQL is the source of truth; Redis only holds derived or ephemeral data, so cache and database are
**eventually consistent**, with staleness bounded by TTL (or by explicit invalidation for profiles). Our fail-open
policy is an explicit **availability-over-consistency** choice: if Redis is unreachable we keep serving without
rate limits and revocation checks. If Redis were replicated (Sentinel), asynchronous replication could lose
acknowledged writes on failover — acceptable for a cache, unacceptable for a source of truth, which is why nothing
authoritative is stored there.

**62. Which rate-limiting algorithm do you use, and what are its weaknesses?**
A **fixed window** per identity: `INCR ratelimit:{identity}:{epoch_minute}` plus `EXPIRE 60` in one Redis
transaction; if the count exceeds 120 the request gets `429 RATE_LIMITED` with `Retry-After` (seconds to the next
window) and `X-RateLimit-Limit/Remaining` headers. Identity is the user ID for authenticated routes and the client IP
for register, login and refresh. Because Redis is shared, the limit is global across all pods. The weakness is the
boundary burst: up to 2 × 120 requests can arrive within a few seconds around a minute boundary; sliding-window
counters or a token bucket smooth this at the cost of more Redis work. NGINX/ingress `limit_req` adds a second,
coarser layer at the edge.

**63. What do the load-test phases tell you?**
**Smoke** (100 VUs) proves the scripts and environment work. **Baseline** (1,000) establishes normal latency and
resource use. **Load** (10,000) checks the targets at expected peak: average < 300 ms, errors < 1 %, CPU and memory
< 80 %, and watches the HPA scale out. **Stress** (25,000) pushes beyond capacity to find the breaking point and the
first bottleneck. **Spike** (50,000) tests sudden bursts and how quickly autoscaling reacts. **Soak** (24 h) reveals
slow problems — memory leaks, connection leaks, log/disk growth. Scripts live in `tests/load/k6/` (and Locust/JMeter
equivalents); the very large phases need distributed load generators and a raised `RATE_LIMIT_PER_MINUTE`.

---

## H. Monitoring and logging

**64. Push vs pull monitoring — why does Prometheus pull?**
In a **pull** model the monitoring server scrapes each target's `/metrics` endpoint on its own schedule; in a
**push** model applications send metrics to a collector (StatsD, InfluxDB, OTLP push). Pulling gives a free health
signal (`up == 0` when a scrape fails), keeps targets ignorant of where monitoring lives, and lets Prometheus
control load. It requires discovery: in Kubernetes Prometheus finds pods through the `prometheus.io/scrape`,
`port` and `path` annotations, and in Compose through `dns_sd_configs` on `chatbot-api`, so every replica is scraped
individually. Push is still right for short-lived batch jobs (Pushgateway). Our log pipeline, by contrast, is push:
Filebeat ships logs to Logstash.

**65. What are RED, USE and the four golden signals — how do your dashboards use them?**
**RED** (Rate, Errors, Duration) describes request-driven services: `rate(scalebot_http_requests_total[5m])`, the
share of `status=~"5.."`, and latency from `scalebot_http_request_duration_seconds`. **USE** (Utilisation,
Saturation, Errors) describes resources: CPU and memory per pod, DB pool saturation
(`scalebot_db_pool_checked_out / scalebot_db_pool_size`), in-flight requests, Redis memory, PostgreSQL connections.
Google's **golden signals** — latency, traffic, errors, saturation — combine both views. The Grafana dashboards in
`monitoring/grafana/dashboards/` follow this split (API overview vs infrastructure), and the alert rules map to them
(`HighErrorRate`, `HighLatencyP95`, `HighCPU`, `DBPoolSaturation`, …).

**66. Why P95 instead of the average, and how is it computed?**
An average hides the tail: if 94 % of requests take 50 ms and 6 % take 3 s, the average is ~227 ms — "within SLO" —
while 1 in 17 users waits 3 seconds. Percentiles describe what users actually experience: P95 means 95 % of requests
were at least this fast. Prometheus estimates it from histogram buckets:
`histogram_quantile(0.95, sum by (le) (rate(scalebot_http_request_duration_seconds_bucket[5m])))`, interpolating
linearly inside the bucket, so accuracy depends on bucket boundaries. That is why the buckets include 0.2, **0.3**
and 0.5 s around the 300 ms target; the 0.3 bucket also gives an exact "share of requests under 300 ms" SLI. Our
alerts use both: `HighLatencyP95` (> 300 ms for 5 min) and `HighAvgLatency`.

**67. How would you find a memory leak in the API?**
Start with metrics: `process_resident_memory_bytes` per pod in Grafana during the 24-hour soak test — a sawtooth is
normal garbage collection, a steady climb under constant load is a leak. Kubernetes shows the symptom as OOMKilled
restarts (`kubectl describe pod` → Last State: OOMKilled, exit code 137) and the memory-based HPA scaling up without
more traffic. Then correlate with deploys and with growth in metrics series or caches. To locate it, reproduce under
load locally and use `tracemalloc` snapshots or tools such as memray/py-spy to see which allocations grow. Typical
causes are unbounded in-process caches, objects held in global lists, unclosed clients, and **unbounded metric
labels** — avoided here by labelling requests with the templated route, not the raw path.

**68. What role does each component of the ELK stack play?**
The app writes one JSON object per line to stdout (and to `LOG_FILE` in Compose) with `@timestamp`, `level`,
`log_type` (api, user, error, system), `request_id`, `user_id`, `route`, `status_code`, `latency_ms` and more.
**Filebeat** (a DaemonSet in Kubernetes) tails container logs on each node and ships them to Logstash on port 5044;
in Compose, **Logstash** reads the shared `app-logs` volume with a file input. Logstash parses and validates the JSON,
copies every error event into the error index, and writes to **Elasticsearch** daily indices
`scalebot-{log_type}-logs-YYYY.MM.dd`, which index and store the documents for search and aggregation. **Kibana**
provides discovery, dashboards and saved searches over the `scalebot-*-logs-*` data views.

**69. Metrics vs logs vs traces — why do you need more than one?**
**Metrics** are cheap numeric time series, ideal for dashboards and alerts ("error rate is 3 %"), but they lose
per-request detail. **Logs** are per-event records with full context ("this request by this user failed with this
exception"), expensive to store but essential for root cause. **Traces** follow one request across components with
timing per span ("400 ms were spent in PostgreSQL"). ScaleBotAI has metrics (Prometheus) and logs (ELK) linked by
`request_id`; distributed tracing with OpenTelemetry and Jaeger/Tempo is future work. The workflow is: alert from
metrics → narrow down in Grafana → find the failing requests in Kibana.

**70. Why is the `route` label the templated path rather than the actual URL?**
Every unique combination of label values creates a separate time series in Prometheus. Using the raw path
(`/api/v1/chat/conversations/9b2e…`) would create a new series for every conversation ID — unbounded
**cardinality**, which bloats Prometheus memory, slows queries and can crash it. The middleware labels requests with
the route template (`/api/v1/chat/conversations/{conversation_id}`) and uses `unmatched` for unknown paths, so the
number of series is bounded by routes × methods × status codes. The same reasoning is why user IDs never appear as
metric labels — they belong in logs.

**71. How did you design alerts, and how do you avoid alert fatigue?**
Alerts in `monitoring/prometheus/alert-rules.yml` focus on **symptoms users feel** (`HighErrorRate` > 1 % 5xx,
`HighLatencyP95` > 300 ms, `ScaleBotAPIDown`) plus key **causes** (`HighCPU`, `HighMemory`, `DBPoolSaturation`,
`PostgresDown`, `RedisDown`, `LowCacheHitRatio`, `RateLimitingSpike`, `TooManyPostgresConnections`). Each uses a
`for:` duration (typically 5 minutes) so short blips do not page anyone, a `severity` label to separate critical
from warning, and a runbook annotation telling the on-call person what to check first. Pre-computed recording rules
keep the expressions cheap. The next step is SLO burn-rate alerting, which pages only when the error budget is being
consumed too fast.

**72. A user says "my request failed at 14:05". How do you investigate?**
Every response carries `X-Request-ID`, and the error envelope includes the same `request_id`; if the user (or the
client app) has it, a single Kibana query `request_id:"…"` finds the access-log line and any error log with the stack
trace, including the pod `hostname`. Without the ID, filter `scalebot-api-logs-*` by `user_id` and time window and look
for `status_code >= 400`. Then check Grafana for that minute — was there an error spike, a deploy (`kubectl rollout
history`), a database or Redis outage, or rate limiting (`scalebot_rate_limited_total`)? Passing an incoming
`X-Request-ID` through NGINX keeps the ID consistent from the edge to the application.

---

## I. Security and database

**73. JWTs are stateless — how can a user log out?**
A JWT stays valid until `exp` because the server keeps no session; deleting it on the client does not stop a stolen
copy. ScaleBotAI combines **short-lived access tokens** (30 minutes) with a **denylist**: logout stores the token's
`jti` in Redis as `jwt:denylist:{jti}` with a TTL equal to the token's remaining lifetime, and every request checks
that key (one fast Redis lookup). If the client sends its refresh token in the logout body it is revoked too, and
refresh tokens rotate — each works only once. The denylist cleans itself up because entries expire exactly when the
tokens would have. The trade-off is that revocation depends on Redis: in a Redis outage (fail-open) a logged-out
token works until it expires.

**74. How are passwords stored, and why bcrypt rather than SHA-256?**
Passwords are hashed with **bcrypt** at cost 12 — a deliberately slow, salted, adaptive algorithm (2¹² rounds of
expensive key setup, ~250 ms) — so stolen hashes are expensive to brute-force, while fast hashes like plain SHA-256
can be tried billions of times per second on GPUs. The cost factor can be raised as hardware improves. bcrypt only
reads the first 72 bytes, so the password is first SHA-256 hashed and base64-encoded (44 bytes, no NUL bytes) — every
character counts. Login also hashes against a dummy hash when the user does not exist, so response timing does not
reveal which usernames are registered, and the error message is the same ("Invalid username or password").

**75. How is authorisation (RBAC and object ownership) enforced?**
Role-based access: the `role` column (`user` or `admin`, protected by a CHECK constraint) is loaded with the user,
and every `/api/v1/admin/*` route depends on `require_admin`, which returns `403 FORBIDDEN` for non-admins. Object
ownership: every query on conversations, chats and feedback includes `user_id = <current user>` from the verified
token, so users can only reach their own data. Requests for someone else's conversation return **404, not 403**, so
the API does not even confirm that the object exists (no ID enumeration). Admins can disable accounts or change
roles via `PATCH /api/v1/admin/users/{user_id}`, which also evicts the user's profile cache so the change applies on
their next request.

**76. How does the project address common web vulnerabilities (OWASP Top 10)?**
**Injection:** all SQL goes through SQLAlchemy with bound parameters — no string concatenation. **Broken access
control:** ownership filters and RBAC (Q75). **Cryptographic failures:** bcrypt, TLS at NGINX/ingress, secrets in
Kubernetes Secrets, a startup warning for weak JWT secrets. **Security misconfiguration:** non-root, read-only
containers, NetworkPolicies, `/metrics` blocked at the edge. **Vulnerable components:** Trivy in CI. **Identification
failures:** rate-limited login, timing-equalised authentication, token revocation. **Logging/monitoring failures:**
structured audit events (`log_type: user`) and alerts. Strict input validation (Pydantic) and bounded request sizes
reduce the attack surface further; the LLM system prompt tells the model to ignore prompt-injection attempts.

**77. Why must `/metrics` not be public, and what is "defence in depth" in your design?**
`/metrics` exposes internal details — routes, error rates, pool sizes, version — that help attackers plan, and
scraping it is free load. NGINX denies it (`location ^~ /metrics { deny all; }`), the Ingress does not route it, and
NetworkPolicies only allow the `monitoring` namespace to reach the pods; Prometheus scrapes pods directly inside the
cluster. Defence in depth means no single control is relied on: rate limiting exists at NGINX/ingress **and** in the
app; validation in Pydantic **and** database constraints; authentication **and** NetworkPolicies; non-root user
**and** read-only filesystem **and** dropped capabilities.

**78. Is your database normalised? Which normal form?**
`users`, `conversations` and `feedback` are in BCNF: every determinant is a candidate key (including `username`,
`email` and `feedback.chat_id`). `chats` contains one deliberate denormalisation: `chats.user_id` is derivable from
`chats.conversation_id → conversations.user_id`, a transitive dependency that strictly violates 3NF. It is kept
because the hottest query — paginated history across all of a user's conversations — becomes a single-table index
scan on `(user_id, timestamp)` without a join or sort, ownership checks are single-row, and bulk deletes are simple.
It is safe because conversations never change owner and the app writes `user_id` from the verified owner in the same
transaction; a composite foreign key could enforce it in the database.

**79. How did you choose your indexes?**
Each index serves a specific query and leads with the equality column followed by the sort/range column:
`ix_chats_user_timestamp (user_id, timestamp)` serves history newest-first, `ix_chats_conversation_timestamp
(conversation_id, timestamp)` serves conversation transcripts and the LLM's last-5 context,
`ix_conversations_user_updated (user_id, updated_at)` serves the conversation list, `ix_users_created_at` serves
"new users today/7 days", and the unique constraints serve login lookups and the feedback upsert. PostgreSQL does not
index foreign keys automatically; every FK here is the leading column of an index, which keeps `ON DELETE CASCADE`
from scanning whole tables. Indexes cost write time and memory, so none exists without a query that needs it.

**80. Why does the app take a PostgreSQL advisory lock at startup?**
With `AUTO_CREATE_TABLES=true` every replica runs `metadata.create_all` at startup, and during a scale-out or the
first deploy several pods start at the same moment. Without coordination two pods can both see a table as missing
and both try to create it; the loser fails on a catalogue unique violation (`pg_type_typname_nsp_index`) even with
`IF NOT EXISTS`. `src/app/db/session.py` therefore runs `SELECT pg_advisory_xact_lock(815100001)` inside the
creation transaction: one pod creates the tables while the others wait, then find them present. Being
transaction-scoped, the lock is released automatically on commit or if the pod dies, so it can never be leaked.

**81. What are your backup, RPO and RTO targets?**
**RPO** is how much data you can afford to lose; **RTO** is how long recovery may take. The capstone baseline is a
nightly `pg_dump --format=custom` Kubernetes CronJob to a separate PVC (RPO ≤ 24 h, RTO ≈ 15–30 min); the production
target adds base backups plus continuous WAL archiving to object storage for point-in-time recovery (RPO ≤ 5 min,
RTO ≤ 30 min), and an HA replica with automatic failover would bring RTO under two minutes. Because 99.9 % only allows
~43 minutes of downtime a month, restores must be rehearsed and timed. Account deletions cascade immediately, but
deleted data survives in backups until they expire, which the privacy policy must state
(`docs/04-database-design.md` §11–12).

---

## Quick-revision cheat sheet

| Concept | One-liner |
|---|---|
| Stateless API | No state in pods; PostgreSQL + Redis + JWT ⇒ any pod serves any request, HPA can add/remove freely |
| 1 M req/day | ≈ 11.6 req/s avg, ~116 peak (10×), design for 500 |
| Little's law | L = λ × W: 116 req/s × 0.3 s ≈ 35 in flight |
| Error budget 99.9 % | ≈ 43 min downtime per 30-day month |
| Container | Process + namespaces (isolation) + cgroups (limits) + layered FS; shares the host kernel |
| Multi-stage build | Build tools and tests in early stages; ship only venv + code ⇒ smaller, fewer CVEs |
| CMD vs ENTRYPOINT | ENTRYPOINT = executable, CMD = default args; use exec form so PID 1 gets SIGTERM |
| COPY vs ADD | COPY copies; ADD also fetches URLs and unpacks tars — prefer COPY |
| Layer cache | First changed instruction invalidates the rest ⇒ copy requirements before source |
| Bridge vs host network | User-defined bridge = isolation + DNS by service name; host = no isolation, port clashes |
| Volume vs bind mount | Volume = Docker-managed data; bind mount = host path, good for config |
| Non-root UID 10001 | Limits damage on compromise; numeric UID lets K8s verify runAsNonRoot |
| Startup probe | Delays liveness/readiness until the app has started (120 s budget) |
| Liveness probe | Failing ⇒ restart; checks only the process (`/health/live`) |
| Readiness probe | Failing ⇒ removed from endpoints, no restart; 503 when DB is down (`/health/ready`) |
| maxSurge 25 % / maxUnavailable 0 | +1 pod at a time, never below 3 ready ⇒ zero-downtime rollout |
| preStop sleep 5 | Keeps serving while endpoint removal propagates; then SIGTERM, 30 s grace |
| Node failure | NotReady after ~40–50 s, endpoints removed, pods evicted after 300 s and recreated elsewhere |
| HPA formula | desired = ceil(current × actual / target), max over metrics, clamped 3–20 |
| HPA anti-flapping | 10 % tolerance, 300 s scale-down window, 10 %/min scale-down, fast scale-up |
| Requests vs limits | Requests = scheduling + HPA base; CPU over limit ⇒ throttled, memory over limit ⇒ OOMKilled (137) |
| ConfigMap vs Secret | Same mechanics; Secret is base64 (not encrypted) with separate RBAC and tmpfs mounts |
| StatefulSet vs Deployment | Stable identity + own volume + ordering (PostgreSQL) vs interchangeable pods (API) |
| PDB minAvailable 2 | Voluntary evictions (drains) cannot drop below 2 ready API pods |
| NetworkPolicy | Default deny + explicit allows; needs Calico/Cilium to be enforced |
| Declarative pipeline | Structured `pipeline {}` with stages and `post`; scripted = free-form Groovy |
| Immutable tags | `<BUILD_NUMBER>-<sha>`: traceable, rollback-safe, never deploy `latest` |
| Rollback | `kubectl rollout undo` ⇒ previous ReplicaSet template; does not undo schema/config |
| Blue-green | Two full versions, switch Service selector: instant switch and rollback, 2× capacity |
| Canary | Small share of traffic to the new version first; promote on healthy metrics |
| Expand/contract | Backward-compatible schema changes across rolling deploys |
| Trivy | Scans image OS packages + Python deps for CVEs; gates the push |
| async vs blocking | Never block the event loop; bcrypt runs in `run_in_threadpool` |
| Pydantic | Declarative validation ⇒ 422 `VALIDATION_ERROR` with field details |
| Depends() | Per-request dependency graph: token → user → rate limit → admin check; overridable in tests |
| Error envelope | `{"error": {code, message, details, request_id}}` for every error |
| JWT logout | Short expiry + `jti` denylist in Redis with TTL = remaining life |
| bcrypt | Slow, salted, adaptive (cost 12 ≈ 250 ms); SHA-256 pre-hash beats the 72-byte limit |
| 404 vs 403 | Others' objects ⇒ 404 (no existence leak); non-admin on admin route ⇒ 403 |
| Fail-open Redis | Availability over strictness: no cache/limit/revocation during outage, API stays up |
| Cache invalidation | TTL for derived data, explicit delete on write for user profiles |
| CAP for cache | Redis is derived data ⇒ eventual consistency bounded by TTL; PostgreSQL is truth |
| Fixed-window limiter | `INCR` per identity per minute; boundary burst up to 2× |
| Pool math | pods × (pool + overflow) < max_connections (200) ⇒ PgBouncer at scale |
| 3NF exception | `chats.user_id` denormalised for single-table history queries |
| FK indexes | PostgreSQL doesn't auto-index FKs; needed for joins and fast cascades |
| Advisory lock | `pg_advisory_xact_lock` serialises `create_all` across replicas |
| Pull monitoring | Prometheus scrapes `/metrics`; `up` = free health signal |
| RED / USE | RED for services (rate, errors, duration); USE for resources (utilisation, saturation, errors) |
| P95 vs average | Average hides the tail; P95 from histogram buckets via `histogram_quantile` |
| Label cardinality | Templated `route` label keeps series count bounded |
| Memory leak | Rising RSS under constant load in soak test, OOMKilled 137 ⇒ tracemalloc/memray |
| ELK | Filebeat ships, Logstash parses/routes, Elasticsearch stores/searches, Kibana visualises |
| Metrics/logs/traces | Alert on metrics, diagnose with logs (linked by `request_id`), traces = future |
| RPO / RTO | Data you may lose / time to recover; nightly dump = 24 h RPO, WAL archiving ≈ minutes |
