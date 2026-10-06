# 14 — Resume, LinkedIn and Interview Content

> Ready-to-use text for presenting ScaleBotAI on a resume, on LinkedIn and in interviews.
>
> **Honesty rule.** The scale figures in this project (1 M+ requests/day, < 300 ms average latency, 99.9 %
> availability, 100–500 concurrent requests) are **design targets** that were **validated in load tests** in a lab
> environment. Phrase them that way ("designed for…", "validated in k6 load tests…"). Where a bullet contains a
> bracketed placeholder such as `[p95 = ___ ms]`, fill it in from your own k6/Locust report — or delete it.
> Never claim production traffic or real users you did not have; interviewers probe numbers.

---

## 1. Project description (3–4 lines)

**ScaleBotAI — Cloud-Native High-Scale Chatbot Platform** | Python, FastAPI, PostgreSQL, Redis, Docker, Kubernetes, Jenkins, Prometheus, Grafana, ELK

Built a production-style chatbot backend (a DevOps learning assistant) with JWT authentication, conversation
history and RBAC-protected admin analytics, packaged as hardened Docker images and deployed on Kubernetes with
HPA autoscaling (3–20 pods) and zero-downtime rolling updates. Automated delivery with a Jenkins CI/CD pipeline
(tests, lint, Trivy scan, deploy, smoke test, auto-rollback) and added full observability with
Prometheus/Grafana and the ELK stack; designed for 1 M+ requests/day at < 300 ms average latency.

---

## 2. ATS-optimised bullet points (pick 4–6 for a one-page resume)

1. **Architected** a cloud-native chatbot platform in **Python 3.12 / FastAPI** exposing **32 REST endpoints**
   (JWT auth, chat history, RBAC-protected admin analytics) on **PostgreSQL 16** (async SQLAlchemy + asyncpg) and
   **Redis**, with a uniform JSON error contract and auto-generated OpenAPI/Swagger documentation.
2. **Designed for 1 M+ requests/day** (≈ 116 req/s peak, 500 req/s headroom) and **validated in k6 load tests**
   against targets of **< 300 ms average latency** and **< 1 % error rate** `[measured: avg ___ ms, p95 ___ ms at ___ req/s]`;
   cross-checked with Locust and JMeter.
3. **Deployed on Kubernetes** with Deployments, a PostgreSQL StatefulSet, ingress-nginx with TLS, ConfigMaps/Secrets
   and a **Horizontal Pod Autoscaler (HPA)** scaling **3 → 20 pods** on CPU (70 %) and memory (75 %) utilisation.
4. **Engineered zero-downtime releases** using RollingUpdate (`maxSurge 25 %`, `maxUnavailable 0`) with
   startup, liveness and readiness probes, plus selectable **rolling, canary and blue-green** deployment strategies.
5. **Automated CI/CD** with a 9-stage declarative **Jenkins** pipeline — build, **pytest**, **Ruff** lint + **Bandit** SAST, Docker build,
   **Trivy** image scan, push, `kubectl` deploy, smoke test — with **automatic rollback** (`kubectl rollout undo`) on failure.
6. **Containerised** the stack with **multi-stage Docker** builds running as a non-root user and a **12-service Docker
   Compose** environment (API, PostgreSQL, Redis, **NGINX** load balancer, Prometheus, Grafana, ELK, exporters)
   supporting horizontal scaling with `--scale`.
7. **Instrumented** the API with custom **Prometheus** metrics (request rate, errors, latency histograms with a 300 ms SLO
   bucket, cache hit ratio, DB-pool saturation) and built **Grafana** dashboards and alert rules on the RED/USE methods.
8. **Centralised structured JSON logging** (api / user / error / system streams) through Filebeat → Logstash →
   Elasticsearch → Kibana (**ELK**), correlating every request end-to-end with an `X-Request-ID`.
9. **Hardened security** with bcrypt password hashing, **JWT** access/refresh tokens with Redis-backed revocation,
   **RBAC**, two-layer rate limiting (NGINX + Redis), TLS termination and **Trivy** vulnerability gating in CI.
