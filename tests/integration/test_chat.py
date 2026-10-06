import uuid

from fastapi.testclient import TestClient

from tests.conftest import bearer, login, register


def chat(client: TestClient, headers: dict, message: str, conversation_id: str | None = None):
    body = {"message": message}
    if conversation_id:
        body["conversation_id"] = conversation_id
    response = client.post("/api/v1/chat", headers=headers, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_first_message_starts_conversation(client: TestClient, user_auth: dict) -> None:
    result = chat(client, user_auth["headers"], "What is Kubernetes?")
    assert result["bot_mode"] == "rule"
    assert "Kubernetes" in result["response"]
    assert result["conversation_id"]
    assert result["chat_id"] > 0
    assert result["latency_ms"] >= 0


def test_continue_conversation_and_cache(client: TestClient, user_auth: dict) -> None:
    headers = user_auth["headers"]
    first = chat(client, headers, "Explain Redis caching")
    second = chat(client, headers, "explain redis caching!", first["conversation_id"])
    assert first["cached"] is False
    assert second["cached"] is True  # same normalized question served from Redis
    assert second["conversation_id"] == first["conversation_id"]

    detail = client.get(f"/api/v1/chat/conversations/{first['conversation_id']}", headers=headers)
    assert detail.status_code == 200
    assert [m["id"] for m in detail.json()["messages"]] == [first["chat_id"], second["chat_id"]]


def test_blank_and_oversized_messages_rejected(client: TestClient, user_auth: dict) -> None:
    headers = user_auth["headers"]
    assert client.post("/api/v1/chat", headers=headers, json={"message": "   "}).status_code == 422
    too_long = {"message": "x" * 2001}
    assert client.post("/api/v1/chat", headers=headers, json=too_long).status_code == 422


def test_conversation_lifecycle(client: TestClient, user_auth: dict) -> None:
    headers = user_auth["headers"]
    created = client.post("/api/v1/chat/conversations", headers=headers, json={"title": "Infra"})
    assert created.status_code == 201
    conv_id = created.json()["id"]
    chat(client, headers, "What is NGINX?", conv_id)

    listing = client.get("/api/v1/chat/conversations", headers=headers).json()
    assert listing["total"] == 1
    assert listing["items"][0]["message_count"] == 1

    renamed = client.patch(
        f"/api/v1/chat/conversations/{conv_id}", headers=headers, json={"title": "Networking"}
    )
    assert renamed.json()["title"] == "Networking"

    assert (
        client.delete(f"/api/v1/chat/conversations/{conv_id}", headers=headers).status_code == 204
    )
    missing = client.get(f"/api/v1/chat/conversations/{conv_id}", headers=headers)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NOT_FOUND"


def test_users_cannot_access_each_others_conversations(client: TestClient, user_auth: dict) -> None:
    conv_id = chat(client, user_auth["headers"], "hello")["conversation_id"]
    intruder = register(client)
    intruder_headers = bearer(login(client, intruder["username"])["access_token"])
    assert (
        client.get(f"/api/v1/chat/conversations/{conv_id}", headers=intruder_headers).status_code
        == 404
    )
    hijack = client.post(
        "/api/v1/chat", headers=intruder_headers, json={"message": "hi", "conversation_id": conv_id}
    )
    assert hijack.status_code == 404
    random_id = uuid.uuid4()
    assert (
        client.get(f"/api/v1/chat/conversations/{random_id}", headers=intruder_headers).status_code
        == 404
    )


def test_history_pagination_and_filter(client: TestClient, user_auth: dict) -> None:
    headers = user_auth["headers"]
    conv = chat(client, headers, "What is Docker?")["conversation_id"]
    for question in ("What is Git?", "What is Jenkins?"):
        chat(client, headers, question, conv)
    chat(client, headers, "What is Grafana?")  # separate conversation

    page = client.get("/api/v1/chat/history?page=1&size=2", headers=headers).json()
    assert page["total"] == 4
    assert page["pages"] == 2
    assert page["items"][0]["question"] == "What is Grafana?"  # newest first

    filtered = client.get(f"/api/v1/chat/history?conversation_id={conv}", headers=headers).json()
    assert filtered["total"] == 3
    assert client.get("/api/v1/chat/history?size=500", headers=headers).status_code == 422


def test_delete_history(client: TestClient, user_auth: dict) -> None:
    headers = user_auth["headers"]
    chat(client, headers, "hello")
    chat(client, headers, "What is SQL?")
    response = client.delete("/api/v1/chat/history", headers=headers)
    assert response.json() == {"deleted": 2}
    assert client.get("/api/v1/chat/history", headers=headers).json()["total"] == 0
    assert client.get("/api/v1/chat/conversations", headers=headers).json()["total"] == 0


def test_feedback_upsert(client: TestClient, user_auth: dict) -> None:
    headers = user_auth["headers"]
    chat_id = chat(client, headers, "What is Prometheus?")["chat_id"]
    first = client.post(f"/api/v1/chat/{chat_id}/feedback", headers=headers, json={"rating": 3})
    second = client.post(
        f"/api/v1/chat/{chat_id}/feedback",
        headers=headers,
        json={"rating": 5, "comment": "Clear answer"},
    )
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert second.json()["rating"] == 5

    history = client.get("/api/v1/chat/history", headers=headers).json()
    assert history["items"][0]["feedback"] == {"rating": 5, "comment": "Clear answer"}

    assert (
        client.post(
            f"/api/v1/chat/{chat_id}/feedback", headers=headers, json={"rating": 6}
        ).status_code
        == 422
    )
    assert (
        client.post("/api/v1/chat/999999/feedback", headers=headers, json={"rating": 4}).status_code
        == 404
    )


def test_bot_info(client: TestClient, user_auth: dict) -> None:
    info = client.get("/api/v1/bot/info", headers=user_auth["headers"]).json()
    assert info["mode"] == "rule"
    assert info["intents"] > 20
    assert info["llm_available"] is False
