import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, or_, select

from app.api.deps import (
    BotDep,
    CacheDep,
    DatabaseDep,
    RedisDep,
    SessionDep,
    SettingsDep,
    require_admin,
)
from app.core.logging import get_logger
from app.db.models import User
from app.schemas.admin import (
    AdminUserUpdate,
    Analytics,
    ChatStats,
    DailyRequests,
    FeedbackSummary,
    HealthReport,
    SystemStats,
    UserStats,
)
from app.schemas.common import Page, PageParams, error_responses
from app.schemas.user import UserResponse
from app.services import analytics_service, user_service
from app.services.cache import user_key

logger = get_logger(__name__)

router = APIRouter(
    prefix="/api/v1/admin",
    tags=["Admin"],
    dependencies=[Depends(require_admin)],
    responses=error_responses(401, 403, 429),
)


@router.get("/analytics", response_model=Analytics, summary="Dashboard summary (cached 30 s)")
async def get_analytics(session: SessionDep, cache: CacheDep, redis: RedisDep) -> Analytics:
    return await analytics_service.analytics(session, cache, redis)


@router.get("/stats/users", response_model=UserStats, summary="User totals")
async def get_user_stats(session: SessionDep) -> UserStats:
    return await analytics_service.user_stats(session)


@router.get("/stats/chats", response_model=ChatStats, summary="Chat totals")
async def get_chat_stats(session: SessionDep) -> ChatStats:
    return await analytics_service.chat_stats(session)


@router.get("/stats/requests", response_model=DailyRequests, summary="Daily API request counts")
async def get_daily_requests(
    redis: RedisDep, days: Annotated[int, Query(ge=1, le=30)] = 7
) -> DailyRequests:
    return await analytics_service.daily_requests(redis, days)


@router.get("/system", response_model=SystemStats, summary="Process, pool and Redis statistics")
async def get_system_stats(
    request: Request, settings: SettingsDep, db: DatabaseDep, redis: RedisDep
) -> SystemStats:
    return await analytics_service.system_stats(settings, db, redis, request.app.state.started_at)


@router.get("/health", response_model=HealthReport, summary="Detailed dependency health")
async def get_health(
    request: Request, settings: SettingsDep, db: DatabaseDep, redis: RedisDep, bot: BotDep
) -> HealthReport:
    return await analytics_service.health_report(
        settings, db, redis, bot, request.app.state.started_at
    )


@router.get("/users", response_model=Page[UserResponse], summary="List users")
async def list_users(
    session: SessionDep,
    paging: Annotated[PageParams, Depends()],
    search: Annotated[str | None, Query(max_length=100)] = None,
) -> Page[UserResponse]:
    filters = []
    if search:
        pattern = f"%{search.lower()}%"
        filters.append(or_(func.lower(User.username).like(pattern), User.email.like(pattern)))
    total = await session.scalar(select(func.count(User.id)).where(*filters))
    users = await session.scalars(
        select(User)
        .where(*filters)
        .order_by(User.created_at.desc())
        .offset(paging.offset)
        .limit(paging.size)
    )
    items = [UserResponse.model_validate(user) for user in users]
    return Page.build(items, page=paging.page, size=paging.size, total=total or 0)


@router.patch(
    "/users/{user_id}",
    response_model=UserResponse,
    responses=error_responses(404, 422),
    summary="Activate/deactivate a user or change their role",
)
async def update_user(
    user_id: uuid.UUID,
    body: AdminUserUpdate,
    admin: Annotated[UserResponse, Depends(require_admin)],
    session: SessionDep,
    cache: CacheDep,
) -> UserResponse:
    user = await user_service.get_user(session, user_id)
    if body.is_active is not None:
        user.is_active = body.is_active
    if body.role is not None:
        user.role = body.role
    await session.commit()
    await cache.delete(user_key(user_id))  # takes effect on the user's next request
    logger.info(
        "Admin updated user",
        extra={
            "log_type": "user",
            "event": "admin_user_update",
            "target_user_id": str(user_id),
            "changes": body.model_dump(exclude_none=True),
            "admin_id": str(admin.id),
        },
    )
    return UserResponse.model_validate(user)


@router.get("/feedback", response_model=FeedbackSummary, summary="Feedback rating summary")
async def get_feedback_summary(session: SessionDep) -> FeedbackSummary:
    return await analytics_service.feedback_summary(session)
