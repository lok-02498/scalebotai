import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

BotMode = Literal["rule", "llm", "rule-fallback"]


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000, examples=["What is a Kubernetes pod?"])
    conversation_id: uuid.UUID | None = Field(
        default=None, description="Omit to start a new conversation"
    )

    @field_validator("message")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message must not be blank")
        return value


class ChatResponse(BaseModel):
    chat_id: int
    conversation_id: uuid.UUID
    question: str
    response: str
    bot_mode: BotMode
    cached: bool
    latency_ms: float
    timestamp: datetime


class FeedbackInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    rating: int
    comment: str | None


class ChatMessage(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    conversation_id: uuid.UUID
    question: str
    response: str
    bot_mode: str
    latency_ms: float | None
    timestamp: datetime
    feedback: FeedbackInfo | None = None


class ConversationCreate(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class ConversationUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class ConversationResponse(BaseModel):
    id: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime
    message_count: int


class ConversationDetail(BaseModel):
    id: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime
    messages: list[ChatMessage]


class DeletedResponse(BaseModel):
    deleted: int


class FeedbackRequest(BaseModel):
    rating: int = Field(ge=1, le=5)
    comment: str | None = Field(default=None, max_length=1000)


class FeedbackResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    chat_id: int
    rating: int
    comment: str | None
    created_at: datetime


class BotInfo(BaseModel):
    mode: Literal["rule", "llm"]
    model: str | None
    intents: int
    llm_available: bool
