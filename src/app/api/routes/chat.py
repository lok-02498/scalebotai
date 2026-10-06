import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from app.api.deps import BotDep, CacheDep, CurrentUser, SessionDep, SettingsDep
from app.schemas.chat import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ConversationCreate,
    ConversationDetail,
    ConversationResponse,
    ConversationUpdate,
    DeletedResponse,
    FeedbackRequest,
    FeedbackResponse,
)
from app.schemas.common import Page, PageParams, error_responses
from app.services import chat_service

router = APIRouter(prefix="/api/v1/chat", tags=["Chat"], responses=error_responses(401, 429))
Paging = Annotated[PageParams, Depends()]


@router.post(
    "",
    response_model=ChatResponse,
    responses=error_responses(404, 422),
    summary="Send a message (omit conversation_id to start a new chat)",
)
async def send_message(
    body: ChatRequest,
    user: CurrentUser,
    session: SessionDep,
    cache: CacheDep,
    bot: BotDep,
    settings: SettingsDep,
) -> ChatResponse:
    return await chat_service.send_message(
        session, cache, bot, user.id, body, settings.cache_ttl_seconds
    )


@router.post(
    "/conversations",
    response_model=ConversationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start a new, empty conversation",
)
async def create_conversation(
    user: CurrentUser, session: SessionDep, body: ConversationCreate | None = None
) -> ConversationResponse:
    return await chat_service.create_conversation(session, user.id, body.title if body else None)


@router.get(
    "/conversations",
    response_model=Page[ConversationResponse],
    summary="List conversations, most recently active first",
)
async def list_conversations(
    user: CurrentUser, session: SessionDep, paging: Paging
) -> Page[ConversationResponse]:
    return await chat_service.list_conversations(session, user.id, paging)


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationDetail,
    responses=error_responses(404),
    summary="Get a conversation with all its messages (to continue it)",
)
async def get_conversation(
    conversation_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> ConversationDetail:
    return await chat_service.get_conversation(session, user.id, conversation_id)


@router.patch(
    "/conversations/{conversation_id}",
    response_model=ConversationResponse,
    responses=error_responses(404, 422),
    summary="Rename a conversation",
)
async def rename_conversation(
    conversation_id: uuid.UUID, body: ConversationUpdate, user: CurrentUser, session: SessionDep
) -> ConversationResponse:
    return await chat_service.rename_conversation(session, user.id, conversation_id, body.title)


@router.delete(
    "/conversations/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses=error_responses(404),
    summary="Delete a conversation and its messages",
)
async def delete_conversation(
    conversation_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> None:
    await chat_service.delete_conversation(session, user.id, conversation_id)


@router.get(
    "/history",
    response_model=Page[ChatMessage],
    summary="Chat history, newest first (optionally for one conversation)",
)
async def get_history(
    user: CurrentUser,
    session: SessionDep,
    paging: Paging,
    conversation_id: Annotated[uuid.UUID | None, Query()] = None,
) -> Page[ChatMessage]:
    return await chat_service.history(session, user.id, paging, conversation_id)


@router.delete(
    "/history", response_model=DeletedResponse, summary="Delete all of the user's chat history"
)
async def delete_history(user: CurrentUser, session: SessionDep) -> DeletedResponse:
    return DeletedResponse(deleted=await chat_service.delete_history(session, user.id))


@router.post(
    "/{chat_id}/feedback",
    response_model=FeedbackResponse,
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(404, 422),
    summary="Rate a bot answer from 1 to 5 (re-rating replaces the previous rating)",
)
async def submit_feedback(
    chat_id: int, body: FeedbackRequest, user: CurrentUser, session: SessionDep
) -> FeedbackResponse:
    return await chat_service.submit_feedback(session, user.id, chat_id, body)
