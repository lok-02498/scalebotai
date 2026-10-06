"""Fail-open Redis helpers: cache-aside, JWT denylist and request counters.

Redis errors never fail a request: the caller carries on as if the key were
missing. The first error also opens a circuit breaker for a few seconds;
while it is open every helper skips Redis immediately, so an outage costs
no latency (one request would otherwise wait on several connect timeouts).
The first call after the cooldown probes Redis again. Errors are logged at
most once every 10 seconds so an outage does not flood the logs.
"""

import hashlib
import json
import time
from datetime import UTC, date, datetime
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.chatbot.rule_based import normalize
from app.core.logging import get_logger
from app.core.metrics import CACHE_HITS, CACHE_MISSES

logger = get_logger(__name__)

USER_CACHE_TTL = 300
ANALYTICS_CACHE_TTL = 30
DAILY_COUNTER_TTL = 35 * 24 * 3600
ANALYTICS_KEY = "analytics:summary"
REDIS_ERRORS = (RedisError, OSError)
BREAKER_COOLDOWN_SECONDS = 5.0

_last_error_log = 0.0
_breaker_open_until = 0.0


def reset_redis_breaker() -> None:
    global _last_error_log, _breaker_open_until
    _last_error_log = _breaker_open_until = 0.0


def redis_usable(redis: Redis | None) -> bool:
    return redis is not None and time.monotonic() >= _breaker_open_until


def log_redis_error(exc: Exception, operation: str) -> None:
    """Open the circuit breaker and log the failure (rate-limited)."""
    global _last_error_log, _breaker_open_until
    now = time.monotonic()
    _breaker_open_until = now + BREAKER_COOLDOWN_SECONDS
    if now - _last_error_log >= 10:
        _last_error_log = now
        logger.error(
            "Redis unavailable during %s, continuing without it: %s",
            operation,
            exc,
            extra={"log_type": "error", "event": "redis_unavailable"},
        )


def bot_response_key(mode: str, message: str) -> str:
    digest = hashlib.sha256(normalize(message).encode()).hexdigest()
    return f"bot:resp:{mode}:{digest}"


def user_key(user_id: object) -> str:
    return f"user:{user_id}"


def denylist_key(jti: str) -> str:
    return f"jwt:denylist:{jti}"


def daily_requests_key(day: date) -> str:
    return f"stats:requests:{day.isoformat()}"


class Cache:
    def __init__(self, redis: Redis | None) -> None:
        self.redis = redis

    async def get_json(self, key: str, cache_name: str) -> Any | None:
        raw = None
        if redis_usable(self.redis):
            try:
                raw = await self.redis.get(key)
            except REDIS_ERRORS as exc:
                log_redis_error(exc, "cache get")
        if raw is None:
            CACHE_MISSES.labels(cache_name).inc()
            return None
        CACHE_HITS.labels(cache_name).inc()
        return json.loads(raw)

    async def set_json(self, key: str, value: Any, ttl: int) -> None:
        if not redis_usable(self.redis):
            return
        try:
            await self.redis.set(key, json.dumps(value, default=str), ex=ttl)
        except REDIS_ERRORS as exc:
            log_redis_error(exc, "cache set")

    async def delete(self, *keys: str) -> None:
        if not redis_usable(self.redis) or not keys:
            return
        try:
            await self.redis.delete(*keys)
        except REDIS_ERRORS as exc:
            log_redis_error(exc, "cache delete")


async def revoke_token(redis: Redis | None, jti: str, ttl_seconds: int) -> None:
    if not redis_usable(redis):
        return
    try:
        await redis.set(denylist_key(jti), "1", ex=max(ttl_seconds, 1))
    except REDIS_ERRORS as exc:
        log_redis_error(exc, "token revoke")


async def is_token_revoked(redis: Redis | None, jti: str) -> bool:
    if not redis_usable(redis):
        return False
    try:
        return bool(await redis.exists(denylist_key(jti)))
    except REDIS_ERRORS as exc:
        log_redis_error(exc, "denylist check")
        return False


async def increment_daily_requests(redis: Redis | None) -> None:
    if not redis_usable(redis):
        return
    key = daily_requests_key(datetime.now(UTC).date())
    try:
        async with redis.pipeline(transaction=False) as pipe:
            pipe.incr(key)
            pipe.expire(key, DAILY_COUNTER_TTL)
            await pipe.execute()
    except REDIS_ERRORS as exc:
        log_redis_error(exc, "request counter")
