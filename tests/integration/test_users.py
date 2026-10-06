from fastapi.testclient import TestClient

from tests.conftest import register


def test_get_and_update_profile(client: TestClient, user_auth: dict) -> None:
    headers = user_auth["headers"]
    me = client.get("/api/v1/users/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["id"] == user_auth["user"]["id"]

    updated = client.put(
        "/api/v1/users/me",
        headers=headers,
        json={"full_name": "Grace Hopper", "email": "grace@scalebotai.dev"},
    )
    assert updated.status_code == 200
    assert updated.json()["full_name"] == "Grace Hopper"
    # The cached profile was invalidated, so the next read is fresh.
    assert client.get("/api/v1/users/me", headers=headers).json()["email"] == "grace@scalebotai.dev"


def test_update_requires_a_field_and_unique_email(client: TestClient, user_auth: dict) -> None:
    other = register(client)
    headers = user_auth["headers"]
    assert client.put("/api/v1/users/me", headers=headers, json={}).status_code == 422
    clash = client.put("/api/v1/users/me", headers=headers, json={"email": other["email"]})
    assert clash.status_code == 409


def test_user_stats(client: TestClient, user_auth: dict) -> None:
    headers = user_auth["headers"]
    client.post("/api/v1/chat", headers=headers, json={"message": "What is Docker?"})
    stats = client.get("/api/v1/users/me/stats", headers=headers).json()
    assert stats["total_conversations"] == 1
    assert stats["total_messages"] == 1
    assert stats["feedback_given"] == 0
    assert stats["last_chat_at"] is not None


def test_delete_account_cascades_and_revokes(client: TestClient, user_auth: dict) -> None:
    headers = user_auth["headers"]
    client.post("/api/v1/chat", headers=headers, json={"message": "hello"})
    response = client.delete("/api/v1/users/me", headers=headers)
    assert response.status_code == 204
    assert response.content == b""
    assert client.get("/api/v1/users/me", headers=headers).status_code == 401
    login = client.post(
        "/api/v1/auth/login",
        json={"username": user_auth["user"]["username"], "password": "Str0ngPassw0rd"},
    )
    assert login.status_code == 401
