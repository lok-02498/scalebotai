import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Depends, status

from app.api.deps import (
    CurrentUser,
    RedisDep,
    SessionDep,
    SettingsDep,
    TokenPayloadDep,
    rate_limit_by_ip,
)
from app.core.errors import ForbiddenError, NotFoundError, UnauthorizedError
from app.core.logging import get_logger
from app.core.metrics import AUTH_EVENTS
from app.core.security import InvalidTokenError, decode_token, seconds_until
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    TokenResponse,
)
from app.schemas.common import MessageResponse, error_responses
from app.schemas.user import RegisterRequest, UserResponse
from app.services import user_service
from app.services.cache import Cache, is_token_revoked, revoke_token, user_key

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["Authentication"])
IpRateLimit = [Depends(rate_limit_by_ip)]


def _audit(event: str, message: str, **fields: object) -> None:
    logger.info(message, extra={"log_type": "user", "event": event, **fields})


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=IpRateLimit,
    responses=error_responses(409, 422, 429),
    summary="Register a new user",
)
async def register(
    body: RegisterRequest, session: SessionDep, settings: SettingsDep
) -> UserResponse:
    user = await user_service.create_user(session, body, settings.bcrypt_rounds)
    AUTH_EVENTS.labels("register").inc()
    _audit("register", "User registered", user_id=str(user.id), username=user.username)
    return UserResponse.model_validate(user)


@router.post(
    "/login",
    response_model=TokenResponse,
    dependencies=IpRateLimit,
    responses=error_responses(401, 403, 422, 429),
    summary="Log in with username or email",
)
async def login(body: LoginRequest, session: SessionDep, settings: SettingsDep) -> TokenResponse:
    try:
        user = await user_service.authenticate(
            session, body.username, body.password, settings.bcrypt_rounds
        )
    except (UnauthorizedError, ForbiddenError) as exc:
        AUTH_EVENTS.labels("login_failure").inc()
        _audit("login_failure", f"Login failed: {exc.message}", username=body.username)
        raise
    AUTH_EVENTS.labels("login_success").inc()
    _audit("login_success", "User logged in", user_id=str(user.id))
    return user_service.issue_tokens(settings, user)


@router.post(
    "/logout",
    response_model=MessageResponse,
    responses=error_responses(401, 429),
    summary="Revoke the current access token (and optionally a refresh token)",
)
async def logout(
    payload: TokenPayloadDep,
    user: CurrentUser,
    redis: RedisDep,
    settings: SettingsDep,
    body: Annotated[LogoutRequest | None, Body()] = None,
) -> MessageResponse:
    await revoke_token(redis, payload["jti"], seconds_until(payload["exp"]))
    if body is not None and body.refresh_token:
        try:
            refresh = decode_token(settings, body.refresh_token, "refresh")
            if refresh["sub"] == str(user.id):
                await revoke_token(redis, refresh["jti"], seconds_until(refresh["exp"]))
        except InvalidTokenError:
            pass  # an invalid refresh token is already unusable
    AUTH_EVENTS.labels("logout").inc()
    _audit("logout", "User logged out", user_id=str(user.id))
    return MessageResponse(message="Logged out successfully")


@router.post(
    "/refresh",
    response_model=TokenResponse,
    dependencies=IpRateLimit,
    responses=error_responses(401, 403, 422, 429),
    summary="Exchange a refresh token for a new token pair (rotation)",
)
async def refresh(
    body: RefreshRequest, session: SessionDep, settings: SettingsDep, redis: RedisDep
) -> TokenResponse:
    try:
        payload = decode_token(settings, body.refresh_token, "refresh")
    except InvalidTokenError as exc:
        raise UnauthorizedError(str(exc)) from exc
    if await is_token_revoked(redis, payload["jti"]):
        raise UnauthorizedError("Token has been revoked")
    try:
        user = await user_service.get_user(session, uuid.UUID(payload["sub"]))
    except (ValueError, NotFoundError) as exc:
        raise UnauthorizedError("User no longer exists") from exc
    if not user.is_active:
        raise ForbiddenError("Account is disabled")
    # Rotation: each refresh token works once.
    await revoke_token(redis, payload["jti"], seconds_until(payload["exp"]))
    AUTH_EVENTS.labels("refresh").inc()
    return user_service.issue_tokens(settings, user)


@router.post(
    "/change-password",
    response_model=MessageResponse,
    responses=error_responses(401, 422, 429),
    summary="Change the current user's password",
)
async def change_password(
    body: ChangePasswordRequest,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    redis: RedisDep,
) -> MessageResponse:
    await user_service.change_password(
        session, user.id, body.current_password, body.new_password, settings.bcrypt_rounds
    )
    await Cache(redis).delete(user_key(user.id))
    _audit("password_change", "Password changed", user_id=str(user.id))
    return MessageResponse(message="Password changed successfully")
