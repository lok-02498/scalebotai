from fastapi import APIRouter, Response, status

from app.api.deps import CacheDep, CurrentUser, RedisDep, SessionDep, TokenPayloadDep
from app.core.logging import get_logger
from app.core.security import seconds_until
from app.schemas.common import error_responses
from app.schemas.user import UserResponse, UserStatsResponse, UserUpdateRequest
from app.services import user_service
from app.services.cache import revoke_token, user_key

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/users", tags=["Users"], responses=error_responses(401, 429))


@router.get("/me", response_model=UserResponse, summary="Get the current user's profile")
async def get_me(user: CurrentUser) -> UserResponse:
    return user


@router.put(
    "/me",
    response_model=UserResponse,
    responses=error_responses(409, 422),
    summary="Update email and/or full name",
)
async def update_me(
    body: UserUpdateRequest, user: CurrentUser, session: SessionDep, cache: CacheDep
) -> UserResponse:
    updated = await user_service.update_profile(session, user.id, body)
    await cache.delete(user_key(user.id))
    logger.info(
        "Profile updated",
        extra={"log_type": "user", "event": "profile_update", "user_id": str(user.id)},
    )
    return UserResponse.model_validate(updated)


@router.delete(
    "/me",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete the account and all of its chat data",
)
async def delete_me(
    user: CurrentUser,
    payload: TokenPayloadDep,
    session: SessionDep,
    cache: CacheDep,
    redis: RedisDep,
) -> None:
    await user_service.delete_user(session, user.id)
    await cache.delete(user_key(user.id))
    await revoke_token(redis, payload["jti"], seconds_until(payload["exp"]))
    logger.info(
        "Account deleted",
        extra={"log_type": "user", "event": "account_delete", "user_id": str(user.id)},
    )


@router.get("/me/stats", response_model=UserStatsResponse, summary="Usage statistics")
async def my_stats(user: CurrentUser, session: SessionDep) -> UserStatsResponse:
    return await user_service.user_stats(session, user.id)
