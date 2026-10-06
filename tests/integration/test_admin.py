from fastapi.testclient import TestClient

ADMIN_GETS = [
    "/api/v1/admin/analytics",
    "/api/v1/admin/stats/users",
    "/api/v1/admin/stats/chats",
    "/api/v1/admin/stats/requests",
    "/api/v1/admin/system",
    "/api/v1/admin/health",
    "/api/v1/admin/users",
    "/api/v1/admin/feedback",
]


def test_admin_endpoints_forbidden_for_regular_users(client: TestClient, user_auth: dict) -> None:
    for path in ADMIN_GETS:
        response = client.get(path, headers=user_auth["headers"])
        assert response.status_code == 403, path
        assert response.json()["error"]["code"] == "FORBIDDEN"


def test_admin_endpoints_available_to_admin(client: TestClient, admin_headers: dict) -> None:
    for path in ADMIN_GETS:
        assert client.get(path, headers=admin_headers).status_code == 200, path


def test_analytics_reflect_activity(
    client: TestClient, user_auth: dict, admin_headers: dict
) -> None:
    chat = client.post(
        "/api/v1/chat", headers=user_auth["headers"], json={"message": "hello"}
    ).json()
    client.post(
        f"/api/v1/chat/{chat['chat_id']}/feedback", headers=user_auth["headers"], json={"rating": 4}
    )

    users = client.get("/api/v1/admin/stats/users", headers=admin_headers).json()
    assert users["total_users"] == 2  # bootstrap admin + user
    assert users["admin_users"] == 1
    assert users["new_users_today"] == 2

    chats = client.get("/api/v1/admin/stats/chats", headers=admin_headers).json()
    assert chats["total_chats"] == 1
    assert chats["chats_today"] == 1
    assert chats["avg_bot_latency_ms"] is not None

    feedback = client.get("/api/v1/admin/feedback", headers=admin_headers).json()
    assert feedback == {
        "total": 1,
        "average_rating": 4.0,
        "distribution": {"1": 0, "2": 0, "3": 0, "4": 1, "5": 0},
    }

    requests = client.get("/api/v1/admin/stats/requests?days=3", headers=admin_headers).json()
    assert requests["days"] == 3
    assert len(requests["series"]) == 3
    assert requests["series"][-1]["requests"] >= 5  # counted by the middleware

    analytics = client.get("/api/v1/admin/analytics", headers=admin_headers).json()
    assert set(analytics) == {"users", "chats", "requests", "feedback", "generated_at"}


def test_system_and_health_details(client: TestClient, admin_headers: dict) -> None:
    system = client.get("/api/v1/admin/system", headers=admin_headers).json()
    assert system["environment"] == "test"
    assert system["redis"]["connected"] is True
    assert system["process"]["memory_rss_mb"] > 0

    health = client.get("/api/v1/admin/health", headers=admin_headers).json()
    assert health["status"] == "ok"
    assert health["checks"]["database"]["status"] == "ok"


def test_list_search_and_deactivate_user(
    client: TestClient, user_auth: dict, admin_headers: dict
) -> None:
    username = user_auth["user"]["username"]
    listing = client.get(f"/api/v1/admin/users?search={username}", headers=admin_headers).json()
    assert listing["total"] == 1

    user_id = user_auth["user"]["id"]
    updated = client.patch(
        f"/api/v1/admin/users/{user_id}", headers=admin_headers, json={"is_active": False}
    )
    assert updated.status_code == 200
    assert updated.json()["is_active"] is False
    blocked = client.get("/api/v1/users/me", headers=user_auth["headers"])
    assert blocked.status_code == 403
    assert (
        client.patch(f"/api/v1/admin/users/{user_id}", headers=admin_headers, json={}).status_code
        == 422
    )


def test_promote_user_to_admin(client: TestClient, user_auth: dict, admin_headers: dict) -> None:
    user_id = user_auth["user"]["id"]
    client.patch(f"/api/v1/admin/users/{user_id}", headers=admin_headers, json={"role": "admin"})
    assert client.get("/api/v1/admin/stats/users", headers=user_auth["headers"]).status_code == 200
