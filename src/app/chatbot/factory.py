from app.chatbot.base import BotEngine
from app.chatbot.llm import OllamaBot
from app.chatbot.rule_based import RuleBasedBot
from app.core.config import Settings


def build_bot(settings: Settings) -> BotEngine:
    if settings.chatbot_mode == "llm":
        return OllamaBot(
            base_url=settings.ollama_base_url,
            model=settings.ollama_model,
            timeout=settings.ollama_timeout_seconds,
            fallback=RuleBasedBot(),
        )
    return RuleBasedBot()
