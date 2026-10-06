"""FastAPI dependencies: settings, resources, authentication, RBAC and rate limiting."""

import uuid
from typing import Annotated, Any

from fastapi import Depends, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.chatbot.base import BotEngine
from app.core.config import Settings
from app.core.context import user_id_ctx
from app.core.errors import ForbiddenError, RateLimitedError, UnauthorizedError
from app.core.metrics import RATE_LIMITED
from app.core.security import InvalidTokenError, decode_token
from app.db.models import User
from app.db.session import Database, get_session
from app.schemas.user import UserResponse
from app.services import rate_limiter
from app.services.cache import USER_CACHE_TTL, Cache, is_token_revoked, user_key

bearer_scheme = HTTPBearer(
    auto_error=False, description="Access token from POST /api/v1/auth/login"
)


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_redis(request: Request) -> Redis | None:
    return request.app.state.redis


def get_cache(request: Request) -> Cache:
    return Cache(request.app.state.redis)


def get_bot(request: Request) -> BotEngine:
    return request.app.state.bot


SettingsDep = Annotated[Settings, Depends(get_settings)]
DatabaseDep = Annotated[Database, Depends(get_db)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
RedisDep = Annotated[Redis | None, Depends(get_redis)]
CacheDep = Annotated[Cache, Depends(get_cache)]
BotDep = Annotated[BotEngine, Depends(get_bot)]


async def get_token_payload(
    settings: SettingsDep,
    redis: RedisDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> dict[str, Any]:
    if credentials is None:
        raise UnauthorizedError("Not authenticated")
    try:
        payload = decode_token(settings, credentials.credentials, "access")
    except InvalidTokenError as exc:
        raise UnauthorizedError(str(exc)) from exc
    if await is_token_revoked(redis, payload["jti"]):
        raise UnauthorizedError("Token has been revoked")
    return payload


TokenPayloadDep = Annotated[dict[str, Any], Depends(get_token_payload)]


async def get_current_user(
    request: Request, payload: TokenPayloadDep, session: SessionDep, cache: CacheDep
) -> UserResponse:
    user_id = payload["sub"]
    cached = await cache.get_json(user_key(user_id), "user")
    if cached:
        user = UserResponse.model_validate(cached)
    else:
        try:
            db_user = await session.get(User, uuid.UUID(user_id))
        except ValueError as exc:
            raise UnauthorizedError("Invalid token subject") from exc
        if db_user is None:
            raise UnauthorizedError("User no longer exists")
        user = UserResponse.model_validate(db_user)
        await cache.set_json(user_key(user_id), user.model_dump(mode="json"), USER_CACHE_TTL)
    if not user.is_active:
        raise ForbiddenError("Account is disabled")
    request.state.user_id = str(user.id)  # read by the access-log middleware
    user_id_ctx.set(str(user.id))
    return user


async def _enforce_rate_limit(request: Request, response: Response, identity: str) -> None:
    settings: Settings = request.app.state.settings
    if not settings.rate_limit_enabled:
        return
    result = await rate_limiter.hit(
        request.app.state.redis, identity, settings.rate_limit_per_minute
    )
    headers = {
        "X-RateLimit-Limit": str(result.limit),
        "X-RateLimit-Remaining": str(result.remaining),
    }
    if not result.allowed:
        RATE_LIMITED.inc()
        raise RateLimitedError(
            "Rate limit exceeded, retry later",
            headers={**headers, "Retry-After": str(result.retry_after)},
        )
    response.headers.update(headers)


async def rate_limit_by_ip(request: Request, response: Response) -> None:
    client_ip = request.client.host if request.client else "unknown"
    await _enforce_rate_limit(request, response, f"ip:{client_ip}")


async def rate_limited_user(
    request: Request,
    response: Response,
    user: Annotated[UserResponse, Depends(get_current_user)],
) -> UserResponse:
    await _enforce_rate_limit(request, response, f"user:{user.id}")
    return user


async def require_admin(
    user: Annotated[UserResponse, Depends(rate_limited_user)],
) -> UserResponse:
    if user.role != "admin":
        raise ForbiddenError("Admin privileges required")
    return user


CurrentUser = Annotated[UserResponse, Depends(rate_limited_user)]
AdminUser = Annotated[UserResponse, Depends(require_admin)]
