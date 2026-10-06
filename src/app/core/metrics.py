"""Prometheus metrics exposed at ``GET /metrics``.

One uvicorn process runs per container, so the default (single-process)
registry is accurate; horizontal scale comes from more pods, each scraped
individually by Prometheus.
"""

from prometheus_client import Counter, Gauge, Histogram

LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0, 2.5, 5.0, 10.0)

HTTP_REQUESTS = Counter(
    "scalebot_http_requests_total",
    "Total HTTP requests handled",
    ["method", "route", "status"],
)
HTTP_LATENCY = Histogram(
    "scalebot_http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "route"],
    buckets=LATENCY_BUCKETS,
)
HTTP_IN_PROGRESS = Gauge(
    "scalebot_http_requests_in_progress",
    "HTTP requests currently being processed",
)
CHAT_MESSAGES = Counter(
    "scalebot_chat_messages_total",
    "Chat messages answered by the bot",
    ["bot_mode", "cached"],
)
BOT_LATENCY = Histogram(
    "scalebot_bot_response_seconds",
    "Time the chatbot engine took to produce an answer",
    ["bot_mode"],
    buckets=LATENCY_BUCKETS,
)
CACHE_HITS = Counter("scalebot_cache_hits_total", "Redis cache hits", ["cache"])
CACHE_MISSES = Counter("scalebot_cache_misses_total", "Redis cache misses", ["cache"])
RATE_LIMITED = Counter("scalebot_rate_limited_total", "Requests rejected by the rate limiter")
AUTH_EVENTS = Counter("scalebot_auth_events_total", "Authentication events", ["event"])
DB_POOL_CHECKED_OUT = Gauge(
    "scalebot_db_pool_checked_out", "Database connections currently checked out"
)
DB_POOL_SIZE = Gauge("scalebot_db_pool_size", "Configured database connection pool size")
APP_INFO = Gauge(
    "scalebot_app_info", "Static application information", ["version", "bot_mode", "environment"]
)
