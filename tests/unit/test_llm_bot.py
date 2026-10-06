import asyncio
import json

import httpx

from app.chatbot.base import ChatTurn
from app.chatbot.llm import OllamaBot


def make_bot(handler, **kwargs) -> OllamaBot:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OllamaBot("http://ollama:11434", "tinyllama", timeout=1, client=client, **kwargs)


def test_successful_completion_sends_history() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"message": {"role": "assistant", "content": " Pods! "}})

    bot = make_bot(handler)
    result = asyncio.run(bot.reply("And pods?", [ChatTurn("What is k8s?", "An orchestrator.")]))

    assert result.text == "Pods!"
    assert result.mode == "llm"
    assert result.cacheable is False  # depends on conversation context
    assert seen["model"] == "tinyllama"
    assert [m["role"] for m in seen["messages"]] == ["system", "user", "assistant", "user"]


def test_failure_falls_back_to_rules() -> None:
    bot = make_bot(lambda request: httpx.Response(500))
    result = asyncio.run(bot.reply("What is Docker?"))
    assert result.mode == "rule-fallback"
    assert result.intent == "docker"
    assert result.cacheable is False


def test_circuit_breaker_skips_llm_after_repeated_failures() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("refused")

    bot = make_bot(handler, failure_threshold=2, cooldown_seconds=60)
    for _ in range(4):
        asyncio.run(bot.reply("hello"))
    assert calls == 2
    assert bot.circuit_open


def test_is_available_checks_model_list() -> None:
    bot = make_bot(lambda r: httpx.Response(200, json={"models": [{"name": "tinyllama:latest"}]}))
    assert asyncio.run(bot.is_available()) is True
    other = make_bot(lambda r: httpx.Response(200, json={"models": [{"name": "llama3:8b"}]}))
    assert asyncio.run(other.is_available()) is False
