# 02 — Architecture Diagrams

This document contains the nine required architecture views of ScaleBotAI, preceded by a C4-style context view and followed by two request-level sequence diagrams. All diagrams are Mermaid and render on GitHub and in VS Code (Markdown Preview Mermaid Support).

| # | View | Section |
|---|---|---|
| 0 | System context (C4 level 1) | [§0](#0-system-context-c4-level-1) |
| 1 | Complete high-level architecture | [§1](#1-complete-high-level-architecture) |
| 2 | Detailed component diagram (inside the API) | [§2](#2-detailed-component-diagram-fastapi-service) |
| 3 | Deployment diagram (Docker Compose and Kubernetes) | [§3](#3-deployment-diagram) |
| 4 | Kubernetes architecture | [§4](#4-kubernetes-architecture-diagram) |
| 5 | CI/CD pipeline | [§5](#5-cicd-diagram) |
| 6 | Monitoring and logging | [§6](#6-monitoring-diagram) |
| 7 | Network | [§7](#7-network-diagram) |
| 8 | Database (ER) | [§8](#8-database-diagram) |
| 9 | Load-testing architecture | [§9](#9-load-testing-architecture-diagram) |
| 10 | Request lifecycle: `POST /api/v1/chat` | [§10](#10-request-lifecycle-post-apiv1chat) |
| 11 | Login sequence | [§11](#11-login-sequence) |

Conventions: solid arrows are request/data flow, dotted arrows are control, observation or optional paths, and cylinders are stateful stores. Port numbers match `docker-compose.yml` and the Kubernetes manifests.

---

## 0. System Context (C4 level 1)

```mermaid
flowchart TB
    user["End user<br/>chat client, Swagger UI, curl"]
    admin["Administrator<br/>role = admin"]
    dev["Developer<br/>commits code"]
    ops["Operator / evaluator<br/>dashboards, logs"]

    subgraph sys["ScaleBotAI platform"]
        api["ScaleBotAI API<br/>FastAPI, 32 REST endpoints<br/>rule engine or TinyLlama"]
    end

    gh["GitHub<br/>source and webhooks"]
    hub["Docker Hub<br/>image registry"]
    ca["Let's Encrypt<br/>ACME CA (production only)"]

    user -->|"HTTPS JSON + JWT"| api
    admin -->|"HTTPS JSON + admin JWT"| api
    ops -->|"Grafana, Kibana"| sys
    dev -->|"git push"| gh
    gh -->|"webhook triggers Jenkins"| sys
    sys -->|"push and pull images"| hub
    ca -->|"TLS certificates via cert-manager"| sys
```

**Key design points**
- The platform has two kinds of human users: API consumers (end users and admins) and operators. Operators use Grafana and Kibana, never the database directly.
- External dependencies are limited to GitHub, Docker Hub and, in production, an ACME CA. The runtime path has **no third-party SaaS dependency**, and the optional LLM runs in-cluster.

---

## 1. Complete High-Level Architecture

The high-level view has five planes: **edge** (DNS, TLS, load balancing), **application** (stateless API pods and their data stores), **monitoring** (metrics and alerting), **logging** (ELK) and **CI/CD** (build and release).

```mermaid
flowchart LR
    subgraph clients["Clients"]
        direction TB
        WEB["Web / mobile / CLI clients"]
        ADM["Admin users"]
        LGEN["Load generators<br/>k6, Locust, JMeter"]
    end

    DNS["DNS<br/>api.scalebotai.local"]

    subgraph edge["Edge layer"]
        direction TB
        LB["L4 load balancer / NodePort<br/>:80, :443"]
        ING["ingress-nginx (K8s) or NGINX (Compose)<br/>TLS 1.2/1.3 termination, HSTS<br/>limit_req per IP, /metrics denied"]
    end

    subgraph appplane["Application plane: namespace scalebotai"]
        direction TB
        SVC["Service scalebotai-api<br/>ClusterIP :80"]
        subgraph apipods["Deployment scalebotai-api, HPA 3 to 20 replicas"]
            P1["Pod 1<br/>Uvicorn + FastAPI :8000"]
            P2["Pod 2<br/>Uvicorn + FastAPI :8000"]
            PN["Pod N<br/>Uvicorn + FastAPI :8000"]
        end
        RD[("Redis 7 :6379<br/>cache, rate limit,<br/>JWT denylist, counters")]
        PG[("PostgreSQL 16 :5432<br/>StatefulSet + PVC")]
        OL["Ollama :11434<br/>TinyLlama, llm mode only"]
    end

    subgraph monplane["Monitoring plane: namespace monitoring"]
        PROM["Prometheus :9090<br/>scrape + alert rules"]
        GRAF["Grafana :3000<br/>dashboards"]
    end

    subgraph logplane["Logging plane: namespace logging"]
        FB["Filebeat DaemonSet"]
        LS["Logstash :5044"]
        ES[("Elasticsearch :9200")]
        KB["Kibana :5601"]
    end

    subgraph cicdplane["CI/CD plane"]
        GH["GitHub"]
        JEN["Jenkins pipeline"]
        HUB["Docker Hub"]
    end

    WEB --> DNS
    ADM --> DNS
    LGEN --> DNS
    DNS --> LB --> ING -->|"HTTP to endpoints :8000"| SVC
    SVC --> P1 & P2 & PN
    P1 & P2 & PN --> RD
    P1 & P2 & PN --> PG
    P1 & P2 & PN -.->|"llm mode"| OL

    PROM -.->|"scrape /metrics"| apipods
    GRAF --> PROM
    apipods -.->|"stdout JSON logs"| FB
    FB --> LS --> ES --> KB
    GRAF -.-> ES

    GH -->|"webhook"| JEN
    JEN -->|"docker push"| HUB
    JEN -->|"kubectl set image"| apipods
    HUB -.->|"image pull"| apipods
```

**Key design points**
- **Stateless compute, stateful stores.** API pods keep no session or cache state in memory. All shared state lives in PostgreSQL (durable) or Redis (ephemeral), so any pod can serve any request and the HPA can add or remove pods freely.
- **Single TLS termination point** at the edge. Traffic inside the cluster is plain HTTP on a private network protected by NetworkPolicies (see §7). A service mesh with mTLS is listed as future work.
- **`/metrics` is never public.** The Compose NGINX denies it explicitly, the Kubernetes Ingress does not route it (only `/api` and `/health`), and Prometheus scrapes pods directly on the cluster network.
- **Observability is out-of-band.** Metrics are pulled and logs are shipped from node log files, so a monitoring or logging outage does not affect request handling.
- **CI/CD changes as little as possible.** For the default rolling strategy, Jenkins only changes the Deployment's image (`kubectl set image`). Everything else is declarative (`kubectl apply -k k8s/base`), which keeps rollouts and rollbacks atomic and auditable. Canary and blue-green additionally manage the `track`/`color`-labelled Deployments and Service selector ([doc 12](12-deployment-strategies.md)).

---

## 2. Detailed Component Diagram (FastAPI service)

```mermaid
flowchart TB
    REQ["HTTP request from NGINX / Ingress<br/>X-Forwarded-For, optional X-Request-ID"]

    subgraph mw["Request pipeline, outer to inner"]
        direction LR
        M1["1. Request ID<br/>echo valid X-Request-ID or UUID4,<br/>contextvar for logs"]
        M2["2. Metrics<br/>in-progress gauge, duration histogram,<br/>requests_total by route template"]
        M3["3. Access logging<br/>one JSON line, log_type=api,<br/>daily counter in Redis"]
        MC["CORS middleware"]
        M4["4. Rate limit<br/>route dependency after JWT auth,<br/>Redis fixed window, 120 per min"]
        M1 --> M2 --> M3 --> MC --> M4
    end

    subgraph routers["Routers: src/app/api/routes"]
        R_H["health<br/>/health, /health/live, /health/ready"]
        R_MET["metrics<br/>/metrics"]
        R_A["auth<br/>/api/v1/auth/..."]
        R_U["users<br/>/api/v1/users/me..."]
        R_C["chat<br/>/api/v1/chat..."]
        R_B["bot<br/>/api/v1/bot/info"]
        R_AD["admin<br/>/api/v1/admin/..."]
    end

    subgraph depsec["Dependencies and security: api/deps.py, core/security.py"]
        D_AUTH["get_current_user<br/>JWT decode + denylist check,<br/>user cache, is_active"]
        D_ADMIN["require_admin<br/>role must be admin, else 403"]
        SEC["security<br/>PyJWT HS256, SHA-256 pre-hash +<br/>bcrypt cost 12 in threadpool"]
        D_DB["get_session<br/>one AsyncSession per request"]
    end

    SCH["Pydantic v2 schemas<br/>request validation, response models,<br/>error envelope"]

    subgraph svc["Services layer: src/app/services"]
        S_USER["user_service<br/>register, authenticate, tokens,<br/>profile, stats"]
        S_CHAT["chat_service<br/>conversations, chat, history,<br/>feedback, cache-aside"]
        S_ADMIN["analytics_service<br/>admin stats, health checks"]
        S_CACHE["cache + rate_limiter<br/>fail-open Redis helpers"]
    end

    subgraph engine["Chatbot engine, strategy pattern: src/app/chatbot"]
        FACT{{"build_bot factory<br/>CHATBOT_MODE"}}
        RULE["RuleBasedBot<br/>weighted keyword intents"]
        LLM["OllamaBot<br/>httpx to Ollama /api/chat,<br/>last 5 exchanges as context"]
        FALL["Fallback + circuit breaker<br/>timeout or error leads to rule-fallback"]
        FACT -->|"rule"| RULE
        FACT -->|"llm"| LLM
        LLM --> FALL
        FALL -.->|"on timeout or error"| RULE
    end

    subgraph dataacc["Data access: src/app/db"]
        ORMM["SQLAlchemy 2 models<br/>users, conversations, chats, feedback"]
        ORM["Async engine<br/>asyncpg pool 10 + overflow 20"]
        RCL["Redis asyncio client<br/>socket timeout 0.5 s"]
        ORMM --> ORM
    end

    subgraph cross["Cross-cutting: src/app/core"]
        CFG["Settings<br/>pydantic-settings from env"]
        PREG["Prometheus registry<br/>scalebot_* metrics"]
        JLOG["JSON logger<br/>stdout + optional LOG_FILE"]
        EXH["Exception handlers<br/>error envelope with request_id"]
    end

    PGX[("PostgreSQL :5432")]
    RDX[("Redis :6379")]
    OLX["Ollama :11434"]

    REQ --> M1
    M4 --> routers
    routers --> SCH
    routers --> depsec
    D_AUTH -.->|"user id"| M4
    R_A & R_U --> S_USER
    R_C --> S_CHAT
    R_B --> engine
    R_AD --> S_ADMIN
    R_H --> S_ADMIN
    R_MET --> PREG
    S_CHAT --> engine
    S_USER & S_CHAT & S_ADMIN --> ORMM
    S_USER & S_CHAT & S_ADMIN --> S_CACHE
    S_CACHE --> RCL
    D_AUTH --> S_CACHE
    M4 --> S_CACHE
    M3 --> S_CACHE
    ORM --> PGX
    RCL --> RDX
    LLM --> OLX
    M2 --> PREG
    M3 --> JLOG
    EXH --> JLOG
```

**Key design points**
- **Layered architecture.** Routers handle HTTP concerns only (status codes, schemas). Services hold the business rules. The ORM layer holds the SQL. Each layer can be unit-tested with fakes (SQLite via aiosqlite and fakeredis).
- **Pipeline order matters.** Stages 1–3 are implemented by one pure-ASGI middleware (`src/app/middleware/request_context.py`), which avoids per-middleware overhead.
  - It assigns the request ID first, so every later log line and error carries it.
  - It wraps everything else, so rejected (429) and failed (5xx) requests are measured and logged too.
  - `/metrics` and `/health*` are excluded from metrics and the daily counter.
- **Rate limiting runs right after authentication.** It is the last link before the handler and is implemented as a FastAPI dependency (`src/app/api/deps.py`) chained after `get_current_user`. It keys on `user:{id}` for authenticated routes and on `ip:{client_ip}` for anonymous ones (login, register). The client IP comes from `X-Forwarded-For`, which is trusted because Uvicorn runs with `--proxy-headers`.
- **Strategy pattern for the bot.** `CHATBOT_MODE` selects an engine at start-up. The LLM engine carries its own fallback and circuit breaker, so a slow or absent Ollama degrades answer quality but never availability.
- **Fail-open Redis.** Every Redis call is wrapped, and on error the request proceeds without cache, rate limit or denylist, and an error log is emitted (throttled). Availability is preferred over these protections (trade-off discussed in [doc 03](03-component-design.md) and [doc 10](10-security-architecture.md)).
- **CPU-bound work off the event loop.** bcrypt hashing and verification run in Starlette's threadpool, so a 250 ms hash does not stall other requests on the same pod.

---

## 3. Deployment Diagram

### 3a. Development: Docker Compose (single host)

```mermaid
flowchart TB
    DEVU["Developer browser, curl, k6"]

    subgraph host["Docker host: laptop or single VM"]
        subgraph edgegrp["Edge"]
            NGX["nginx<br/>host :80 and :443<br/>stub_status :8080 internal<br/>network: frontend"]
        end
        subgraph appgrp["Application"]
            API["chatbot-api x N replicas<br/>:8000, no host port<br/>networks: frontend, backend, monitoring"]
            PGC[("postgres :5432<br/>network: backend")]
            RDC[("redis :6379<br/>network: backend")]
            OLC["ollama :11434<br/>profile llm, network: backend"]
        end
        subgraph mongrp["Monitoring and logging"]
            PRC["prometheus<br/>127.0.0.1:9090"]
            GRC["grafana<br/>127.0.0.1:3000"]
            PGE["postgres-exporter :9187"]
            RDE["redis-exporter :9121"]
            NGE["nginx-exporter :9113<br/>networks: frontend, monitoring"]
            LSC["logstash :5044<br/>file input"]
            ESC[("elasticsearch<br/>127.0.0.1:9200")]
            KBC["kibana<br/>127.0.0.1:5601"]
        end
        subgraph volgrp["Named volumes"]
            V_PG[("postgres-data")]
            V_RD[("redis-data")]
            V_ES[("es-data")]
            V_PR[("prometheus-data")]
            V_GR[("grafana-data")]
            V_LOG[("app-logs")]
            V_OL[("ollama-data")]
        end
    end

    DEVU -->|"https://localhost"| NGX
    NGX -->|"upstream chatbot-api:8000"| API
    API --> PGC
    API --> RDC
    API -.->|"llm mode"| OLC
    PRC -.->|"dns_sd A records"| API
    PRC -.-> PGE & RDE & NGE
    PGE -.-> PGC
    RDE -.-> RDC
    NGE -.->|"nginx_status"| NGX
    GRC --> PRC
    GRC --> ESC
    API -->|"writes app.log"| V_LOG
    V_LOG -->|"reads app.log"| LSC
    LSC --> ESC --> KBC
    PGC --- V_PG
    RDC --- V_RD
    ESC --- V_ES
    PRC --- V_PR
    GRC --- V_GR
    OLC --- V_OL
```

**Key design points**
- `chatbot-api` has **no `container_name` and no host port**. That is what makes `docker compose up -d --scale chatbot-api=3` possible: Docker's embedded DNS returns one A record per replica, NGINX balances across them, and Prometheus discovers each one through `dns_sd_configs` (type A).
- Three user-defined bridge networks isolate tiers. Containers can only talk across a network they share. The database and Redis are reachable only on `backend` and never from the host. Operator UIs (Prometheus 9090, Grafana 3000, Kibana 5601) bind to `127.0.0.1` by default (`MONITORING_BIND_ADDR`), and Elasticsearch always binds to `127.0.0.1:9200` because its security is disabled for local development.
- Every service inherits `restart: unless-stopped` and `no-new-privileges`. The API container also runs with a read-only root filesystem and all capabilities dropped.
- Logs reach ELK through the shared `app-logs` volume: the API appends JSON lines to `/var/log/scalebotai/app.log`, and Logstash's `file` input tails them.

### 3b. Production: Kubernetes (multi-node)

```mermaid
flowchart TB
    INET["Internet clients"]
    HUBP["Docker Hub<br/>scalebotai/scalebotai-api:BUILD-sha"]

    subgraph cluster["Kubernetes cluster"]
        CLB["Cloud L4 load balancer<br/>:80, :443"]
        subgraph cp["Control plane"]
            KAPI["kube-apiserver"]
            ETCD[("etcd<br/>enable Secret encryption at rest")]
            CTRL["scheduler + controller-manager"]
        end
        subgraph node1["Worker node 1"]
            IC1["ingress-nginx controller pod"]
            A1["scalebotai-api pod<br/>container api :8000"]
            PG0["postgres-0 pod :5432"]
            FB1["filebeat pod"]
        end
        subgraph node2["Worker node 2"]
            IC2["ingress-nginx controller pod"]
            A2["scalebotai-api pod<br/>container api :8000"]
            RDP["redis pod :6379"]
            PRP["prometheus + grafana pods"]
            FB2["filebeat pod"]
        end
        subgraph node3["Worker node 3"]
            A3["scalebotai-api pod<br/>container api :8000"]
            OLP["ollama pod :11434<br/>optional"]
            ELKP["elasticsearch, logstash,<br/>kibana pods"]
            FB3["filebeat pod"]
        end
        subgraph stor["Persistent storage: cloud StorageClass, e.g. gp3 or pd-ssd"]
            PV_PG[("PV for PVC postgres-data")]
            PV_RD[("PV for PVC redis-data")]
            PV_OB[("PVs for Prometheus and<br/>Elasticsearch data")]
        end
    end

    INET -->|"HTTPS :443"| CLB
    CLB --> IC1 & IC2
    IC1 & IC2 -->|"HTTP :8000"| A1 & A2 & A3
    A1 & A2 & A3 --> PG0
    A1 & A2 & A3 --> RDP
    A1 & A2 & A3 -.-> OLP
    PG0 --- PV_PG
    RDP --- PV_RD
    PRP --- PV_OB
    ELKP --- PV_OB
    CTRL -.->|"schedules"| node1 & node2 & node3
    HUBP -.->|"kubelet image pull"| node1 & node2 & node3
```

**Key design points**
- Placement is decided by the scheduler. The diagram shows one valid outcome where **topology spread constraints** (`maxSkew: 1` across nodes) keep API replicas on different nodes, so losing a node removes at most one replica.
- Two ingress-nginx controller replicas sit behind the cloud load balancer, which removes the edge single point of failure.
- Stateful workloads (`postgres` StatefulSet, `redis`) use PersistentVolumeClaims `postgres-data` and `redis-data`. If a node fails, the pod is rescheduled and re-attaches its volume (subject to the storage class's zone constraints).
- Every node pulls the same immutable image tag `<BUILD_NUMBER>-<git short sha>`, which is what makes `kubectl rollout undo` deterministic.

---

## 4. Kubernetes Architecture Diagram

```mermaid
flowchart TB
    CLIENT["Client HTTPS :443"]

    subgraph ns_ing["namespace ingress-nginx"]
        ICTL["ingress-nginx controller<br/>ingressClassName nginx"]
    end

    subgraph ns_sys["namespace kube-system"]
        MSRV["metrics-server<br/>metrics.k8s.io API"]
        KDNS["CoreDNS :53"]
    end

    subgraph ns_app["namespace scalebotai"]
        INGR["Ingress scalebotai-ingress<br/>host api.scalebotai.local<br/>TLS secret scalebotai-tls"]
        SVCA["Service scalebotai-api<br/>ClusterIP :80 to targetPort http"]
        DEPA["Deployment scalebotai-api<br/>RollingUpdate maxSurge 25 percent,<br/>maxUnavailable 0"]
        RSA["ReplicaSet<br/>current pod-template-hash"]
        PODA1["Pod api :8000"]
        PODA2["Pod api :8000"]
        PODA3["Pod api :8000"]
        HPAA["HPA scalebotai-api<br/>autoscaling/v2, min 3, max 20<br/>CPU 70 percent, memory 75 percent"]
        PDBA["PodDisruptionBudget scalebotai-api<br/>minAvailable 2"]
        CMA["ConfigMap scalebotai-config"]
        SECA["Secret scalebotai-secrets"]
        SAA["ServiceAccount scalebotai-api<br/>automountServiceAccountToken false"]
        STSP["StatefulSet postgres<br/>replicas 1"]
        PODP["Pod postgres-0"]
        SVCP["Service postgres :5432"]
        PVCP["PVC postgres-data"]
        DEPR["Deployment redis"]
        PODR["Pod redis"]
        SVCR["Service redis :6379"]
        PVCR["PVC redis-data"]
        OLLA["Deployment + Service ollama :11434<br/>optional, llm mode"]
        NPOL["NetworkPolicies<br/>default deny + allow-list"]
        JSA["ServiceAccount jenkins-deployer<br/>Role + RoleBinding, namespace scoped"]
    end

    subgraph ns_mon["namespace monitoring"]
        PROMK["Prometheus<br/>kubernetes_sd, pod annotations"]
        GRAFK["Grafana"]
    end

    subgraph ns_log["namespace logging"]
        FBK["Filebeat DaemonSet"]
        LSK["Logstash :5044"]
        ESK[("Elasticsearch :9200")]
        KBK["Kibana :5601"]
    end

    PVP[("PersistentVolume<br/>static in lab, StorageClass in cloud")]
    PVR[("PersistentVolume<br/>static in lab, StorageClass in cloud")]

    CLIENT --> ICTL
    ICTL -.->|"watches"| INGR
    ICTL -->|"routes to Service endpoints"| SVCA
    SVCA --> PODA1 & PODA2 & PODA3
    DEPA -->|"owns"| RSA
    RSA -->|"owns"| PODA1 & PODA2 & PODA3
    HPAA -->|"scales replicas"| DEPA
    MSRV -->|"CPU and memory usage"| HPAA
    PDBA -.->|"guards voluntary evictions"| PODA1
    CMA -.->|"envFrom"| PODA1
    SECA -.->|"envFrom"| PODA1
    SAA -.->|"identity"| PODA1
    STSP -->|"owns"| PODP
    SVCP --> PODP
    PODP --> PVCP --> PVP
    DEPR -->|"owns"| PODR
    SVCR --> PODR
    PODR --> PVCR --> PVR
    PODA1 & PODA2 & PODA3 --> SVCP
    PODA1 & PODA2 & PODA3 --> SVCR
    PODA1 -.-> OLLA
    PODA1 -.->|"DNS"| KDNS
    NPOL -.->|"restricts traffic"| PODP
    JSA -.->|"set image, rollout status, undo"| DEPA
    PROMK -.->|"scrape :8000/metrics"| PODA2
    GRAFK --> PROMK
    FBK --> LSK --> ESK --> KBK
```

**Key design points**
- **Ownership chain.** The Deployment owns ReplicaSets, which own Pods. A rolling update creates a new ReplicaSet and scales the old one down. The old ReplicaSet is kept (`revisionHistoryLimit`), which is what `kubectl rollout undo` scales back up.
- **HPA ↔ metrics-server.** metrics-server aggregates kubelet/cAdvisor usage every ~15 s and exposes `metrics.k8s.io`. The HPA control loop compares usage against *requests* (250m CPU, 256Mi memory) and sets `spec.replicas` between 3 and 20. Prometheus is **not** in the autoscaling path, so a monitoring outage cannot break scaling.
- **ingress-nginx routes to endpoints directly.** The controller reads the Service's EndpointSlices and load-balances to pod IPs on port 8000, bypassing kube-proxy. The ClusterIP Service (port 80) is used by in-cluster clients and smoke tests.
- **Configuration split.** Non-secret settings come from ConfigMap `scalebotai-config` and secrets from Secret `scalebotai-secrets`, both injected as environment variables (12-factor).
- **Least privilege.** The API ServiceAccount has no token mounted, because the app never calls the Kubernetes API. Jenkins uses the separate `jenkins-deployer` ServiceAccount, limited by a namespace-scoped Role.
- **Disruption safety.** The PDB (`minAvailable: 2`) stops `kubectl drain` and cluster upgrades from evicting API pods below two. Rolling updates are not governed by PDBs; the RollingUpdate setting `maxUnavailable: 0` gives the equivalent guarantee during deploys.
- **Pod Security Admission.** Namespace `scalebotai` is labelled `pod-security.kubernetes.io/enforce: restricted`, so the API server rejects any pod that runs as root, keeps capabilities or lacks a seccomp profile.

---

## 5. CI/CD Diagram

```mermaid
flowchart LR
    DEVR["Developer"]
    GHR["GitHub repository<br/>main branch"]

    subgraph jenkins["Jenkins declarative pipeline: jenkins/Jenkinsfile"]
        direction LR
        S1["1 Checkout<br/>git SHA, BUILD_NUMBER"]
        S2["2 Build<br/>venv + requirements-dev"]
        S3["3 Unit Test<br/>pytest, JUnit + coverage"]
        S4["4 Lint, parallel<br/>ruff check, ruff format --check,<br/>bandit SAST"]
        S5["5 Docker Build<br/>tag BUILD_NUMBER-sha"]
        S6["6 Security Scan, Trivy<br/>report HIGH and CRITICAL,<br/>block secrets and fixable CRITICAL"]
        S7["7 Docker Push, main and develop<br/>dockerhub-credentials"]
        S8{{"8 Kubernetes Deploy, main only<br/>DEPLOY_STRATEGY: rolling, canary, blue-green<br/>kubeconfig-scalebotai"}}
        S9["9 Smoke Test<br/>scripts/smoke_test.sh BASE_URL"]
        S1 --> S2 --> S3 --> S4 --> S5 --> S6 --> S7 --> S8 --> S9
    end

    HUBR[("Docker Hub<br/>scalebotai/scalebotai-api")]
    K8SR["Kubernetes<br/>namespace scalebotai"]
    FAILB["Pipeline FAILED<br/>nothing deployed"]
    RBK["10 Rollback<br/>scripts/rollback.sh,<br/>kubectl rollout undo"]
    DONE["Release complete<br/>image tag recorded"]

    DEVR -->|"git push / PR merge"| GHR
    GHR -->|"webhook"| S1
    S3 -.->|"tests fail"| FAILB
    S4 -.->|"lint or SAST fails"| FAILB
    S6 -.->|"secret or CVE gate fails"| FAILB
    S7 -->|"docker push"| HUBR
    S8 -->|"kubectl set image + rollout status 300s"| K8SR
    HUBR -.->|"image pull"| K8SR
    S8 -.->|"rollout timeout"| RBK
    S9 -->|"pass"| DONE
    S9 -.->|"fail"| RBK
    RBK -->|"previous ReplicaSet restored"| K8SR
```

| Stage | What it proves | Fails the build when | Typical duration *(estimate)* |
|---|---|---|---|
| Checkout | Exact commit is built | — | < 10 s |
| Build | Dependencies resolve on Python 3.12 | pip resolution error | 30–60 s (cached) |
| Unit Test | Business logic correct (SQLite + fakeredis, no services) | any test fails | 30–90 s |
| Lint (parallel) | Style (`ruff check`, `ruff format --check`) and static security analysis (`bandit`) | any ruff violation, or a Bandit finding of MEDIUM+ severity | < 30 s |
| Docker Build | Multi-stage image builds reproducibly | build error | 1–3 min (layer cache) |
| Security Scan | No secrets committed to the repo. No *fixable* CRITICAL CVEs in the image. HIGH/CRITICAL report and IaC misconfiguration report archived | Trivy secret scan or `--severity CRITICAL --ignore-unfixed` image scan exits ≠ 0 | 30–90 s |
| Docker Push (`main`, `develop`) | Image available to the cluster | registry auth/push error | 20–60 s |
| Kubernetes Deploy (`main` only) | New pods pass readiness, old pods drained | `rollout status` timeout (300 s) | 1–3 min |
| Smoke Test | Real user journey works on the live endpoint | any check non-zero | 10–30 s |
| Rollback (post-failure) | Service restored to last good revision | — | < 2 min |

**Key design points**
- **Fail fast, cheapest checks first.** Tests, lint and SAST run before the slow image build. The image is scanned *before* it is pushed, so an image with a fixable CRITICAL CVE never reaches the registry. HIGH findings are reported for review rather than blocking, a deliberate trade-off so that unfixable upstream CVEs do not stall delivery.
- **Immutable, traceable artefacts.** The tag `<BUILD_NUMBER>-<git short sha>` links a running pod to its Jenkins build and Git commit. `latest` is pushed only from `main` as a convenience (the base manifest's initial image). Every pipeline deployment pins the exact tag with `kubectl set image`.
- **Automated rollback** (`scripts/rollback.sh`) is triggered by both a rollout timeout and a failed smoke test. A `post { failure }` safety net rolls back if the pipeline dies mid-deploy. This bounds the mean time to recover to a few minutes.
- **Credentials never appear in the Jenkinsfile.** `dockerhub-credentials` (username/password) and `kubeconfig-scalebotai` (secret file) are injected through credentials binding and masked in the logs.
- `SKIP_DEPLOY=true` turns the pipeline into pure CI, for example on feature branches.

---

## 6. Monitoring Diagram

```mermaid
flowchart LR
    subgraph targets["Scrape targets"]
        PODS["scalebotai-api pods :8000/metrics<br/>K8s: kubernetes_sd + prometheus.io annotations<br/>Compose: dns_sd type A on chatbot-api"]
        PGEX["postgres-exporter :9187<br/>pg_*"]
        RDEX["redis-exporter :9121<br/>redis_*"]
        NGEX["nginx-exporter :9113<br/>nginx_*"]
        CADV["kubelet / cAdvisor<br/>container CPU, memory, restarts"]
        KSMX["kube-state-metrics<br/>replicas, HPA status, pod phases"]
    end

    subgraph promsrv["Prometheus :9090"]
        SCRP["Scraper<br/>every 15 s"]
        TSDB[("TSDB<br/>prometheus-data")]
        RULESET["Recording + alert rules<br/>alert-rules.yml"]
        SCRP --> TSDB --> RULESET
    end

    ALRT["Alerts page / Grafana alerting<br/>Alertmanager optional"]

    subgraph grafsrv["Grafana :3000"]
        GDS["Provisioned datasources<br/>uid prometheus, uid elasticsearch"]
        GD1["API overview<br/>RPS, latency, errors, cache"]
        GD2["Infrastructure<br/>pods, CPU, memory, DB, Redis"]
        GD3["Load-test war room"]
    end

    subgraph elkstack["Logging pipeline"]
        SRC1["K8s: container stdout<br/>/var/log/containers"]
        SRC2["Compose: app-logs volume<br/>/var/log/scalebotai/app.log"]
        FBT["Filebeat DaemonSet"]
        LST["Logstash<br/>inputs: beats 5044 and file"]
        EST[("Elasticsearch<br/>scalebot-log_type-logs-YYYY.MM.dd")]
        KBN["Kibana :5601<br/>search by request_id, user_id"]
        SRC1 --> FBT --> LST
        SRC2 --> LST
        LST --> EST --> KBN
    end

    MSV["metrics-server<br/>HPA path only"]

    PODS & PGEX & RDEX & NGEX & CADV & KSMX --> SCRP
    RULESET -.-> ALRT
    TSDB --> GDS
    EST --> GDS
    GDS --> GD1 & GD2 & GD3
    CADV -.-> MSV
```

| Signal | Source | Example use |
|---|---|---|
| `scalebot_http_requests_total{method,route,status}` | API | RPS, error ratio, availability SLI |
| `scalebot_http_request_duration_seconds` (histogram) | API | Average and p50/p95/p99 latency against the 300 ms objective |
| `scalebot_http_requests_in_progress` | API | Concurrency (100–500 target) |
| `scalebot_chat_messages_total{bot_mode,cached}`, `scalebot_bot_response_seconds{bot_mode}` | API | Rule vs LLM comparison, fallback rate |
| `scalebot_cache_hits_total` / `scalebot_cache_misses_total{cache}` | API | Hit ratio per cache (`bot_response`, `user`, `analytics`) |
| `scalebot_rate_limited_total`, `scalebot_auth_events_total{event}` | API | Abuse and credential-stuffing detection |
| `scalebot_db_pool_checked_out` / `scalebot_db_pool_size` | API | Connection-pool saturation |
| `pg_*`, `redis_*`, `nginx_*` | Exporters | Database connections, Redis memory, NGINX active connections |
| `kube_deployment_*`, `kube_horizontalpodautoscaler_*` | kube-state-metrics (K8s) | Replica counts and HPA decisions during load tests |
| JSON logs (`log_type` = api, user, error, system) | API → ELK | Per-request forensics by `request_id`, audit of user events |

**Key design points**
- **Pull model.** Prometheus discovers targets dynamically (pod annotations in K8s, DNS A records in Compose), so newly scaled replicas are scraped automatically.
- **Recording rules** pre-compute the SLO queries (RPS, error ratio, p95, average latency), and alert rules are written against those recordings, for example `HighErrorRate` (> 1 %), `HighAvgLatency` / `HighLatencyP95` (> 300 ms), `ScaleBotAPIDown`, `DBPoolSaturation`, `RedisDown`, `PostgresDown`, `LowCacheHitRatio`.
- **Metrics and logs are joined by `request_id`.** Every error envelope and log line carries it, so a Grafana spike can be traced to individual requests in Kibana.
- **Separate indices per `log_type`** (`scalebot-api-logs-*`, `scalebot-user-logs-*`, `scalebot-error-logs-*`, `scalebot-system-logs-*`) allow different retention periods, for example a longer one for audit (`user`).

---

## 7. Network Diagram

### 7a. Kubernetes traffic path and ports

```mermaid
flowchart LR
    INETC["Internet client"]

    subgraph edgez["Edge zone"]
        LBZ["L4 load balancer<br/>:80 and :443"]
        TLSZ["ingress-nginx<br/>:443 TLS TERMINATES HERE<br/>:80 redirects to HTTPS"]
    end

    subgraph appz["namespace scalebotai, private pod network"]
        CIPZ["Service scalebotai-api<br/>ClusterIP :80"]
        PODZ["API pods<br/>:8000 plain HTTP"]
        PGZ[("postgres :5432")]
        RDZ[("redis :6379<br/>requirepass")]
        OLZ["ollama :11434"]
    end

    subgraph sysz["namespace kube-system"]
        DNSZ["CoreDNS :53"]
    end

    subgraph monz["namespace monitoring"]
        PRMZ["Prometheus"]
    end

    INETC -->|"HTTPS :443"| LBZ --> TLSZ
    TLSZ -->|"HTTP, X-Forwarded-For"| CIPZ
    CIPZ -->|"targetPort http 8000"| PODZ
    PODZ -->|"TCP 5432"| PGZ
    PODZ -->|"TCP 6379"| RDZ
    PODZ -.->|"HTTP 11434, llm mode"| OLZ
    PODZ -->|"UDP and TCP 53"| DNSZ
    PRMZ -.->|"HTTP 8000 /metrics"| PODZ
```

### 7b. Docker Compose networks

```mermaid
flowchart LR
    NETF{{"frontend network"}}
    NETB{{"backend network"}}
    NETM{{"monitoring network"}}

    C_NGX["nginx<br/>host 80, 443"]
    C_API["chatbot-api x N<br/>:8000"]
    C_PG[("postgres :5432")]
    C_RD[("redis :6379")]
    C_OL["ollama :11434"]
    C_PGE["postgres-exporter :9187"]
    C_RDE["redis-exporter :9121"]
    C_NGE["nginx-exporter :9113<br/>networks: frontend, monitoring"]
    C_PR["prometheus 127.0.0.1:9090"]
    C_GR["grafana 127.0.0.1:3000"]
    C_ELK["elasticsearch, logstash,<br/>kibana 127.0.0.1:5601"]

    C_NGX --- NETF
    C_API --- NETF
    C_API --- NETB
    C_API --- NETM
    C_PG --- NETB
    C_RD --- NETB
    C_OL --- NETB
    C_PGE --- NETB
    C_PGE --- NETM
    C_RDE --- NETB
    C_RDE --- NETM
    C_NGE --- NETF
    C_NGE --- NETM
    C_PR --- NETM
    C_GR --- NETM
    C_ELK --- NETM
```

Network membership matches `docker-compose.yml`. `nginx-exporter` joins `frontend` to read `nginx:8080/nginx_status` and `monitoring` to be scraped, so NGINX itself never needs to be on the monitoring network. The exporters bridge `backend` and `monitoring` in the same way. Details are in [doc 06](06-docker-implementation.md).

### 7c. NetworkPolicy allow-list (namespace `scalebotai`, file `k8s/base/12-network-policies.yaml`)

The baseline is **`default-deny-all`**, which denies **both ingress and egress** for every pod in the namespace. Additive allow rules then open exactly the flows the architecture needs:

| Policy | From | To | Port | Why |
|---|---|---|---|---|
| `allow-dns-egress` | Every pod in `scalebotai` | CoreDNS (`k8s-app: kube-dns`, namespace `kube-system`) | UDP/TCP 53 | Name resolution |
| `allow-api-ingress` | Namespace `ingress-nginx` | `app: scalebotai-api` | TCP 8000 | Public API traffic |
| `allow-api-ingress` | Namespace `monitoring` | `app: scalebotai-api` | TCP 8000 | Prometheus `/metrics` scrape |
| `allow-api-egress` | `app: scalebotai-api` | `app: postgres` / `app: redis` / `app: ollama` | TCP 5432 / 6379 / 11434 | SQL, cache and limiter, LLM |
| `allow-postgres-ingress` | `app: scalebotai-api`, `app: postgres-exporter` | `app: postgres` | TCP 5432 | Queries, database metrics |
| `allow-redis-ingress` | `app: scalebotai-api`, `app: redis-exporter` | `app: redis` | TCP 6379 | Cache, Redis metrics |
| `allow-ollama-ingress` | `app: scalebotai-api` | `app: ollama` | TCP 11434 | LLM mode only |
| (implicit) | Anything else | Any pod | — | **Denied** |

Rules for exporter scraping and the Ollama model download sit next to those optional workloads in `k8s/optional/`. Kubelet health probes come from the node and are always allowed.

**Key design points**
- **TLS termination point:** NGINX (Compose) or ingress-nginx (Kubernetes). HSTS tells browsers never to downgrade. Internal hops are on the private pod network, isolated by NetworkPolicies.
- **Only the edge is exposed.** In Compose, the host publishes 80/443 for NGINX. The operator UIs (3000, 9090, 5601) and Elasticsearch (9200) bind to loopback. PostgreSQL, Redis and the API publish no host ports. In Kubernetes, the Ingress routes only the `/api` and `/health` path prefixes, so `/metrics` and `/docs` are not reachable from outside.
- **Defence against lateral movement.** A compromised pod cannot open a connection that no allow rule permits. Because egress is denied by default, a compromised API pod cannot reach the internet or other namespaces either. NetworkPolicies need a CNI that enforces them (Calico, Cilium; e.g. `minikube start --cni=calico`), and minikube's default CNI silently ignores them.

---

## 8. Database Diagram

```mermaid
erDiagram
    USERS ||--o{ CONVERSATIONS : "owns"
    USERS ||--o{ CHATS : "asks"
    CONVERSATIONS ||--o{ CHATS : "contains"
    CHATS ||--o| FEEDBACK : "is rated by"

    USERS {
        UUID id PK "primary key"
        VARCHAR(50) username UK "NOT NULL, uq_users_username"
        VARCHAR(255) email UK "NOT NULL, stored lowercase, uq_users_email"
        VARCHAR(255) password_hash "NOT NULL, bcrypt"
        VARCHAR(100) full_name "NULL"
        VARCHAR(20) role "NOT NULL DEFAULT user, CHECK user or admin"
        BOOLEAN is_active "NOT NULL DEFAULT true"
        TIMESTAMPTZ created_at "NOT NULL DEFAULT now(), ix_users_created_at"
        TIMESTAMPTZ updated_at "NOT NULL DEFAULT now()"
        TIMESTAMPTZ last_login_at "NULL"
    }

    CONVERSATIONS {
        UUID id PK "primary key"
        UUID user_id FK "NOT NULL, users.id ON DELETE CASCADE"
        VARCHAR(200) title "NOT NULL DEFAULT New chat"
        TIMESTAMPTZ created_at "NOT NULL DEFAULT now()"
        TIMESTAMPTZ updated_at "NOT NULL, ix_conversations_user_updated (user_id, updated_at DESC)"
    }

    CHATS {
        BIGINT id PK "GENERATED AS IDENTITY"
        UUID user_id FK "NOT NULL, users.id ON DELETE CASCADE"
        UUID conversation_id FK "NOT NULL, conversations.id ON DELETE CASCADE"
        TEXT question "NOT NULL"
        TEXT response "NOT NULL"
        VARCHAR(20) bot_mode "NOT NULL DEFAULT rule"
        DOUBLE_PRECISION latency_ms "NULL"
        TIMESTAMPTZ timestamp "NOT NULL DEFAULT now()"
    }

    FEEDBACK {
        BIGINT id PK "GENERATED AS IDENTITY"
        BIGINT chat_id FK, UK "NOT NULL, chats.id ON DELETE CASCADE, uq_feedback_chat_id"
        SMALLINT rating "NOT NULL, CHECK BETWEEN 1 AND 5"
        VARCHAR(1000) comment "NULL"
        TIMESTAMPTZ created_at "NOT NULL DEFAULT now()"
    }
```

| Table | Indexes (besides PK) | Serves |
|---|---|---|
| users | `uq_users_username`, `uq_users_email`, `ix_users_created_at` | Login by username/email, "new users today/7d" |
| conversations | `ix_conversations_user_updated (user_id, updated_at DESC)` | "My conversations, most recent first" |
| chats | `ix_chats_user_timestamp (user_id, timestamp DESC)`, `ix_chats_conversation_timestamp (conversation_id, timestamp)`, `ix_chats_timestamp (timestamp)` | History pages, conversation replay, "chats today" |
| feedback | `uq_feedback_chat_id` (unique) | One rating per answer, upsert |

**Key design points**
- `chats.user_id` is deliberately **denormalised** (it is derivable from the conversation). It lets history and per-user stats use a single index range scan without a join.
- **Cascades** implement "delete my account" and "delete conversation" in one statement and keep referential integrity at the database level.
- A `UNIQUE` foreign key in `feedback` turns the 1:N relationship into **1:0..1**, which is how the rating upsert is enforced.
- UUID keys for users and conversations stop ID enumeration in URLs. `BIGINT` identity for the high-volume `chats` table keeps its indexes compact.
- Redis keys complement the schema: `bot:resp:{mode}:{sha256}`, `user:{id}`, `analytics:summary`, `ratelimit:{user_or_ip}:{epoch_minute}`, `jwt:denylist:{jti}`, `stats:requests:{YYYY-MM-DD}`. Full detail is in [doc 04](04-database-design.md).

---

## 9. Load Testing Architecture Diagram

```mermaid
flowchart LR
    subgraph gens["Load generators"]
        K6L["k6 CLI<br/>tests/load/k6/<br/>smoke, baseline, load"]
        subgraph k6op["k6-operator, distributed in-cluster"]
            TRUN["TestRun custom resource<br/>parallelism N"]
            KR1["k6 runner pod 1"]
            KR2["k6 runner pod 2"]
            KRN["k6 runner pod N"]
            TRUN --> KR1 & KR2 & KRN
        end
        LOCU["Locust master + workers<br/>tests/load/locust/locustfile.py"]
        JMTR["JMeter non-GUI<br/>tests/load/jmeter/scalebotai-load-test.jmx"]
    end

    subgraph sutz["System under test"]
        INZ["Ingress / NGINX :443<br/>api.scalebotai.local"]
        APIZ["scalebotai-api pods<br/>HPA 3 to 20"]
        PGLT[("PostgreSQL")]
        RDLT[("Redis")]
        INZ --> APIZ
        APIZ --> PGLT
        APIZ --> RDLT
    end

    subgraph obsz["Observation"]
        PRMLT["Prometheus<br/>remote-write receiver enabled"]
        GRFLT["Grafana war-room dashboard<br/>k6 VUs, RPS, p95, errors,<br/>HPA replicas, CPU, DB pool"]
        RPTS["k6 summary JSON / HTML,<br/>Locust CSV, JMeter report"]
    end

    K6L & KR1 & KR2 & KRN & LOCU & JMTR -->|"HTTPS: register, login,<br/>POST /api/v1/chat, GET /api/v1/chat/history"| INZ
    K6L & KR1 & KR2 & KRN -.->|"k6 -o experimental-prometheus-rw"| PRMLT
    APIZ -.->|"scraped /metrics"| PRMLT
    PRMLT --> GRFLT
    K6L & LOCU & JMTR --> RPTS
```

| Phase | Virtual users | Duration *(indicative)* | Purpose | Generator |
|---|---|---|---|---|
| Smoke | 100 | 1–2 min | Scripts and environment work | k6 CLI |
| Baseline | 1,000 | 10 min | Reference latency at 3 replicas | k6 CLI |
| Load | 10,000 | 30 min | Peak-day traffic; HPA scale-out | k6 CLI or k6-operator |
| Stress | 25,000 | 20 min | Find the breaking point and first bottleneck | k6-operator |
| Spike | 50,000 | sudden ramp | Scale-up speed, error rate during the ramp | k6-operator |
| Soak | steady peak load | 24 h | Memory leaks, connection exhaustion, 99.9 % | k6-operator |

**Key design points**
- **Generators run outside the pods under test.** Generators that compete for the same CPU distort results, so the k6-operator runners go on a separate node pool or in a separate cluster.
- **Distributed generation** (k6-operator `parallelism`) is needed above roughly 10–20k VUs per machine *(estimate)*, and is how the 25k and 50k phases are reached.
- **One pane of glass.** k6 pushes its client-side metrics to Prometheus with remote write. Prometheus must be started with `--web.enable-remote-write-receiver`. The war-room dashboard then plots client-observed latency next to server-side `scalebot_http_request_duration_seconds` and HPA replica counts.
- **Rate limits must be adjusted for load tests.** A test from few source IPs or few users would hit `RATE_LIMIT_PER_MINUTE=120`. Raise it for the test window, or create many users and distribute source IPs. Full detail is in [doc 11](11-load-testing-strategy.md).
- Success criteria: average < 300 ms, error rate < 1 %, availability 99.9 %, CPU < 80 %, memory < 80 %.

---

## 10. Request Lifecycle: `POST /api/v1/chat`

```mermaid
sequenceDiagram
    autonumber
    actor C as Client
    participant N as NGINX or Ingress
    participant MW as Middleware chain
    participant AU as Auth dependency
    participant RL as Rate limiter
    participant CS as Chat service
    participant R as Redis
    participant P as PostgreSQL
    participant B as Bot engine
    participant O as Ollama

    C->>N: POST /api/v1/chat over HTTPS with Bearer access token
    N->>N: TLS termination, limit_req per client IP
    N->>MW: HTTP to pod port 8000 with X-Forwarded-For
    MW->>MW: set X-Request-ID, start timer, in_progress gauge +1
    MW->>AU: route dependencies run
    AU->>AU: verify HS256 signature, exp, iss, type=access
    AU->>R: EXISTS jwt:denylist:{jti}
    R-->>AU: 0, token not revoked
    AU->>R: GET user:{id}
    alt profile cache miss
        AU->>P: SELECT user by id
        P-->>AU: user row, must be active
        AU->>R: SET user:{id} EX 300
    end
    AU->>RL: authenticated user id
    RL->>R: INCR ratelimit:user:{id}:{epoch_minute} then EXPIRE 60
    alt count above RATE_LIMIT_PER_MINUTE
        RL-->>C: 429 RATE_LIMITED with Retry-After
    else within limit
        RL->>CS: ChatRequest validated by Pydantic
        alt conversation_id supplied
            CS->>P: SELECT conversation WHERE id and user_id match
            P-->>CS: conversation, or 404 NOT_FOUND
        else no conversation_id
            CS->>P: INSERT new conversation
        end
        CS->>R: GET bot:resp:{mode}:{sha256 of normalized question}
        alt cache hit
            R-->>CS: cached answer, cached=true
        else cache miss
            CS->>B: generate answer
            opt CHATBOT_MODE is llm
                B->>P: load last 5 exchanges of the conversation
                B->>O: POST /api/chat with model tinyllama and timeout
                O-->>B: answer, or timeout or error
                Note over B: on timeout or error the rule engine answers<br/>and bot_mode becomes rule-fallback
            end
            B-->>CS: answer and bot_mode
            CS->>R: SET bot:resp:{mode}:{hash} EX CACHE_TTL_SECONDS
        end
        CS->>P: INSERT chat row, touch conversation.updated_at, COMMIT
        P-->>CS: chat_id
        CS-->>MW: ChatResponse with latency_ms
        MW->>R: INCR stats:requests:{YYYY-MM-DD}
        MW->>MW: observe histogram, requests_total, chat_messages_total, JSON access log
        MW-->>N: 200 with X-Request-ID, X-Process-Time-ms, X-RateLimit headers
        N-->>C: 200 ChatResponse
    end
```

**Key design points**
- **Cheapest rejection first.** NGINX drops floods before they reach Python. The JWT signature is checked locally (no I/O) before any Redis or database call.
- **Latency budget (rule mode, cache hit, estimate):** TLS and proxying ~1–3 ms, JWT ~0.1 ms, 3–4 Redis round trips ~0.3 ms each, one conversation `SELECT` plus one `INSERT … COMMIT` ~2–6 ms. That is **~5–15 ms server time**, well under the 300 ms objective. LLM mode adds 0.5–5 s on CPU, which is why it is not the default.
- **The cache stores the expensive part, not the write.** Every exchange is still persisted (history is a feature). The cache removes bot computation, which matters most for the LLM.
- **Every hop is observable.** The same `request_id` appears in the response header, the access log, any error envelope and Kibana.

---

## 11. Login Sequence

```mermaid
sequenceDiagram
    autonumber
    actor C as Client
    participant N as NGINX or Ingress
    participant A as Auth router and user_service
    participant R as Redis
    participant P as PostgreSQL
    participant T as Threadpool

    C->>N: POST /api/v1/auth/login with username or email and password
    N->>N: auth_per_ip zone, 30 requests per minute per IP
    N->>A: forward over HTTP, TLS already terminated
    A->>A: validate LoginRequest with Pydantic
    A->>R: INCR ratelimit:ip:{client_ip}:{epoch_minute}
    A->>P: SELECT user WHERE username matches OR email matches lowercased input
    alt no such user
        A->>T: bcrypt verify against a dummy hash, same cost as a real check
        T-->>A: always false, about 250 ms
        A->>A: auth_events_total login_failure +1, user log
        A-->>C: 401 UNAUTHORIZED, Invalid username or password
    else user found
        A->>T: bcrypt verify of SHA-256 pre-hashed password, cost 12
        T-->>A: match or no match, about 250 ms off the event loop
        alt wrong password
            A->>A: auth_events_total login_failure +1, user log without secrets
            A-->>C: 401 UNAUTHORIZED, same generic message
        else correct password but account disabled
            A-->>C: 403 FORBIDDEN, Account is disabled
        else success
            A->>P: UPDATE users SET last_login_at = now()
            A->>A: sign access JWT 30 min and refresh JWT 7 days, each with new jti
            A->>A: auth_events_total login_success +1, user log
            A-->>C: 200 TokenResponse with access_token, refresh_token, expires_in 1800, user
        end
    end
```

**Key design points**
- **No user enumeration through login.** The body is the same for "no such user" and "wrong password", and the timing is equalised by verifying against a dummy bcrypt hash, so neither reveals which usernames exist. The disabled-account status (403) is returned only *after* a correct password.
- **bcrypt runs in the threadpool.** One login costs ~250 ms of CPU *(estimate)* but does not block the event loop, so chat requests on the same pod are unaffected. Login is still the most CPU-expensive endpoint and the prime target for credential stuffing. Both rate-limit layers (NGINX `auth_per_ip` and Redis per IP) and the `login_failure` counter address this; see [doc 10](10-security-architecture.md).
- **Stateless verification, stateful revocation.** Tokens are verified with the shared secret alone, so any pod can verify any token. Revocation (logout, refresh rotation) is recorded in Redis under `jwt:denylist:{jti}` with a TTL equal to the token's remaining lifetime, so the denylist cleans itself up.
