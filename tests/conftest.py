"""Shared fixtures. Tests run without external services: SQLite + fakeredis."""

import itertools
from collections.abc import Callable, Iterator
from pathlib import Path

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app

PASSWORD = "Str0ngPassw0rd"
ADMIN_PASSWORD = "AdminPassw0rd"
_counter = itertools.count(1)


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "test",
        "database_url": f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}",
        "jwt_secret_key": "test-secret-key-that-is-long-enough-for-hs256",
        "bcrypt_rounds": 4,
        "rate_limit_per_minute": 1000,
        "admin_password": ADMIN_PASSWORD,
        "log_level": "WARNING",
        "chatbot_mode": "rule",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


@pytest.fixture
def make_client(tmp_path: Path) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def factory(**overrides: object) -> TestClient:
        app = create_app(
            make_settings(tmp_path, **overrides),
            redis_factory=lambda _: fakeredis.FakeAsyncRedis(decode_responses=True),
        )
        client = TestClient(app)
        client.__enter__()  # runs the lifespan (creates tables, admin user)
        clients.append(client)
        return client

    yield factory
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client: Callable[..., TestClient]) -> TestClient:
    return make_client()


def register(client: TestClient, username: str | None = None, password: str = PASSWORD) -> dict:
    username = username or f"user{next(_counter)}"
    response = client.post(
        "/api/v1/auth/register",
        json={"username": username, "email": f"{username}@scalebotai.dev", "password": password},
    )
    assert response.status_code == 201, response.text
    return response.json()


def login(client: TestClient, username: str, password: str = PASSWORD) -> dict:
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response.json()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def user_auth(client: TestClient) -> dict:
    """A registered, logged-in user: {"user", "headers", "tokens"}."""
    user = register(client)
    tokens = login(client, user["username"])
    return {"user": user, "tokens": tokens, "headers": bearer(tokens["access_token"])}


@pytest.fixture
def admin_headers(client: TestClient) -> dict[str, str]:
    return bearer(login(client, "admin", ADMIN_PASSWORD)["access_token"])
