"""Locust load test for the ScaleBotAI API.

Every simulated user behaves like a learner using the DevOps assistant:

1. ``on_start``: registers a brand-new account (POST /api/v1/auth/register) and logs in
   (POST /api/v1/auth/login). The bearer token is attached to every later request.
2. Weighted tasks (wait 1-3 s between tasks, i.e. ~0.5 req/s per user):
   chat 6, history 2, conversations 1, profile 1.

Configuration (environment variables):

    BASE_URL               target if --host is not given (https://api.scalebotai.local)
    LOCUST_INSECURE        "true" (default) skips TLS verification for self-signed certs
    LOCUST_SHAPE           none|smoke|baseline|load|step|stress|spike|soak (def. none)
    LOCUST_REPEAT_RATIO    share of chats that repeat a question verbatim (default 0.2)
    LOCUST_CONTINUE_RATIO  share of chats continuing the last conversation (default 0.7)
    LOCUST_WAIT_MIN/MAX    think time between tasks in seconds (default 1 / 3; use 3 / 10
                           for the soak test so it runs at realistic, not peak, load)
    LOADTEST_EMAIL_DOMAIN  e-mail domain of generated users (default scalebotai.dev)
    LOADTEST_PASSWORD      password of generated users (default LoadTest123!)
    SLO_MAX_FAIL_RATIO     exit code 1 above this failure ratio (default 0.01 = 1 %)
    SLO_MAX_AVG_MS         exit code 1 above this average response time (default 300)

Examples (see README.md in this folder for more):

    locust -f tests/load/locust/locustfile.py --headless -u 100 -r 10 -t 2m \
        --host https://api.scalebotai.local
    LOCUST_SHAPE=step locust -f tests/load/locust/locustfile.py --headless \
        --host https://api.scalebotai.local

The API rate limit (RATE_LIMIT_PER_MINUTE, default 120/min per user or client IP) and
the NGINX limit_req zone must be raised for load tests, otherwise most requests from a
few load-generator IPs come back as 429 and are reported as failures here.
"""

import itertools
import logging
import os
import random
import time
import uuid
from typing import Any

import urllib3
from locust import HttpUser, LoadTestShape, between, events, task
from locust.exception import StopUser
from locust.runners import WorkerRunner

logger = logging.getLogger("scalebotai.locust")

# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------
DEFAULT_BASE_URL = "https://api.scalebotai.local"
TRUTHY = {"1", "true", "yes", "on"}

INSECURE_TLS = os.getenv("LOCUST_INSECURE", "true").strip().lower() in TRUTHY
SHAPE = os.getenv("LOCUST_SHAPE", "").strip().lower()
REPEAT_RATIO = float(os.getenv("LOCUST_REPEAT_RATIO", "0.2"))
CONTINUE_RATIO = float(os.getenv("LOCUST_CONTINUE_RATIO", "0.7"))
SLO_MAX_FAIL_RATIO = float(os.getenv("SLO_MAX_FAIL_RATIO", "0.01"))
SLO_MAX_AVG_MS = float(os.getenv("SLO_MAX_AVG_MS", "300"))
WAIT_MIN = float(os.getenv("LOCUST_WAIT_MIN", "1"))
WAIT_MAX = float(os.getenv("LOCUST_WAIT_MAX", "3"))

# Reserved TLDs (.local, .test, .example, ...) are rejected by Pydantic's EmailStr,
# so generated users get a normal-looking domain. Not a real mailbox: nothing is sent.
EMAIL_DOMAIN = os.getenv("LOADTEST_EMAIL_DOMAIN", "scalebotai.dev")
# Test-only password: 8-128 chars with at least one letter and one digit.
PASSWORD = os.getenv("LOADTEST_PASSWORD", "LoadTest123!")

RATE_LIMITED_MSG = "rate limited (raise RATE_LIMIT_PER_MINUTE for load tests)"
# Renew the access token this many seconds before it expires (matters in soak tests).
TOKEN_RENEW_MARGIN_S = 60

if INSECURE_TLS:
    # Self-signed certificate on NGINX / ingress: do not flood the log with warnings.
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Per-process counter; together with the uuid4 prefix it keeps usernames unique across
# all users of all workers (username rule: ^[a-zA-Z0-9_.-]+$, 3-50 chars).
_USER_SEQ = itertools.count(1)

