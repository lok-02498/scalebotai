"""Async engine, connection pool and schema bootstrap."""

import asyncio
import time
from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.models import Base

logger = get_logger(__name__)

# Arbitrary constant shared by all replicas: only one pod creates tables at a time.
SCHEMA_LOCK_KEY = 815_100_001


class Database:
    def __init__(self, settings: Settings) -> None:
        url = settings.sqlalchemy_database_url
        self.is_sqlite = url.startswith("sqlite")
        kwargs: dict[str, object] = {"pool_pre_ping": True}
        if not self.is_sqlite:
            kwargs.update(
                pool_size=settings.db_pool_size,
                max_overflow=settings.db_max_overflow,
                pool_timeout=settings.db_pool_timeout,
                pool_recycle=1800,
            )
        self.engine = create_async_engine(url, **kwargs)
        self.pool_size = settings.db_pool_size if not self.is_sqlite else 1
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

        if self.is_sqlite:
            # SQLite needs this for ON DELETE CASCADE (used only by unit tests).
            @event.listens_for(self.engine.sync_engine, "connect")
            def _enable_fk(dbapi_connection, _record):  # type: ignore[no-untyped-def]
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

    async def create_tables(self, attempts: int = 10, delay: float = 3.0) -> None:
        """Create missing tables, retrying while the database is still starting."""
        for attempt in range(1, attempts + 1):
            try:
                async with self.engine.begin() as conn:
                    if conn.dialect.name == "postgresql":
                        # Transaction-scoped lock: released automatically on commit.
                        await conn.execute(
                            text("SELECT pg_advisory_xact_lock(:key)"), {"key": SCHEMA_LOCK_KEY}
                        )
                    await conn.run_sync(Base.metadata.create_all)
                logger.info(
                    "Database schema ready",
                    extra={"log_type": "system", "event": "db_schema_ready"},
                )
                return
            # asyncpg raises OSError, CannotConnectNowError ("starting up"), etc. while
            # PostgreSQL boots; retry everything and give up after the last attempt.
            except Exception as exc:
                if attempt == attempts:
                    raise
                logger.warning(
                    "Database not ready (attempt %d/%d): %s",
                    attempt,
                    attempts,
                    exc,
                    extra={"log_type": "system", "event": "db_retry"},
                )
                await asyncio.sleep(delay)

    async def ping(self) -> float:
        """Run ``SELECT 1`` and return the round-trip time in milliseconds."""
        start = time.perf_counter()
        async with self.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return (time.perf_counter() - start) * 1000

    def pool_status(self) -> dict[str, int]:
        pool = self.engine.pool
        checked_out = getattr(pool, "checkedout", lambda: 0)()
        overflow = max(getattr(pool, "overflow", lambda: 0)(), 0)
        return {"size": self.pool_size, "checked_out": checked_out, "overflow": overflow}

    async def dispose(self) -> None:
        await self.engine.dispose()


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request, always closed afterwards."""
    db: Database = request.app.state.db
    async with db.sessionmaker() as session:
        yield session