10. **Reduced database load** by caching bot responses, user profiles and analytics in **Redis** (TTL-based, fail-open
    on outage) and designing an indexed **3NF PostgreSQL** schema with cascading deletes for GDPR-style erasure.

---

## 3. Short variant (one-page resume, 3 bullets)

**ScaleBotAI — Cloud-Native Chatbot Platform** | FastAPI · PostgreSQL · Redis · Docker · Kubernetes · Jenkins · Prometheus · ELK
- Built a FastAPI chatbot backend (32 REST endpoints, JWT + RBAC, Redis caching) designed for 1 M+ requests/day and
  validated in k6 load tests against a < 300 ms average-latency target.
- Deployed on Kubernetes with HPA autoscaling (3–20 pods), ingress-nginx TLS and zero-downtime rolling updates;
  automated CI/CD in Jenkins with pytest, Trivy scanning, smoke tests and automatic rollback.
- Implemented observability with Prometheus/Grafana dashboards and alerts and ELK centralised logging.

## 4. Long variant (detailed CV / portfolio / project report)

**ScaleBotAI — Cloud-Native High-Scale Chatbot Platform** (Final-year capstone, 2026)
*Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 (async), PostgreSQL 16, Redis, Docker, Docker Compose, NGINX,
Kubernetes (HPA, Ingress, StatefulSet, Kustomize), Jenkins, Trivy, Prometheus, Grafana, Elasticsearch, Logstash,
Kibana, Filebeat, k6, Locust, JMeter, Ollama (TinyLlama)*

A DevOps learning-assistant chatbot engineered as a production-style service. Goals: 1 M+ requests/day,
99.9 % availability, < 300 ms average latency, 100–500 concurrent requests, autoscaling and zero-downtime deploys.

- **Backend:** FastAPI application with 32 endpoints across auth, users, chat, bot and admin domains; async
  PostgreSQL access through SQLAlchemy 2 + asyncpg; Pydantic v2 validation; uniform error envelope with request IDs;
  bcrypt executed in a threadpool so CPU-bound hashing never blocks the event loop.
- **AI engine:** rule-based intent/regex engine (default, millisecond responses, Redis-cached) and an optional
  TinyLlama LLM mode via Ollama with conversation context and automatic fallback on timeout.
- **Data:** normalised PostgreSQL schema (users → conversations → chats → feedback) with composite indexes for every
  hot query, cascade deletes for account erasure and an advisory-lock-guarded schema bootstrap safe for concurrent
  replicas; Redis for response/profile/analytics caching, distributed rate limiting, JWT denylist and daily counters.
- **Containers:** multi-stage, non-root Docker image; Compose stack with segmented frontend/backend/monitoring networks,
  NGINX TLS reverse proxy and load balancer, and horizontally scalable API replicas.
- **Kubernetes:** Deployment with probes, resource requests/limits and RollingUpdate (`maxUnavailable 0`); HPA
  (autoscaling/v2, 3–20 replicas, CPU 70 % / memory 75 %, fast scale-up/slow scale-down); PostgreSQL StatefulSet,
  Redis, ingress-nginx with TLS; least-privilege ServiceAccounts and RBAC for the Jenkins deployer.
- **CI/CD:** declarative Jenkins pipeline (Checkout → Build → Unit Test → Lint → Docker Build → Security Scan →
  Docker Push → Kubernetes Deploy → Smoke Test → Rollback on failure), immutable `<build>-<sha>` image tags,
  rolling/canary/blue-green strategies selected by a pipeline parameter.
- **Observability:** custom Prometheus metrics, exporters for PostgreSQL/Redis/NGINX, Grafana dashboards and alert
  rules; JSON logs shipped to Elasticsearch through Logstash/Filebeat and explored in Kibana.
- **Performance engineering:** load-test plan (smoke → baseline → load → stress → spike → 24 h soak) in k6, Locust and
  JMeter with explicit pass criteria (avg < 300 ms, errors < 1 %, CPU/memory < 80 %) `[add your measured results]`.

---

## 5. Skills / keywords line (ATS)