# --------------------------------------------------------------------------------------
# Question pool
# --------------------------------------------------------------------------------------
QUESTIONS = (
    "Hello!",
    "Hi, what can you help me with?",
    "help",
    "What is Docker and why should I use containers?",
    "How do I write a multi-stage Dockerfile for a Python app?",
    "What is the difference between a Docker image and a container?",
    "How do Docker volumes and bind mounts differ?",
    "What is Kubernetes?",
    "What is the difference between a Deployment and a StatefulSet in Kubernetes?",
    "How does the Horizontal Pod Autoscaler work?",
    "How do readiness and liveness probes work in Kubernetes?",
    "How do I expose a Kubernetes service with an Ingress?",
    "What is a Jenkins pipeline?",
    "How do I write a declarative Jenkinsfile with build, test and deploy stages?",
    "What is CI/CD and why does it matter?",
    "How do I roll back a failed deployment?",
    "What is the difference between git merge and git rebase?",
    "What is a good Git branching strategy for a small team?",
    "How do I configure NGINX as a reverse proxy and load balancer?",
    "How does rate limiting work in NGINX?",
    "How can Redis be used as a cache?",
    "How do I tune PostgreSQL connection pooling?",
    "What is Prometheus and how does it scrape metrics?",
    "How do I build a Grafana dashboard for request latency?",
    "What is the ELK stack used for?",
    "How do I ship container logs to Elasticsearch?",
    "What are the main cloud service models IaaS, PaaS and SaaS?",
    "How do I scale a web application to handle a million requests per day?",
    "What is a blue-green deployment?",
    "How does a canary release reduce deployment risk?",
)

# Context sentences make most questions unique (Redis cache miss -> bot engine runs),
# while REPEAT_RATIO of the chats re-ask a pool question verbatim (cache hit after
# warm-up). The resulting hit ratio roughly equals REPEAT_RATIO, like real traffic
# where a few popular questions repeat and the long tail does not.
CONTEXTS = (
    "We run {n} microservices in production.",
    "Our cluster has {n} worker nodes.",
    "I have an interview in {n} days.",
    "Our pipeline currently takes {n} minutes.",
    "We serve about {n}k requests per day.",
    "This is for my capstone project, team of {n}.",
)


def pick_question() -> str:
    """Return a chat message: a verbatim repeat or a (mostly unique) variant."""
    question = random.choice(QUESTIONS)
    if random.random() < REPEAT_RATIO:
        return question
    context = random.choice(CONTEXTS).format(n=random.randint(2, 500))
    return f"{question} {context}"


def _json(resp: Any) -> Any:
    """Parse a JSON body, returning None instead of raising on invalid JSON."""
    try:
        return resp.json()
    except ValueError:
        return None


def _snippet(resp: Any, limit: int = 200) -> str:
    """First characters of the body, for failure messages."""
    return (resp.text or "")[:limit]


