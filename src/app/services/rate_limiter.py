"""Fixed-window rate limiter shared by all replicas through Redis.

Key ``ratelimit:{identity}:{epoch_minute}`` is incremented on every request
and expires after the window, so every pod sees the same count.
"""

import time
from dataclasses import dataclass

from redis.asyncio import Redis

from app.services.cache import REDIS_ERRORS, log_redis_error, redis_usable

WINDOW_SECONDS = 60


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int


async def hit(redis: Redis | None, identity: str, limit: int) -> RateLimitResult:
    now = int(time.time())
    window = now // WINDOW_SECONDS
    retry_after = WINDOW_SECONDS - now % WINDOW_SECONDS
    if not redis_usable(redis):
        return RateLimitResult(True, limit, limit, retry_after)
    key = f"ratelimit:{identity}:{window}"
    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, WINDOW_SECONDS)
            count, _ = await pipe.execute()
    except REDIS_ERRORS as exc:
        log_redis_error(exc, "rate limit")  # fail open
        return RateLimitResult(True, limit, limit, retry_after)
    return RateLimitResult(count <= limit, limit, max(limit - count, 0), retry_after)
