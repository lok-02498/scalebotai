"""Option 1: rule-based DevOps/cloud assistant.

Intent matching is keyword based. Each intent lists keyword phrases; a phrase
that matches as whole words scores its word count, so the more specific
phrase wins ("docker compose" beats "docker"). Replies are chosen
deterministically from the normalized message, which keeps them cacheable.
"""

import hashlib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.chatbot.base import BotEngine, BotReply, ChatTurn


def normalize(text: str) -> str:
    text = text.lower().replace("ci/cd", "cicd")
    text = re.sub(r"[^a-z0-9+#\s-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class Intent:
    name: str
    keywords: list[str]
    responses: list[str] = field(default_factory=list)
    handler: Callable[[], str] | None = None
    cacheable: bool = True
    patterns: list[tuple[re.Pattern[str], int]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.patterns = [
            (re.compile(rf"\b{re.escape(normalize(kw))}\b"), len(kw.split()))
            for kw in self.keywords
        ]

    def score(self, text: str) -> int:
        return sum(weight for pattern, weight in self.patterns if pattern.search(text))


def _now_time() -> str:
    return f"The current server time is {datetime.now(UTC):%H:%M:%S} UTC."


def _today() -> str:
    return f"Today is {datetime.now(UTC):%A, %d %B %Y} (UTC)."


INTENTS: list[Intent] = [
    Intent(
        "greeting",
        ["hi", "hello", "hey", "good morning", "good afternoon", "good evening", "namaste"],
        [
            "Hello! I'm ScaleBot, your DevOps and cloud assistant. Ask me about Docker, "
            "Kubernetes, CI/CD, monitoring or scaling.",
            "Hi there! I can explain containers, orchestration, pipelines and observability. "
            "What would you like to learn today?",
        ],
    ),
    Intent(
        "farewell",
        ["bye", "goodbye", "see you", "good night"],
        ["Goodbye! Your conversation is saved, so you can continue it any time."],
    ),
    Intent(
        "thanks",
        ["thanks", "thank you", "thx", "appreciate"],
        ["You're welcome! Anything else about DevOps or cloud you'd like to explore?"],
    ),
    Intent(
        "help",
        ["help", "what can you do", "commands", "topics", "capabilities"],
        [
            "I can answer questions on: Docker & Compose, Kubernetes (pods, deployments, HPA), "
            "Jenkins & CI/CD, Git, NGINX, Redis, PostgreSQL, Prometheus, Grafana, the ELK stack, "
            "cloud computing, security (JWT, RBAC, TLS) and load testing. Try: 'What is a "
            "Kubernetes deployment?'"
        ],
    ),
    Intent(
        "identity",
        ["who are you", "your name", "what are you", "about you", "scalebot"],
        [
            "I'm ScaleBot, the assistant of ScaleBotAI - a cloud-native chatbot platform built "
            "with FastAPI, PostgreSQL, Redis, Docker and Kubernetes, designed for 1M+ requests "
            "per day."
        ],
    ),
    Intent("time", ["what time", "current time", "time now"], handler=_now_time, cacheable=False),
    Intent(
        "date",
        ["what date", "today s date", "todays date", "what day", "date today"],
        handler=_today,
        cacheable=False,
    ),
    Intent(
        "joke",
        ["joke", "funny", "make me laugh"],
        [
            "Why did the container break up with the VM? It needed more space - and less overhead.",
            "I'd tell you a UDP joke, but you might not get it.",
        ],
    ),
    Intent(
        "docker",
        ["docker", "container", "containers", "dockerfile", "docker image", "containerization"],
        [
            "Docker packages an application with its runtime and dependencies into an image; a "
            "container is a running instance of that image. Containers share the host kernel, so "
            "they start in milliseconds and are far lighter than VMs. ScaleBotAI uses a "
            "multi-stage Dockerfile: a builder stage installs dependencies and a slim runtime "
            "stage runs as a non-root user."
        ],
    ),
    Intent(
        "docker_compose",
        ["docker compose", "docker-compose", "compose file", "compose"],
        [
            "Docker Compose defines a multi-container application in one YAML file - services, "
            "networks and volumes - and starts it with 'docker compose up -d'. ScaleBotAI's "
            "compose file runs the API, PostgreSQL, Redis, NGINX, Prometheus, Grafana and the ELK "
            "stack, and 'docker compose up --scale chatbot-api=3' scales the API behind NGINX."
        ],
    ),
    Intent(
        "docker_storage",
        ["volume", "volumes", "bind mount", "docker storage", "persistent storage"],
        [
            "Container filesystems are ephemeral. Named volumes (managed by Docker) persist data "
            "such as the PostgreSQL data directory; bind mounts map a host path into a container "
            "(handy for configs); tmpfs mounts keep data in memory only. In Kubernetes the "
            "equivalent is a PersistentVolume claimed through a PersistentVolumeClaim."
        ],
    ),
    Intent(
        "docker_network",
        ["docker network", "bridge network", "container networking", "networking"],
        [
            "User-defined bridge networks give containers built-in DNS, so services reach each "
            "other by name (e.g. 'postgres:5432'). ScaleBotAI splits traffic into frontend, "
            "backend and monitoring networks, so NGINX can reach the API but not the database."
        ],
    ),
    Intent(
        "kubernetes",
        ["kubernetes", "k8s", "kubectl", "pod", "pods", "cluster", "orchestration", "node"],
        [
            "Kubernetes is a container orchestrator: you declare the desired state in YAML and "
            "controllers continuously reconcile the cluster towards it. Pods run containers, "
            "Deployments manage ReplicaSets for rolling updates, Services give pods a stable "
            "virtual IP, and Ingress exposes HTTP routes. ScaleBotAI runs 3-20 API pods."
        ],
    ),
    Intent(
        "k8s_deployment",
        ["deployment", "replicaset", "rolling update", "rollout", "rollback"],
        [
            "A Deployment manages ReplicaSets. Changing the pod template creates a new ReplicaSet "
            "and shifts pods gradually (rolling update). With maxUnavailable: 0 and maxSurge: 25%, "
            "capacity never drops during a release, and 'kubectl rollout undo' returns to the "
            "previous ReplicaSet in seconds."
        ],
    ),
    Intent(
        "k8s_service",
        ["service", "clusterip", "nodeport", "loadbalancer", "ingress"],
        [
            "A Service selects pods by label and load-balances to them through a stable virtual "
            "IP and DNS name (ClusterIP for internal traffic, NodePort/LoadBalancer for external). "
            "An Ingress adds host/path routing and TLS termination in front of Services."
        ],
    ),
    Intent(
        "k8s_config",
        ["configmap", "secret", "secrets", "environment variables"],
        [
            "ConfigMaps hold non-sensitive configuration; Secrets hold credentials. Both can be "
            "injected as environment variables or mounted files. Secrets are only base64-encoded "
            "by default, so enable etcd encryption at rest and restrict access with RBAC."
        ],
    ),
    Intent(
        "autoscaling",
        ["autoscaling", "autoscale", "hpa", "horizontal pod autoscaler", "scale out", "scaling"],
        [
            "The Horizontal Pod Autoscaler compares observed metrics with targets: desired = "
            "ceil(current x currentUtilization / target). ScaleBotAI scales between 3 and 20 pods "
            "on 70% CPU and 75% memory, scales up fast and waits 5 minutes before scaling down "
            "to avoid flapping. metrics-server must be installed."
        ],
    ),
    Intent(
        "probes",
        ["probe", "probes", "liveness", "readiness", "health check", "healthcheck"],
        [
            "Liveness probes restart a stuck container, readiness probes remove a pod from Service "
            "endpoints until it can serve, and startup probes give slow apps time to boot. "
            "ScaleBotAI uses /health/live for liveness and /health/ready (database check) for "
            "readiness."
        ],
    ),
    Intent(
        "jenkins",
        ["jenkins", "jenkinsfile", "pipeline", "build server"],
        [
            "Jenkins automates build, test and deployment through a Jenkinsfile kept in the "
            "repository (pipeline as code). ScaleBotAI's pipeline runs Checkout, Build, Unit Test, "
            "Lint, Docker Build, Security Scan (Trivy), Docker Push, Kubernetes Deploy and Smoke "
            "Test, rolling back automatically if the deployment fails."
        ],
    ),
    Intent(
        "cicd",
        ["cicd", "continuous integration", "continuous delivery", "continuous deployment"],
        [
            "CI/CD means every commit is automatically built and tested (continuous integration) "
            "and every passing build can be released automatically (continuous delivery or "
            "deployment). It shortens lead time, makes releases routine and lets failed changes "
            "be rolled back quickly."
        ],
    ),
    Intent(
        "git",
        ["git", "github", "branch", "commit", "merge", "pull request"],
        [
            "Git is a distributed version-control system. A simple flow: create a feature branch, "
            "commit small changes, open a pull request, let CI run, review and merge into main. "
            "A GitHub webhook then triggers the Jenkins pipeline."
        ],
    ),
    Intent(
        "nginx",
        ["nginx", "reverse proxy", "load balancer", "load balancing"],
        [
            "NGINX is a high-performance reverse proxy. In ScaleBotAI it terminates TLS, "
            "load-balances requests across API replicas with keep-alive connections, applies "
            "per-IP rate limits and adds security headers. In Kubernetes the ingress-nginx "
            "controller plays the same role."
        ],
    ),
    Intent(
        "redis",
        ["redis", "cache", "caching", "in-memory"],
        [
            "Redis is an in-memory key-value store with sub-millisecond latency. ScaleBotAI uses "
            "it as a cache-aside layer for bot answers and user profiles, for per-user rate "
            "limiting, for the JWT logout denylist and for daily request counters."
        ],
    ),
    Intent(
        "postgres",
        ["postgres", "postgresql", "database", "sql", "rdbms"],
        [
            "PostgreSQL is an ACID relational database. ScaleBotAI stores users, conversations, "
            "chats and feedback there, with foreign keys, unique constraints and composite indexes "
            "such as (user_id, timestamp) so chat history pages stay fast as data grows."
        ],
    ),
    Intent(
        "prometheus",
        ["prometheus", "metrics", "promql", "monitoring", "alerting"],
        [
            "Prometheus scrapes metrics over HTTP (pull model) and stores them as time series. "
            "ScaleBotAI exposes request rate, latency histograms, error counts, cache hits and DB "
            "pool usage at /metrics, and alert rules fire on error rate > 1% or P95 > 300 ms."
        ],
    ),
    Intent(
        "grafana",
        ["grafana", "dashboard", "dashboards", "visualization"],
        [
            "Grafana visualizes metrics from Prometheus and logs from Elasticsearch. ScaleBotAI "
            "ships provisioned dashboards for API overview (RPS, P50/P95/P99, errors), load "
            "testing and infrastructure (PostgreSQL, Redis, NGINX)."
        ],
    ),
    Intent(
        "elk",
        ["elk", "elasticsearch", "logstash", "kibana", "logging", "logs", "filebeat"],
        [
            "The ELK stack centralizes logs: the app writes JSON logs, Logstash (fed by Filebeat "
            "in Kubernetes) parses and routes them, Elasticsearch indexes them, and Kibana lets "
            "you search and visualize them. ScaleBotAI keeps separate indices for API, user, "
            "error and system logs."
        ],
    ),
    Intent(
        "cloud",
        ["cloud", "cloud computing", "aws", "azure", "gcp", "iaas", "paas", "saas"],
        [
            "Cloud computing delivers compute, storage and networking on demand with pay-as-you-go "
            "pricing. IaaS gives you VMs, PaaS a managed platform and SaaS a finished product. "
            "Cloud-native apps like ScaleBotAI are containerized, stateless and horizontally "
            "scalable."
        ],
    ),
    Intent(
        "devops",
        ["devops", "sre", "site reliability"],
        [
            "DevOps unites development and operations through automation, shared ownership and "
            "fast feedback: version control, CI/CD, infrastructure as code, monitoring and "
            "blameless post-mortems. Key outcome metrics are the four DORA metrics."
        ],
    ),
    Intent(
        "microservices",
        ["microservice", "microservices", "monolith"],
        [
            "Microservices split a system into small, independently deployable services that own "
            "their data and communicate over APIs. They scale and release independently but add "
            "operational complexity - which is why containers, orchestration and observability "
            "matter."
        ],
    ),
    Intent(
        "security",
        ["security", "jwt", "token", "rbac", "tls", "https", "authentication", "network policy"],
        [
            "ScaleBotAI layers its security: TLS at the edge, JWT access tokens (30 min) with "
            "refresh rotation and a Redis logout denylist, bcrypt password hashing, rate limiting, "
            "Kubernetes Secrets, least-privilege RBAC, default-deny NetworkPolicies and non-root, "
            "read-only containers scanned with Trivy."
        ],
    ),
    Intent(
        "fastapi",
        ["fastapi", "python api", "asgi", "uvicorn", "pydantic", "async"],
        [
            "FastAPI is an async Python web framework built on Starlette and Pydantic. It "
            "validates requests from type hints, generates OpenAPI docs at /docs, and handles "
            "thousands of concurrent I/O-bound requests per process with async/await."
        ],
    ),
    Intent(
        "load_testing",
        ["load test", "load testing", "k6", "jmeter", "locust", "stress test", "performance test"],
        [
            "Load testing checks behaviour under expected and extreme traffic. ScaleBotAI's plan "
            "runs smoke, baseline, load, stress, spike and 24-hour soak tests with k6, Locust or "
            "JMeter, passing when average latency < 300 ms, errors < 1% and CPU/memory < 80%."
        ],
    ),
    Intent(
        "high_availability",
        ["high availability", "uptime", "fault tolerance", "99 9", "availability", "downtime"],
        [
            "99.9% availability allows about 43.8 minutes of downtime per month. ScaleBotAI gets "
            "there with multiple replicas spread across nodes, readiness probes, a "
            "PodDisruptionBudget, zero-downtime rolling updates and automatic rollback."
        ],
    ),
]

# Equal scores keep the earlier intent, so order by specificity: sub-topics
# ("kubernetes deployment" -> k8s_deployment) before broad topics, and small
# talk last ("hi, what is docker?" -> docker).
_SPECIFIC = {
    "docker_compose",
    "docker_storage",
    "docker_network",
    "k8s_deployment",
    "k8s_service",
    "k8s_config",
    "autoscaling",
    "probes",
    "load_testing",
    "high_availability",
}
_SMALL_TALK = {"greeting", "farewell", "thanks", "help", "identity", "time", "date", "joke"}
INTENTS.sort(key=lambda i: 0 if i.name in _SPECIFIC else 2 if i.name in _SMALL_TALK else 1)

FALLBACK_RESPONSES = [
    "I'm not sure about that one yet. I specialise in DevOps and cloud topics - try asking about "
    "Docker, Kubernetes, Jenkins, CI/CD, Redis, PostgreSQL, monitoring or scaling.",
    "That's outside what I know so far. Type 'help' to see the topics I can explain.",
]


class RuleBasedBot(BotEngine):
    mode = "rule"

    def __init__(self, intents: Sequence[Intent] = INTENTS) -> None:
        self.intents = list(intents)

    @property
    def intent_count(self) -> int:
        return len(self.intents)

    def match(self, message: str) -> Intent | None:
        text = normalize(message)
        best: Intent | None = None
        best_score = 0
        for intent in self.intents:
            score = intent.score(text)
            if score > best_score:  # ties keep the earlier (higher-priority) intent
                best, best_score = intent, score
        return best

    async def reply(self, message: str, history: Sequence[ChatTurn] = ()) -> BotReply:
        intent = self.match(message)
        if intent is None:
            return BotReply(_pick(FALLBACK_RESPONSES, message), self.mode, "fallback")
        if intent.handler is not None:
            return BotReply(intent.handler(), self.mode, intent.name, intent.cacheable)
        return BotReply(_pick(intent.responses, message), self.mode, intent.name, intent.cacheable)


def _pick(options: Sequence[str], message: str) -> str:
    digest = hashlib.sha256(normalize(message).encode()).digest()
    return options[digest[0] % len(options)]
