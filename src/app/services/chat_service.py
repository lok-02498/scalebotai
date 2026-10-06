"""Conversations, chat messages, history and feedback."""

import time
import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.chatbot.base import BotEngine, BotReply, ChatTurn
from app.core.errors import NotFoundError
from app.core.metrics import BOT_LATENCY, CHAT_MESSAGES
from app.db.models import Chat, Conversation, Feedback, utcnow
from app.schemas.chat import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ConversationDetail,
    ConversationResponse,
    FeedbackRequest,
    FeedbackResponse,
)
from app.schemas.common import Page, PageParams
from app.services.cache import Cache, bot_response_key

HISTORY_TURNS = 5
TITLE_LENGTH = 60


def _title_from(message: str) -> str:
    title = " ".join(message.split())
    return title if len(title) <= TITLE_LENGTH else title[: TITLE_LENGTH - 1].rstrip() + "…"


async def _owned_conversation(
    session: AsyncSession, user_id: uuid.UUID, conversation_id: uuid.UUID
) -> Conversation:
    conversation = await session.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id, Conversation.user_id == user_id
        )
    )
    if conversation is None:
        raise NotFoundError("Conversation not found")
    return conversation


async def _answer(
    cache: Cache, bot: BotEngine, message: str, history: list[ChatTurn], ttl: int
) -> tuple[BotReply, bool]:
    """Cache-aside: Redis first, the bot engine on a miss."""
    key = None if history else bot_response_key(bot.mode, message)
    if key:
        hit = await cache.get_json(key, "bot_response")
        if hit:
            return BotReply(hit["text"], hit["mode"], hit.get("intent")), True
    start = time.perf_counter()
    reply = await bot.reply(message, history)
    BOT_LATENCY.labels(reply.mode).observe(time.perf_counter() - start)
    if key and reply.cacheable:
        await cache.set_json(
            key, {"text": reply.text, "mode": reply.mode, "intent": reply.intent}, ttl
        )
    return reply, False


async def send_message(
    session: AsyncSession,
    cache: Cache,
    bot: BotEngine,
    user_id: uuid.UUID,
    request: ChatRequest,
    cache_ttl: int,
) -> ChatResponse:
    start = time.perf_counter()
    history: list[ChatTurn] = []
    if request.conversation_id is not None:
        conversation = await _owned_conversation(session, user_id, request.conversation_id)
        if bot.uses_history:
            rows = await session.execute(
                select(Chat.question, Chat.response)
                .where(Chat.conversation_id == conversation.id)
                .order_by(Chat.timestamp.desc(), Chat.id.desc())
                .limit(HISTORY_TURNS)
            )
            history = [ChatTurn(q, r) for q, r in reversed(rows.all())]
    else:
        conversation = Conversation(
            id=uuid.uuid4(), user_id=user_id, title=_title_from(request.message)
        )
        session.add(conversation)

    reply, cached = await _answer(cache, bot, request.message, history, cache_ttl)
    latency_ms = round((time.perf_counter() - start) * 1000, 2)

    chat = Chat(
        user_id=user_id,
        conversation_id=conversation.id,
        question=request.message,
        response=reply.text,
        bot_mode=reply.mode,
        latency_ms=latency_ms,
        timestamp=utcnow(),
    )
    conversation.updated_at = chat.timestamp
    session.add(chat)
    await session.commit()

    CHAT_MESSAGES.labels(reply.mode, str(cached).lower()).inc()
    return ChatResponse(
        chat_id=chat.id,
        conversation_id=conversation.id,
        question=chat.question,
        response=chat.response,
        bot_mode=reply.mode,  # type: ignore[arg-type]
        cached=cached,
        latency_ms=latency_ms,
        timestamp=chat.timestamp,
    )


def _message_count():  # type: ignore[no-untyped-def]
    # Correlated subquery served by ix_chats_conversation_timestamp.
    return (
        select(func.count(Chat.id))
        .where(Chat.conversation_id == Conversation.id)
        .correlate(Conversation)
        .scalar_subquery()
    )


def _conversation_response(conversation: Conversation, count: int) -> ConversationResponse:
    return ConversationResponse(
        id=conversation.id,
        title=conversation.title,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        message_count=count,
    )


