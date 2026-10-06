"""Redis client factory.

Redis backs the response cache, rate limiter, JWT denylist and request
counters. All callers treat Redis as optional: if it is unreachable the API
keeps serving (fail-open) and the error is logged.
"""

from redis.asyncio import Redis

from app.core.config import Settings


def create_redis(settings: Settings) -> Redis:
    # Tight timeouts: Redis runs on the same network (sub-millisecond RTT), and a
    # slow Redis must never be slower than having no cache at all. During an
    # outage only the circuit breaker's recovery probe pays the connect timeout.
    return Redis.from_url(
        settings.redis_dsn,
        decode_responses=True,
        socket_timeout=0.5,
        socket_connect_timeout=0.25,
        health_check_interval=30,
        max_connections=100,
        retry_on_timeout=False,
    )