# --------------------------------------------------------------------------------------
# User behaviour
# --------------------------------------------------------------------------------------
class ScaleBotUser(HttpUser):
    """A learner who signs up, logs in and chats with the DevOps assistant."""

    host = os.getenv("BASE_URL", DEFAULT_BASE_URL)
    wait_time = between(WAIT_MIN, WAIT_MAX)

    def on_start(self) -> None:
        if INSECURE_TLS:
            self.client.verify = False
        self.client.headers.update({"Accept": "application/json"})

        self.username = f"lt_{uuid.uuid4().hex[:8]}_{next(_USER_SEQ)}"
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self.token_renew_at = 0.0
        self.conversation_id: str | None = None

        # A user that cannot authenticate is useless: record the failure and stop this
        # user gracefully (Locust logs the StopUser, calls on_stop; the test goes on).
        if not self._register() or not self._login():
            raise StopUser()

    # ------------------------------------------------------------------- auth helpers
    def _register(self) -> bool:
        payload = {
            "username": self.username,
            "email": f"{self.username}@{EMAIL_DOMAIN}",
            "password": PASSWORD,
            "full_name": f"Load Test {self.username}",
        }
        with self.client.post(
            "/api/v1/auth/register",
            json=payload,
            name="/api/v1/auth/register",
            catch_response=True,
        ) as resp:
            if resp.status_code in (201, 409):
                # 409 = the user already exists (e.g. re-used name): just log in.
                resp.success()
                return True
            self._fail(resp, "register")
            return False

    def _login(self) -> bool:
        self.client.headers.pop("Authorization", None)
        payload = {"username": self.username, "password": PASSWORD}
        with self.client.post(
            "/api/v1/auth/login",
            json=payload,
            name="/api/v1/auth/login",
            catch_response=True,
        ) as resp:
            if resp.status_code != 200:
                self._fail(resp, "login")
                return False
            data = _json(resp)
            if not isinstance(data, dict) or not data.get("access_token"):
                resp.failure("login: response has no access_token")
                return False
            self._store_tokens(data)
            resp.success()
            return True

    def _store_tokens(self, data: dict[str, Any]) -> None:
        self.access_token = data["access_token"]
        self.refresh_token = data.get("refresh_token")
        try:
            expires_in = float(data.get("expires_in") or 1800)
        except (TypeError, ValueError):
            expires_in = 1800.0
        lifetime = max(expires_in - TOKEN_RENEW_MARGIN_S, 30.0)
        self.token_renew_at = time.monotonic() + lifetime
        self.client.headers["Authorization"] = f"Bearer {self.access_token}"

    def _renew_token(self) -> None:
        """Renew the access token shortly before expiry (refresh, else re-login)."""
        if self.refresh_token:
            with self.client.post(
                "/api/v1/auth/refresh",
                json={"refresh_token": self.refresh_token},
                name="/api/v1/auth/refresh",
                catch_response=True,
            ) as resp:
                data = _json(resp) if resp.status_code == 200 else None
                if isinstance(data, dict) and data.get("access_token"):
                    self._store_tokens(data)
                    resp.success()
                    return
                self._fail(resp, "refresh")
        if not self._login():
            raise StopUser()

    # ---------------------------------------------------------------- request helpers
    @staticmethod
    def _fail(resp: Any, action: str) -> None:
        if resp.status_code == 429:
            resp.failure(RATE_LIMITED_MSG)
        elif resp.status_code == 0:
            error = getattr(resp, "error", None)
            resp.failure(f"{action}: connection error: {error}")
        else:
            resp.failure(f"{action}: HTTP {resp.status_code}: {_snippet(resp)}")

    def _call(
        self,
        method: str,
        path: str,
        name: str,
        *,
        json: dict[str, Any] | None = None,
        required: tuple[str, ...] = (),
        retry_on_401: bool = True,
    ) -> dict[str, Any] | None:
        """Send an authenticated request and validate status 200 + required JSON keys.

        A 401 is recorded as a failure, then the user logs in again and retries once.
        """
        if time.monotonic() >= self.token_renew_at:
            self._renew_token()

        unauthorized = False
        with self.client.request(
            method,
            path,
            name=name,
            json=json,
            catch_response=True,
        ) as resp:
            if resp.status_code == 200:
                data = _json(resp)
                if not isinstance(data, dict):
                    resp.failure(f"{name}: response is not a JSON object")
                    return None
                missing = [key for key in required if key not in data]
                if missing:
                    resp.failure(f"{name}: JSON is missing {missing}")
                    return None
                resp.success()
                return data
            if resp.status_code == 401 and retry_on_401:
                resp.failure("401 unauthorized (token rejected), re-logging in")
                unauthorized = True
            else:
                self._fail(resp, name)
                return None

        # Outside the context manager so the 401 above is reported before the re-login.
        if unauthorized:
            if not self._login():
                raise StopUser()
            return self._call(
                method,
                path,
                name,
                json=json,
                required=required,
                retry_on_401=False,
            )
        return None

    # -------------------------------------------------------------------------- tasks
    @task(6)
    def chat(self) -> None:
        payload: dict[str, Any] = {"message": pick_question()}
        # ~70 % of messages continue the previous conversation, the rest start anew.
        if self.conversation_id and random.random() < CONTINUE_RATIO:
            payload["conversation_id"] = self.conversation_id
        data = self._call(
            "POST",
            "/api/v1/chat",
            "/api/v1/chat",
            json=payload,
            required=("response", "conversation_id"),
        )
        # On failure start a fresh conversation next time (e.g. it was deleted -> 404).
        self.conversation_id = data["conversation_id"] if data else None

    @task(2)
    def history(self) -> None:
        self._call(
            "GET",
            "/api/v1/chat/history?page=1&size=20",
            "/api/v1/chat/history",
            required=("items",),
        )

    @task(1)
    def conversations(self) -> None:
        self._call(
            "GET",
            "/api/v1/chat/conversations?page=1&size=20",
            "/api/v1/chat/conversations",
            required=("items",),
        )

    @task(1)
    def me(self) -> None:
        self._call(
            "GET",
            "/api/v1/users/me",
            "/api/v1/users/me",
            required=("id", "username"),
        )