async def create_conversation(
    session: AsyncSession, user_id: uuid.UUID, title: str | None
) -> ConversationResponse:
    conversation = Conversation(id=uuid.uuid4(), user_id=user_id, title=title or "New chat")
    session.add(conversation)
    await session.commit()
    return _conversation_response(conversation, 0)


async def list_conversations(
    session: AsyncSession, user_id: uuid.UUID, params: PageParams
) -> Page[ConversationResponse]:
    total = await session.scalar(
        select(func.count(Conversation.id)).where(Conversation.user_id == user_id)
    )
    rows = await session.execute(
        select(Conversation, _message_count())
        .where(Conversation.user_id == user_id)
        .order_by(Conversation.updated_at.desc())
        .offset(params.offset)
        .limit(params.size)
    )
    items = [_conversation_response(conv, count) for conv, count in rows.all()]
    return Page.build(items, page=params.page, size=params.size, total=total or 0)


async def get_conversation(
    session: AsyncSession, user_id: uuid.UUID, conversation_id: uuid.UUID
) -> ConversationDetail:
    conversation = await _owned_conversation(session, user_id, conversation_id)
    chats = await session.scalars(
        select(Chat)
        .options(selectinload(Chat.feedback))
        .where(Chat.conversation_id == conversation.id)
        .order_by(Chat.timestamp, Chat.id)
    )
    return ConversationDetail(
        id=conversation.id,
        title=conversation.title,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        messages=[ChatMessage.model_validate(chat) for chat in chats],
    )


async def rename_conversation(
    session: AsyncSession, user_id: uuid.UUID, conversation_id: uuid.UUID, title: str
) -> ConversationResponse:
    conversation = await _owned_conversation(session, user_id, conversation_id)
    conversation.title = title
    await session.commit()
    count = await session.scalar(
        select(func.count(Chat.id)).where(Chat.conversation_id == conversation_id)
    )
    return _conversation_response(conversation, count or 0)


async def delete_conversation(
    session: AsyncSession, user_id: uuid.UUID, conversation_id: uuid.UUID
) -> None:
    result = await session.execute(
        delete(Conversation).where(
            Conversation.id == conversation_id, Conversation.user_id == user_id
        )
    )
    if result.rowcount == 0:
        raise NotFoundError("Conversation not found")
    await session.commit()


async def history(
    session: AsyncSession,
    user_id: uuid.UUID,
    params: PageParams,
    conversation_id: uuid.UUID | None = None,
) -> Page[ChatMessage]:
    filters = [Chat.user_id == user_id]
    if conversation_id is not None:
        filters.append(Chat.conversation_id == conversation_id)
    total = await session.scalar(select(func.count(Chat.id)).where(*filters))
    chats = await session.scalars(
        select(Chat)
        .options(selectinload(Chat.feedback))
        .where(*filters)
        .order_by(Chat.timestamp.desc(), Chat.id.desc())
        .offset(params.offset)
        .limit(params.size)
    )
    items = [ChatMessage.model_validate(chat) for chat in chats]
    return Page.build(items, page=params.page, size=params.size, total=total or 0)


async def delete_history(session: AsyncSession, user_id: uuid.UUID) -> int:
    deleted = await session.execute(delete(Chat).where(Chat.user_id == user_id))
    await session.execute(delete(Conversation).where(Conversation.user_id == user_id))
    await session.commit()
    return deleted.rowcount or 0


async def submit_feedback(
    session: AsyncSession, user_id: uuid.UUID, chat_id: int, data: FeedbackRequest
) -> FeedbackResponse:
    """Create or replace the feedback for one of the user's chat messages."""
    for attempt in range(2):
        chat = await session.scalar(
            select(Chat)
            .options(selectinload(Chat.feedback))
            .where(Chat.id == chat_id, Chat.user_id == user_id)
        )
        if chat is None:
            raise NotFoundError("Chat message not found")
        feedback = chat.feedback
        if feedback is None:
            feedback = Feedback(chat_id=chat.id, rating=data.rating, comment=data.comment)
            session.add(feedback)
        else:
            feedback.rating = data.rating
            feedback.comment = data.comment
        try:
            await session.commit()
            return FeedbackResponse.model_validate(feedback)
        except IntegrityError:
            # A concurrent request inserted feedback first; retry as an update.
            await session.rollback()
            session.expunge_all()
            if attempt == 1:
                raise
    raise AssertionError("unreachable")