**Skills:** Python, FastAPI, REST API Design, OpenAPI/Swagger, Pydantic, AsyncIO, SQLAlchemy, PostgreSQL, SQL,
Database Indexing, Redis, Caching, Rate Limiting, JWT, OAuth2 Bearer Authentication, RBAC, bcrypt, Docker,
Multi-stage Docker Builds, Docker Compose, NGINX, Load Balancing, TLS, Kubernetes, kubectl, Kustomize, HPA
(Horizontal Pod Autoscaler), Ingress, StatefulSet, ConfigMaps & Secrets, Rolling Updates, Canary & Blue-Green
Deployments, Jenkins, CI/CD, DevOps, DevSecOps, Trivy, Bandit, SAST, Container Security, Prometheus, Grafana, Alerting, ELK Stack
(Elasticsearch, Logstash, Kibana), Filebeat, Observability, SRE, SLOs, Load Testing, k6, Locust, JMeter, Performance
Tuning, Scalability, High Availability, Microservices, Cloud-Native, Linux, Bash, Git, pytest, Ruff, LLM, Ollama.

---

## 6. LinkedIn project summary (~160 words)

> **ScaleBotAI — Cloud-Native High-Scale Chatbot Platform**
>
> ScaleBotAI is my final-year capstone: a chatbot platform engineered to behave like a production service, not a
> demo. The backend is a Python 3.12 FastAPI application — a DevOps learning assistant — with JWT authentication,
> role-based admin APIs, conversation history in PostgreSQL, and Redis for caching, rate limiting and token
> revocation. A rule-based intent engine answers by default; an optional TinyLlama model (via Ollama) falls back
> automatically when it is slow.
>
> Most of the work went into the platform. The API ships as a multi-stage, non-root Docker image and runs on
> Kubernetes behind ingress-nginx, with a Horizontal Pod Autoscaler scaling from 3 to 20 pods and rolling updates
> configured for zero downtime. A Jenkins pipeline tests, lints, scans the image with Trivy, deploys, smoke-tests and
> rolls back automatically on failure. Prometheus and Grafana track latency, errors and saturation; the ELK stack
> centralises structured logs.
>
> It is designed for 1M+ requests/day at under 300 ms average latency, validated with k6, Locust and JMeter load tests.

---

## 7. LinkedIn post draft

> Wrapped up my final-year capstone: **ScaleBotAI**, a cloud-native chatbot platform built to production standards.
>
> The goal wasn't "make a chatbot" — it was "make a chatbot that could survive 1 million requests a day."
>
> What's inside:
> - FastAPI + PostgreSQL + Redis backend with JWT auth, RBAC and 32 REST endpoints
> - Docker multi-stage images (non-root) and a full Docker Compose stack behind NGINX
> - Kubernetes with an HPA that scales 3 → 20 pods, plus zero-downtime rolling updates
> - Jenkins CI/CD: tests → lint → Trivy scan → deploy → smoke test → auto-rollback
> - Prometheus + Grafana dashboards and alerts, ELK for centralised logs
> - Load-tested with k6, Locust and JMeter against a < 300 ms average-latency target
>
> Three things I learned the hard way:
> 1. Pool sizes multiply: 20 pods × a "small" DB pool can exhaust PostgreSQL's connections — the math matters.
> 2. One blocking call (like bcrypt) in an async handler can stall every request on that pod.
> 3. Readiness probes + `maxUnavailable: 0` are what actually make "zero downtime" true.
>
> Code and docs (architecture, ADRs, runbooks, load-test plan): [link to repository]
>
> Thanks to [mentor/guide name] and [team members] for the guidance and reviews. Feedback welcome!
>
> #DevOps #Kubernetes #Docker #FastAPI #Python #Jenkins #CICD #Prometheus #Grafana #ELK #CloudNative #SRE #CapstoneProject

*(Keep the numbers consistent with your load-test report; add a screenshot of the Grafana dashboard during the load test for reach.)*

---

## 8. 60-second elevator pitch (≈ 150 words)

