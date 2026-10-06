from collections.abc import Callable

from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError

from tests.conftest import register


def test_health_endpoints(client: TestClient) -> None:
    health = client.get("/health")
    assert health.status_code == 200
    body = health.json()
    assert body["status"] == "ok"
    assert set(body["checks"]) == {"database", "redis", "chatbot"}
    assert client.get("/health/live").json() == {"status": "alive"}
    ready = client.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"


def test_metrics_exposed_in_prometheus_format(client: TestClient, user_auth: dict) -> None:
    client.post("/api/v1/chat", headers=user_auth["headers"], json={"message": "hi"})
    client.get(
        "/api/v1/chat/conversations/3fa85f64-5717-4562-b3fc-2c963f66afa6",
        headers=user_auth["headers"],
    )
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    text = response.text
    assert 'scalebot_http_requests_total{method="POST",route="/api/v1/chat",status="200"}' in text
    # Path parameters are templated to keep label cardinality bounded.
    assert 'route="/api/v1/chat/conversations/{conversation_id}"' in text
    assert "scalebot_http_request_duration_seconds_bucket" in text
    assert 'scalebot_chat_messages_total{bot_mode="rule",cached="false"}' in text
    assert "scalebot_auth_events_total" in text
    assert "scalebot_app_info" in text


def test_request_id_and_timing_headers(client: TestClient) -> None:
    generated = client.get("/health/live")
    assert len(generated.headers["X-Request-ID"]) == 32
    assert float(generated.headers["X-Process-Time-ms"]) >= 0
    echoed = client.get("/health/live", headers={"X-Request-ID": "abc-123"})
    assert echoed.headers["X-Request-ID"] == "abc-123"
    unsafe = client.get("/health/live", headers={"X-Request-ID": "bad id\nwith newline"})
    assert unsafe.headers["X-Request-ID"] != "bad id\nwith newline"


def test_unknown_route_uses_error_envelope(client: TestClient) -> None:
    response = client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_rate_limit_returns_429(make_client: Callable[..., TestClient]) -> None:
    client = make_client(rate_limit_per_minute=3)
    user = register(client)
    token = client.post(
        "/api/v1/auth/login", json={"username": user["username"], "password": "Str0ngPassw0rd"}
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    statuses = [client.get("/api/v1/users/me", headers=headers) for _ in range(4)]
    assert [r.status_code for r in statuses] == [200, 200, 200, 429]
    assert statuses[0].headers["X-RateLimit-Remaining"] == "2"
    limited = statuses[-1]
    assert limited.json()["error"]["code"] == "RATE_LIMITED"
    assert int(limited.headers["Retry-After"]) > 0


class BrokenRedis:
    """Simulates a Redis outage: every command raises a connection error."""

    def __init__(self) -> None:
        self.attempts = 0

    def __getattr__(self, name: str):
        async def fail(*args, **kwargs):
            self.attempts += 1
            raise RedisConnectionError("redis is down")

        return fail

    def pipeline(self, *args, **kwargs):
        self.attempts += 1
        raise RedisConnectionError("redis is down")

    async def aclose(self) -> None:
        return None


def test_api_fails_open_when_redis_is_down(tmp_path) -> None:
    from app.main import create_app
    from tests.conftest import make_settings

    broken = BrokenRedis()
    app = create_app(make_settings(tmp_path), redis_factory=lambda _: broken)  # type: ignore[arg-type, return-value]
    with TestClient(app) as client:
        user = register(client)
        token = client.post(
            "/api/v1/auth/login", json={"username": user["username"], "password": "Str0ngPassw0rd"}
        ).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        for question in ("What is Docker?", "What is Redis?", "What is Jenkins?"):
            chat = client.post("/api/v1/chat", headers=headers, json={"message": question})
            assert chat.status_code == 200
            assert chat.json()["cached"] is False
        assert client.get("/api/v1/chat/history", headers=headers).json()["total"] == 3
        health = client.get("/health").json()
        assert health["status"] == "degraded"
        assert health["checks"]["redis"] == {
            "status": "down",
            "latency_ms": None,
            "error": "circuit open",
        }
        assert client.get("/health/ready").status_code == 200  # still serving traffic

    # The circuit breaker opened on the first failure: ~20 Redis operations above
    # (rate limits, denylist, cache, counters, health) cost a single attempt.
    assert broken.attempts == 1
