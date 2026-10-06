from fastapi.testclient import TestClient

from tests.conftest import PASSWORD, bearer, login, register


def test_register_returns_user_without_password(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/register",
        json={"username": "alice", "email": "Alice@ScaleBotAI.dev", "password": PASSWORD},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["username"] == "alice"
    assert body["email"] == "alice@scalebotai.dev"
    assert body["role"] == "user"
    assert "password" not in body and "password_hash" not in body


def test_duplicate_username_and_email_conflict(client: TestClient) -> None:
    register(client, "bob")
    dup_name = client.post(
        "/api/v1/auth/register",
        json={"username": "bob", "email": "other@scalebotai.dev", "password": PASSWORD},
    )
    dup_email = client.post(
        "/api/v1/auth/register",
        json={"username": "bobby", "email": "bob@scalebotai.dev", "password": PASSWORD},
    )
    assert dup_name.status_code == dup_email.status_code == 409
    assert dup_name.json()["error"]["code"] == "CONFLICT"


def test_validation_errors_use_error_envelope(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/register",
        json={"username": "x", "email": "not-an-email", "password": "short"},
        headers={"X-Request-ID": "test-req-123"},
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["request_id"] == "test-req-123"
    assert {tuple(d["loc"])[-1] for d in error["details"]} == {"username", "email", "password"}


def test_password_needs_letter_and_digit(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/register",
        json={"username": "carol", "email": "carol@scalebotai.dev", "password": "onlyletters"},
    )
    assert response.status_code == 422


def test_login_with_username_or_email(client: TestClient) -> None:
    register(client, "dave")
    by_name = login(client, "dave")
    by_email = login(client, "DAVE@scalebotai.dev")
    assert by_name["token_type"] == "bearer"
    assert by_name["expires_in"] == 30 * 60
    assert by_email["user"]["username"] == "dave"
    assert by_name["user"]["last_login_at"] is not None


def test_login_failures(client: TestClient) -> None:
    register(client, "erin")
    wrong = client.post("/api/v1/auth/login", json={"username": "erin", "password": "Wrong123"})
    unknown = client.post("/api/v1/auth/login", json={"username": "nobody", "password": PASSWORD})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["error"]["message"] == unknown.json()["error"]["message"]
    assert wrong.headers["WWW-Authenticate"] == "Bearer"


def test_protected_route_requires_valid_token(client: TestClient) -> None:
    assert client.get("/api/v1/users/me").status_code == 401
    assert client.get("/api/v1/users/me", headers=bearer("garbage")).status_code == 401


def test_logout_revokes_access_and_refresh_tokens(client: TestClient, user_auth: dict) -> None:
    tokens = user_auth["tokens"]
    response = client.post(
        "/api/v1/auth/logout",
        headers=user_auth["headers"],
        json={"refresh_token": tokens["refresh_token"]},
    )
    assert response.status_code == 200
    me = client.get("/api/v1/users/me", headers=user_auth["headers"])
    assert me.status_code == 401
    assert me.json()["error"]["message"] == "Token has been revoked"
    refresh = client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert refresh.status_code == 401


def test_logout_accepts_empty_or_missing_body(client: TestClient) -> None:
    for kwargs in ({"json": {}}, {}):
        user = register(client)
        headers = bearer(login(client, user["username"])["access_token"])
        assert client.post("/api/v1/auth/logout", headers=headers, **kwargs).status_code == 200
        assert client.get("/api/v1/users/me", headers=headers).status_code == 401


def test_refresh_rotates_tokens(client: TestClient, user_auth: dict) -> None:
    old_refresh = user_auth["tokens"]["refresh_token"]
    first = client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert first.status_code == 200
    new_access = first.json()["access_token"]
    assert client.get("/api/v1/users/me", headers=bearer(new_access)).status_code == 200
    replay = client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert replay.status_code == 401


def test_access_token_cannot_be_used_as_refresh(client: TestClient, user_auth: dict) -> None:
    access = user_auth["tokens"]["access_token"]
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": access}).status_code == 401


def test_change_password(client: TestClient, user_auth: dict) -> None:
    headers = user_auth["headers"]
    bad = client.post(
        "/api/v1/auth/change-password",
        headers=headers,
        json={"current_password": "Wrong1234", "new_password": "N3wPassword"},
    )
    assert bad.status_code == 401
    ok = client.post(
        "/api/v1/auth/change-password",
        headers=headers,
        json={"current_password": PASSWORD, "new_password": "N3wPassword"},
    )
    assert ok.status_code == 200
    login(client, user_auth["user"]["username"], "N3wPassword")
