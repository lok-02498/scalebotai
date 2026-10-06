# ScaleBotAI — Capstone Documentation

**ScaleBotAI: Cloud-Native High-Scale Chatbot Platform** — final-year engineering capstone
demonstrating FastAPI, Docker, Kubernetes, Jenkins CI/CD, PostgreSQL, Redis, NGINX,
Prometheus/Grafana, ELK, security and scalability to 1,000,000+ API requests per day.

Read the chapters in order; they follow the structure of the project brief.

| # | Chapter | What it covers |
|---|---|---|
| 1 | [Executive Summary](01-executive-summary.md) | Summary, problem statement, objectives, scope, technology stack, lab-concept coverage, timeline |
| 2 | [Architecture Diagrams](02-architecture-diagrams.md) | The nine required diagrams: high-level, component, deployment, Kubernetes, CI/CD, monitoring, network, database, load testing; request sequences |
| 3 | [Component Design](03-component-design.md) | Every component explained, high-availability design, availability budget, chatbot Option 1 (rules) vs Option 2 (TinyLlama) |
| 4 | [Database Design](04-database-design.md) | ER diagram, tables, PK/FK/indexes, DDL, query patterns, optimization, capacity, backups |
| 5 | [API Design](05-api-design.md) | All 32 endpoints with request/response schemas, validation, errors |
| 6 | [Docker Implementation](06-docker-implementation.md) | Multi-stage Dockerfile, Compose stack, storage, networking, images, containers |
| 7 | [Kubernetes Implementation](07-kubernetes-implementation.md) | Every manifest explained, HPA, RBAC, NetworkPolicies, minikube lab |
| 8 | [Jenkins CI/CD Pipeline](08-cicd-jenkins.md) | Repository structure, branching, every pipeline stage, rollback, DORA metrics |
| 9 | [Monitoring & Logging](09-monitoring-logging.md) | Prometheus metrics catalog, Grafana dashboards, alerts, ELK pipeline |
| 10 | [Security Architecture](10-security-architecture.md) | JWT, bcrypt, TLS, Secrets, RBAC, NetworkPolicies, rate limiting, STRIDE threat model |
| 11 | [Load Testing Strategy](11-load-testing-strategy.md) | Traffic math for 1M req/day, k6 vs Locust vs JMeter, six test phases, bottleneck analysis |
| 12 | [Deployment Strategies](12-deployment-strategies.md) | Rolling update, blue-green, canary — YAML, diagrams, pros/cons |
| 13 | [Design Decisions & Future Work](13-design-decisions-and-future.md) | Architecture decision records, future enhancements, limitations |
| 14 | [Resume Content](14-resume-content.md) | Project description, ATS bullet points, LinkedIn summary |
| 15 | [Viva Questions & Answers](15-viva-questions.md) | 60+ questions with detailed answers |

## Where each documentation deliverable lives

| Deliverable | Chapter |
|---|---|
| Executive summary, problem statement, objectives, scope, technology stack | 1 |
| Architecture | 2, 3 |
| Design decisions | 13 |
| API documentation | 5 (live OpenAPI at `/docs`, `/redoc`, `/openapi.json`) |
| Database design | 4 |
| Docker setup | 6 |
| Kubernetes setup | 7 |
| CI/CD workflow | 8 |
| Monitoring setup | 9 |
| Load testing strategy | 11 |
| Security design | 10 |
| Future enhancements | 13 |

## Verification evidence

These checks were run on the finished project (Windows 11 laptop, Docker Desktop, 20 CPUs / 8 GB VM).

| Check | Result |
|---|---|
| Unit + integration tests (`pytest`) | 60 passed, 92 % coverage (Jenkins gate: 70 %); also passes inside the Linux image build (`--target test`) |
| Lint / format / SAST (`ruff`, `bandit -ll`) | clean |
| Jenkinsfile | validated by the Jenkins Declarative linter (`jenkins/Dockerfile` image) |
| Kubernetes manifests | `kubectl kustomize` renders `k8s/base`, `k8s/monitoring`, `k8s/logging`; all YAML parses; Prometheus rules pass `promtool` (not yet applied to a live cluster) |
| Concurrent start-up (real PostgreSQL 16 + Redis 7) | two replicas started at the same instant: schema created once (advisory lock), bootstrap admin created once |
| Docker Compose stack through NGINX (TLS) | `scripts/smoke_test.sh`: 6/6 checks pass against 2–3 replicas; HTTP→HTTPS redirect, HSTS, `/metrics` blocked (403), round-robin across replicas |
| k6 smoke (20 VUs, `AUTH_MODE=pool`, 2 min, through NGINX, 3 replicas) | 921 requests, **0 % errors, avg 41.9 ms, p95 48.8 ms**; chat p95 48 ms; 74 % response-cache hit ratio |
| Prometheus + Grafana | all 5 scrape targets up; scaling to 3 replicas discovered automatically; 123 dashboard queries return data (the empty ones need k6 remote-write or Kubernetes-only metrics) |
| ELK | API, user, system and error logs indexed in `scalebot-{type}-logs-*` with numeric `latency_ms`/`status_code` |
| Redis outage (`docker compose stop redis`) | smoke test still passes; `/health` = `degraded`, readiness 200 in 26 ms; chat latency **avg 73 ms** (≈ 2 s per request before the circuit breaker was added); automatic recovery |

Not yet measured: the full load-test phases of chapter 11 (1,000 to 50,000 VUs need distributed load
generators and a real cluster). The report template in chapter 11 is where those numbers belong; a laptop
smoke test does not demonstrate 1M requests/day.

## Conventions used in these documents

- Diagrams are Mermaid and render on GitHub and in VS Code (Markdown Preview Mermaid Support).
- Numbers marked "estimate" are design targets to be validated with the load tests in chapter 11.
- Paths are relative to the repository root.