> "My capstone is **ScaleBotAI**, a cloud-native chatbot platform designed for a million requests a day. The
> chatbot itself is a DevOps learning assistant built with FastAPI, PostgreSQL and Redis — it has JWT
> authentication, conversation history and admin analytics, with a fast rule-based engine and an optional
> TinyLlama model that falls back automatically.
>
> The interesting part is how it runs. Every commit goes through a Jenkins pipeline that tests, lints, scans the
> Docker image with Trivy, deploys to Kubernetes and runs a smoke test — and rolls back automatically if anything
> fails. In the cluster, a Horizontal Pod Autoscaler scales from three to twenty pods, and rolling updates never
> drop below full capacity, so deploys cause no downtime. Prometheus and Grafana show latency and errors in real
> time, and the ELK stack centralises the logs.
>
> I load-tested it with k6 against a sub-300-millisecond target, which taught me a lot about connection pools,
> caching and autoscaling behaviour."

---

## 9. Likely interviewer follow-up questions (with crisp answers)

**Q1. "1 million requests a day" — how did you size and validate that?**
1,000,000 / 86,400 s ≈ **11.6 req/s** average. Traffic is bursty, so I assumed a **10× peak ≈ 116 req/s** and
designed headroom for **500 req/s**. By Little's law, 116 req/s × 0.3 s ≈ 35 requests in flight — well inside the
100–500 concurrency target. I validated it with k6 in phases (smoke, baseline, load, stress, spike, 24 h soak)
with pass criteria of average latency < 300 ms, errors < 1 % and CPU/memory < 80 %, and watched the HPA add pods
in Grafana. `[State your measured numbers and environment honestly — e.g. "on a 3-node lab cluster we sustained ___ req/s at ___ ms p95."]`

**Q2. What happens when Redis goes down?**
The application **fails open**: every Redis call has short timeouts (0.5 s socket, 0.25 s connect), and the first
error opens a 5 s circuit breaker so later calls skip Redis instantly (measured: chat latency stayed at ~73 ms
average during a real Redis outage, versus ~2 s per request without the breaker). On error it logs and continues — cache misses fall through to computation/PostgreSQL, the app-level rate limiter allows requests
(NGINX still rate-limits at the edge), and counters are skipped. The trade-off is security: the JWT denylist cannot
be checked, so a logged-out token works until it expires — at most 30 minutes because access tokens are
short-lived. I chose availability because Redis holds no authoritative data.

**Q3. How do you get zero-downtime deployments?**
The Deployment uses RollingUpdate with `maxSurge: 25%` and `maxUnavailable: 0`, so with 3 replicas Kubernetes
first adds one new pod, waits until its **readiness probe** (`/health/ready`, which returns 503 until PostgreSQL is reachable)
passes, and only then removes an old pod — capacity never drops below three ready pods. Jenkins deploys an
immutable image tag with `kubectl set image`, waits on `kubectl rollout status --timeout=300s`, runs
`scripts/smoke_test.sh`, and on failure runs `kubectl rollout undo` to return to the previous ReplicaSet.

**Q4. How does your HPA decide to scale, and how do you stop it from flapping?**
The HPA (autoscaling/v2) computes `desired = ceil(current × currentUtilisation / target)` for CPU (target 70 % of
the 250m request) and memory (75 % of 256 Mi) and takes the larger result, bounded to 3–20 replicas. It ignores
changes within a 10 % tolerance, and the `behavior` section scales up quickly but scales down slowly through a
stabilisation window, so a brief dip in load does not remove pods that are needed a minute later.

**Q5. What would you change before putting this in real production?**
Put **PgBouncer** in front of PostgreSQL (20 pods × pool size can exceed `max_connections`), run PostgreSQL with
replicas and automated failover (CloudNativePG or a managed database) and Redis with Sentinel, replace the
`create_all` bootstrap with **Alembic** migrations, move secrets to Vault or External Secrets, adopt OIDC for
authentication, add OpenTelemetry tracing and SLO burn-rate alerts, and migrate from the retired community
ingress-nginx to the Gateway API. I documented these as ADRs and future work in the project.
