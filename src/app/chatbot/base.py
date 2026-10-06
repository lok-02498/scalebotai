"""Chatbot engine interface (Strategy pattern).

The API depends only on ``BotEngine``; ``CHATBOT_MODE`` selects the
implementation at startup (rule-based or Ollama/TinyLlama).
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import NamedTuple


class ChatTurn(NamedTuple):
    question: str
    response: str


@dataclass(frozen=True, slots=True)
class BotReply:
    text: str
    mode: str  # "rule" | "llm" | "rule-fallback"
    intent: str | None = None
    # False for answers that depend on time or conversation context.
    cacheable: bool = True


class BotEngine(ABC):
    mode: str
    model: str | None = None
    uses_history: bool = False

    @abstractmethod
    async def reply(self, message: str, history: Sequence[ChatTurn] = ()) -> BotReply: ...

    @property
    @abstractmethod
    def intent_count(self) -> int: ...

    async def is_available(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None
