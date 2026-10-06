import asyncio

import pytest

from app.chatbot.rule_based import RuleBasedBot, normalize


@pytest.fixture
def bot() -> RuleBasedBot:
    return RuleBasedBot()


def reply(bot: RuleBasedBot, message: str):
    return asyncio.run(bot.reply(message))


@pytest.mark.parametrize(
    ("message", "intent"),
    [
        ("Hello!", "greeting"),
        ("What is Docker?", "docker"),
        ("How do I write a docker compose file?", "docker_compose"),
        ("What is a Kubernetes deployment?", "k8s_deployment"),
        ("Explain the HPA in k8s", "autoscaling"),
        ("hi, what is redis?", "redis"),
        ("Tell me about CI/CD", "cicd"),
        ("liveness vs readiness probes", "probes"),
        ("thanks!", "thanks"),
    ],
)
def test_intent_matching(bot: RuleBasedBot, message: str, intent: str) -> None:
    assert reply(bot, message).intent == intent


def test_unknown_message_uses_fallback(bot: RuleBasedBot) -> None:
    result = reply(bot, "Recommend a good pizza topping")
    assert result.intent == "fallback"
    assert result.mode == "rule"
    assert result.text


def test_replies_are_deterministic(bot: RuleBasedBot) -> None:
    assert reply(bot, "hello").text == reply(bot, "  HELLO ").text


def test_time_answers_are_not_cacheable(bot: RuleBasedBot) -> None:
    result = reply(bot, "what time is it?")
    assert result.intent == "time"
    assert result.cacheable is False


def test_normalize() -> None:
    assert normalize("  What's   CI/CD?? ") == "what s cicd"
