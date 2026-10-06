"""User accounts: registration, authentication, profile and token issuing."""

import uuid
from functools import lru_cache

from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.config import Settings
from app.core.errors import ConflictError, ForbiddenError, NotFoundError, UnauthorizedError
from app.core.logging import get_logger
from app.core.security import (
    create_token,
    hash_password,
    hash_password_async,
    verify_password_async,
)
from app.db.models import Chat, Conversation, Feedback, User, utcnow
from app.schemas.auth import TokenResponse
from app.schemas.user import RegisterRequest, UserResponse, UserStatsResponse, UserUpdateRequest

logger = get_logger(__name__)


@lru_cache
def _dummy_hash(rounds: int) -> str:
    return hash_password("timing-equalizer-not-a-real-password", rounds)


async def create_user(
    session: AsyncSession, data: RegisterRequest, rounds: int, role: str = "user"
) -> User:
    taken = await session.execute(
        select(User.username, User.email).where(
            or_(User.username == data.username, User.email == data.email)
        )
    )
    for username, _ in taken.all():
        if username == data.username:
            raise ConflictError("Username is already taken")
        raise ConflictError("Email is already registered")

    user = User(
        id=uuid.uuid4(),
        username=data.username,
        email=data.email,
        password_hash=await hash_password_async(data.password, rounds),
        full_name=data.full_name,
        role=role,
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError as exc:  # lost a race with a concurrent registration
        await session.rollback()
        raise ConflictError("Username or email is already registered") from exc
    return user


async def authenticate(session: AsyncSession, login: str, password: str, rounds: int) -> User:
    user = await session.scalar(
        select(User).where(or_(User.username == login, User.email == login.lower()))
    )
    if user is None:
        # Spend the same time as a real check so response timing doesn't
        # reveal which usernames exist.
        await verify_password_async(password, await run_in_threadpool(_dummy_hash, rounds))
        raise UnauthorizedError("Invalid username or password")
    if not await verify_password_async(password, user.password_hash):
        raise UnauthorizedError("Invalid username or password")
    if not user.is_active:
        raise ForbiddenError("Account is disabled")
    user.last_login_at = utcnow()
    await session.commit()
    return user


def issue_tokens(settings: Settings, user: User) -> TokenResponse:
    access = create_token(settings, subject=str(user.id), role=user.role, token_type="access")
    refresh = create_token(settings, subject=str(user.id), role=user.role, token_type="refresh")
    return TokenResponse(
        access_token=access.token,
        refresh_token=refresh.token,
        expires_in=settings.access_token_expire_minutes * 60,
        user=UserResponse.model_validate(user),
    )


async def get_user(session: AsyncSession, user_id: uuid.UUID) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise NotFoundError("User not found")
    return user


async def update_profile(
    session: AsyncSession, user_id: uuid.UUID, data: UserUpdateRequest
) -> User:
    user = await get_user(session, user_id)
    if data.email is not None and data.email != user.email:
        clash = await session.scalar(select(User.id).where(User.email == data.email))
        if clash is not None:
            raise ConflictError("Email is already registered")
        user.email = data.email
    if data.full_name is not None:
        user.full_name = data.full_name
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError("Email is already registered") from exc
    return user


async def change_password(
    session: AsyncSession, user_id: uuid.UUID, current: str, new: str, rounds: int
) -> None:
    user = await get_user(session, user_id)
    if not await verify_password_async(current, user.password_hash):
        raise UnauthorizedError("Current password is incorrect")
    user.password_hash = await hash_password_async(new, rounds)
    await session.commit()


async def delete_user(session: AsyncSession, user_id: uuid.UUID) -> None:
    # ON DELETE CASCADE removes conversations, chats and feedback in the database.
    result = await session.execute(delete(User).where(User.id == user_id))
    if result.rowcount == 0:
        raise NotFoundError("User not found")
    await session.commit()


async def user_stats(session: AsyncSession, user_id: uuid.UUID) -> UserStatsResponse:
    conversations = await session.scalar(
        select(func.count(Conversation.id)).where(Conversation.user_id == user_id)
    )
    messages, first, last = (
        await session.execute(
            select(func.count(Chat.id), func.min(Chat.timestamp), func.max(Chat.timestamp)).where(
                Chat.user_id == user_id
            )
        )
    ).one()
    feedback = await session.scalar(
        select(func.count(Feedback.id)).join(Chat).where(Chat.user_id == user_id)
    )
    return UserStatsResponse(
        total_conversations=conversations or 0,
        total_messages=messages or 0,
        feedback_given=feedback or 0,
        first_chat_at=first,
        last_chat_at=last,
    )


async def ensure_admin(session: AsyncSession, settings: Settings) -> None:
    """Create the bootstrap admin from ADMIN_* settings if it does not exist."""
    if not settings.admin_password:
        return
    existing = await session.scalar(select(User).where(User.username == settings.admin_username))
    if existing is not None:
        if existing.role != "admin":
            # Never silently promote an account someone else registered.
            logger.warning(
                "Bootstrap admin username is taken by a non-admin account; not promoting it",
                extra={"log_type": "system", "event": "admin_bootstrap_skipped"},
            )
        return
    session.add(
        User(
            id=uuid.uuid4(),
            username=settings.admin_username,
            email=settings.admin_email.lower(),
            password_hash=await hash_password_async(
                settings.admin_password, settings.bcrypt_rounds
            ),
            full_name="Platform Administrator",
            role="admin",
        )
    )
    try:
        await session.commit()
        logger.info(
            "Bootstrap admin created", extra={"log_type": "system", "event": "admin_bootstrap"}
        )
    except IntegrityError:  # another replica created it first
        await session.rollback()