# --------------------------------------------------------------------------------------
# Load profiles (LOCUST_SHAPE)
# --------------------------------------------------------------------------------------
# Each stage is (end_time_seconds, users, spawn_rate): until `end_time_seconds` after
# the start, Locust moves towards `users` at `spawn_rate` users/s. After the last stage
# the test stops. When a shape is active, -u/-r/-t on the command line are ignored.
#
# Locust gotcha: if a locustfile defines more than one LoadTestShape subclass, Locust
# refuses to pick one (or needs --class-picker). So the tables live in a plain dict and
# exactly ONE shape class is created below, and only when LOCUST_SHAPE names a profile.
# In distributed mode the shape runs on the master: set LOCUST_SHAPE there.
#
# Rough arithmetic: one user ~ 1 request / (2 s think + latency) ~ 0.5 req/s, so
# 1,000 users ~ 500 req/s (the platform's design headroom) and 50,000 users ~ 25k req/s
# (far beyond it: the spike/stress phases measure degradation and recovery, not SLOs).
# Every new user also registers + logs in (two bcrypt hashes server-side), so high spawn
# rates double as an authentication storm.
Stage = tuple[int, int, float]

# Stress steps: stage i ends at 180*i seconds with 2,500*i users (i = 1..10).
_STRESS_STEPS = tuple((180 * i, 2_500 * i, 50) for i in range(1, 11))

PROFILES: dict[str, tuple[Stage, ...]] = {
    # 100 users for 2 minutes: does everything work at all?
    "smoke": ((120, 100, 10),),
    # 1,000 users for 10 minutes (100 s ramp-up, then steady state).
    "baseline": ((600, 1_000, 10),),
    # Ramp to 10,000 users over ~10 minutes, hold until 30 minutes.
    "load": ((600, 10_000, 17), (1_800, 10_000, 17)),
    # Stress: +2,500 users every 3 minutes up to 25,000 (t=30 min), hold 5 minutes.
    "step": (*_STRESS_STEPS, (2_100, 25_000, 50)),
    # Spike: 5 minutes at 1,000 -> jump to 50,000 (~50 s surge + ~2 min at peak)
    # -> drop back to 1,000 for 5 minutes of recovery -> stop.
    "spike": ((300, 1_000, 20), (480, 50_000, 1_000), (780, 1_000, 1_000)),
    # Soak: 500 users for 24 hours (memory leaks, pool exhaustion, token renewal).
    "soak": ((86_400, 500, 5),),
}
PROFILES["stress"] = PROFILES["step"]  # alias

if SHAPE in PROFILES:

    class StagesShape(LoadTestShape):
        """Stage-table load shape selected with LOCUST_SHAPE."""

        stages = PROFILES[SHAPE]

        def tick(self) -> tuple[int, float] | None:
            run_time = self.get_run_time()
            for end_time, users, spawn_rate in self.stages:
                if run_time < end_time:
                    return users, spawn_rate
            return None  # past the last stage: stop the test

elif SHAPE not in ("", "none"):
    valid = ", ".join(["none", *sorted(PROFILES)])
    msg = f"Unknown LOCUST_SHAPE={SHAPE!r}; valid values: {valid}"
    raise SystemExit(msg)


# --------------------------------------------------------------------------------------
# Event hooks
# --------------------------------------------------------------------------------------
@events.test_start.add_listener
def log_test_start(environment: Any, **_kwargs: Any) -> None:
    logger.info(
        "ScaleBotAI load test: host=%s shape=%s insecure_tls=%s",
        environment.host or ScaleBotUser.host,
        SHAPE if SHAPE in PROFILES else "none (CLI -u/-r/-t)",
        INSECURE_TLS,
    )


@events.quitting.add_listener
def enforce_slo(environment: Any, **_kwargs: Any) -> None:
    """Exit code 1 when the load-testing success criteria are missed.

    Criteria: error rate < 1 % and average response time < 300 ms (all requests,
    including register/login). Only the master/standalone runner sees the totals.
    """
    if isinstance(environment.runner, WorkerRunner):
        return
    total = environment.stats.total
    if total.num_requests == 0:
        logger.warning("SLO check skipped: no requests were made")
        return

    fail_ratio = total.fail_ratio
    avg_ms = total.avg_response_time
    p95_ms = total.get_response_time_percentile(0.95)
    summary = (
        f"requests={total.num_requests} fail_ratio={fail_ratio:.3%} "
        f"avg={avg_ms:.1f}ms p95={p95_ms:.0f}ms "
        f"(limits: fail<{SLO_MAX_FAIL_RATIO:.2%}, avg<{SLO_MAX_AVG_MS:.0f}ms)"
    )

    if fail_ratio > SLO_MAX_FAIL_RATIO or avg_ms > SLO_MAX_AVG_MS:
        logger.error("SLO FAILED: %s", summary)
        environment.process_exit_code = 1
    else:
        logger.info("SLO passed: %s", summary)
        environment.process_exit_code = 0
