"""Admin analytics, system statistics and dependency health."""

import os
import platform
import socket
import time
from datetime import UTC, datetime, timedelta

import psutil
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chatbot.base import BotEngine
from app.core.config import Settings
from app.db.models import Chat, Conversation, Feedback, User
from app.db.session import Database
from app.schemas.admin import (
    Analytics,
    ChatbotHealth,
    ChatStats,
    DailyRequestPoint,
    DailyRequests,
    DependencyHealth,
    FeedbackSummary,
    HealthChecks,
    HealthReport,
    PoolStats,
    ProcessStats,
    RedisStats,
    SystemStats,
    UserStats,
)
from app.services.cache import (
    ANALYTICS_CACHE_TTL,
    ANALYTICS_KEY,
    REDIS_ERRORS,
    Cache,
    daily_requests_key,
    log_redis_error,
    redis_usable,
)

_process = psutil.Process()
_process.cpu_percent(None)  # prime: the first call always returns 0.0


def _start_of_today() -> datetime:
    return datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


async def user_stats(session: AsyncSession) -> UserStats:
    today = _start_of_today()
    week_ago = datetime.now(UTC) - timedelta(days=7)
    row = (
        await session.execute(
            select(
                func.count(User.id),
                func.count(User.id).filter(User.is_active.is_(True)),
                func.count(User.id).filter(User.role == "admin"),
                func.count(User.id).filter(User.created_at >= today),
                func.count(User.id).filter(User.created_at >= week_ago),
            )
        )
    ).one()
    return UserStats(
        total_users=row[0],
        active_users=row[1],
        admin_users=row[2],
        new_users_today=row[3],
        new_users_7d=row[4],
    )


async def chat_stats(session: AsyncSession) -> ChatStats:
    total_chats, chats_today, avg_latency = (
        await session.execute(
            select(
                func.count(Chat.id),
                func.count(Chat.id).filter(Chat.timestamp >= _start_of_today()),
                func.avg(Chat.latency_ms),
            )
        )
    ).one()
    total_conversations = await session.scalar(select(func.count(Conversation.id))) or 0
    total_users = await session.scalar(select(func.count(User.id))) or 0
    return ChatStats(
        total_chats=total_chats,
        total_conversations=total_conversations,
        chats_today=chats_today,
        avg_chats_per_user=round(total_chats / total_users, 2) if total_users else 0.0,
        avg_bot_latency_ms=round(float(avg_latency), 2) if avg_latency is not None else None,
    )


async def daily_requests(redis: Redis | None, days: int) -> DailyRequests:
    today = datetime.now(UTC).date()
    dates = [today - timedelta(days=offset) for offset in range(days - 1, -1, -1)]
    values: list[str | None] = [None] * len(dates)
    if redis_usable(redis):
        try:
            values = await redis.mget([daily_requests_key(day) for day in dates])
        except REDIS_ERRORS as exc:
            log_redis_error(exc, "daily requests")
    series = [
        DailyRequestPoint(date=day, requests=int(value or 0))
        for day, value in zip(dates, values, strict=True)
    ]
    return DailyRequests(days=days, total=sum(p.requests for p in series), series=series)


async def feedback_summary(session: AsyncSession) -> FeedbackSummary:
    rows = await session.execute(
        select(Feedback.rating, func.count(Feedback.id)).group_by(Feedback.rating)
    )
    distribution = {str(rating): 0 for rating in range(1, 6)}
    for rating, count in rows.all():
        distribution[str(rating)] = count
    total = sum(distribution.values())
    weighted = sum(int(rating) * count for rating, count in distribution.items())
    return FeedbackSummary(
        total=total,
        average_rating=round(weighted / total, 2) if total else None,
        distribution=distribution,
    )


async def analytics(session: AsyncSession, cache: Cache, redis: Redis | None) -> Analytics:
    cached = await cache.get_json(ANALYTICS_KEY, "analytics")
    if cached:
        return Analytics.model_validate(cached)
    result = Analytics(
        users=await user_stats(session),
        chats=await chat_stats(session),
        requests=await daily_requests(redis, 7),
        feedback=await feedback_summary(session),
        generated_at=datetime.now(UTC),
    )
    await cache.set_json(ANALYTICS_KEY, result.model_dump(mode="json"), ANALYTICS_CACHE_TTL)
    return result


async def _redis_stats(redis: Redis | None) -> RedisStats:
    if not redis_usable(redis):
        return RedisStats(connected=False)
    try:
        await redis.ping()
    except REDIS_ERRORS as exc:
        log_redis_error(exc, "ping")
        return RedisStats(connected=False)
    try:
        info = await redis.info()
    except REDIS_ERRORS:  # INFO may be disabled (some managed Redis services)
        return RedisStats(connected=True)
    hits = int(info.get("keyspace_hits", 0))
    misses = int(info.get("keyspace_misses", 0))
    return RedisStats(
        connected=True,
        used_memory_human=info.get("used_memory_human"),
        connected_clients=info.get("connected_clients"),
        keyspace_hit_ratio=round(hits / (hits + misses), 4) if hits + misses else None,
    )


async def system_stats(
    settings: Settings, db: Database, redis: Redis | None, started_at: float
) -> SystemStats:
    with _process.oneshot():
        process = ProcessStats(
            cpu_percent=_process.cpu_percent(None),
            memory_rss_mb=round(_process.memory_info().rss / 1024 / 1024, 2),
            threads=_process.num_threads(),
        )
    return SystemStats(
        hostname=os.getenv("POD_NAME") or socket.gethostname(),
        app_version=settings.app_version,
        python_version=platform.python_version(),
        environment=settings.app_env,
        bot_mode=settings.chatbot_mode,
        uptime_seconds=round(time.time() - started_at, 1),
        process=process,
        db_pool=PoolStats(**db.pool_status()),
        redis=await _redis_stats(redis),
    )


async def check_database(db: Database) -> DependencyHealth:
    try:
        return DependencyHealth(status="ok", latency_ms=round(await db.ping(), 2))
    except Exception as exc:
        return DependencyHealth(status="down", error=type(exc).__name__)


async def check_redis(redis: Redis | None) -> DependencyHealth:
    if redis is None:
        return DependencyHealth(status="down", error="not configured")
    if not redis_usable(redis):
        # Breaker open: answer instantly so health/readiness probes never wait on Redis.
        return DependencyHealth(status="down", error="circuit open")
    start = time.perf_counter()
    try:
        await redis.ping()
    except REDIS_ERRORS as exc:
        log_redis_error(exc, "health check")
        return DependencyHealth(status="down", error=type(exc).__name__)
    return DependencyHealth(status="ok", latency_ms=round((time.perf_counter() - start) * 1000, 2))


async def health_report(
    settings: Settings, db: Database, redis: Redis | None, bot: BotEngine, started_at: float
) -> HealthReport:
    database = await check_database(db)
    cache = await check_redis(redis)
    bot_ok = await bot.is_available()
    # The LLM falls back to rules, so an unavailable model degrades rather than breaks.
    chatbot = ChatbotHealth(status="ok" if bot_ok else "degraded", mode=settings.chatbot_mode)
    if database.status == "down":
        status = "down"
    elif cache.status == "down" or chatbot.status != "ok":
        status = "degraded"  # API keeps serving: Redis is fail-open
    else:
        status = "ok"
    return HealthReport(
        status=status,
        version=settings.app_version,
        uptime_seconds=round(time.time() - started_at, 1),
        checks=HealthChecks(database=database, redis=cache, chatbot=chatbot),
    )
