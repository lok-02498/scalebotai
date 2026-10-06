"""ScaleBotAI application factory.

Run locally:   uvicorn app.main:app --reload            (from the src/ directory)
In containers: uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --proxy-headers
"""

import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis

from app.api.routes import admin, auth, bot, chat, health, users
from app.chatbot.base import BotEngine
from app.chatbot.factory import build_bot
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import get_logger, setup_logging
from app.core.metrics import APP_INFO, DB_POOL_SIZE
from app.core.redis import create_redis
from app.db.session import Database
from app.middleware.request_context import RequestContextMiddleware
from app.services.cache import reset_redis_breaker
from app.services.user_service import ensure_admin

logger = get_logger("app")

OPENAPI_TAGS = [
    {"name": "Authentication", "description": "Register, log in, refresh and revoke JWTs."},
    {"name": "Users", "description": "The authenticated user's profile and account."},
    {"name": "Chat", "description": "Conversations, messages, history and feedback."},
    {"name": "Bot", "description": "Chatbot engine information."},
    {"name": "Admin", "description": "Analytics and user management (role: admin)."},
    {"name": "Operations", "description": "Health probes and Prometheus metrics."},
]


def create_app(
    settings: Settings | None = None,
    *,
    redis_factory: Callable[[Settings], Redis] = create_redis,
    bot_factory: Callable[[Settings], BotEngine] = build_bot,
) -> FastAPI:
    settings = settings or get_settings()
    setup_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.started_at = time.time()
        reset_redis_breaker()
        app.state.db = Database(settings)
        app.state.redis = redis_factory(settings)
        app.state.bot = bot_factory(settings)
        logger.info(
            "Starting %s %s (env=%s, bot=%s)",
            settings.app_name,
            settings.app_version,
            settings.app_env,
            settings.chatbot_mode,
            extra={"log_type": "system", "event": "startup"},
        )
        if settings.uses_insecure_secret:
            logger.warning(
                "JWT_SECRET_KEY is the default or shorter than 32 characters - set a strong secret",
                extra={"log_type": "system", "event": "insecure_config"},
            )
        if settings.auto_create_tables:
            await app.state.db.create_tables()
        async with app.state.db.sessionmaker() as session:
            await ensure_admin(session, settings)
        APP_INFO.labels(settings.app_version, settings.chatbot_mode, settings.app_env).set(1)
        DB_POOL_SIZE.set(app.state.db.pool_size)
        logger.info("Startup complete", extra={"log_type": "system", "event": "startup_complete"})
        try:
            yield
        finally:
            logger.info("Shutting down", extra={"log_type": "system", "event": "shutdown"})
            await app.state.bot.aclose()
            await app.state.redis.aclose()
            await app.state.db.dispose()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "Cloud-native, horizontally scalable chatbot backend. Authenticate with "
            "`POST /api/v1/auth/login`, then click **Authorize** and paste the access token."
        ),
        openapi_tags=OPENAPI_TAGS,
        lifespan=lifespan,
    )
    app.state.settings = settings

    origins = settings.cors_origin_list
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials="*" not in origins,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=[
            "X-Request-ID",
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining",
            "Retry-After",
        ],
    )
    app.add_middleware(RequestContextMiddleware)  # added last = outermost
    register_exception_handlers(app)

    for router in (health.router, auth.router, users.router, chat.router, bot.router, admin.router):
        app.include_router(router)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {"name": settings.app_name, "version": settings.app_version, "docs": "/docs"}

    return app


app = create_app()
