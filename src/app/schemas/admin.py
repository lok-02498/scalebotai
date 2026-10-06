from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, model_validator


class UserStats(BaseModel):
    total_users: int
    active_users: int
    admin_users: int
    new_users_today: int
    new_users_7d: int


class ChatStats(BaseModel):
    total_chats: int
    total_conversations: int
    chats_today: int
    avg_chats_per_user: float
    avg_bot_latency_ms: float | None


class DailyRequestPoint(BaseModel):
    date: date
    requests: int


class DailyRequests(BaseModel):
    days: int
    total: int
    series: list[DailyRequestPoint]


class FeedbackSummary(BaseModel):
    total: int
    average_rating: float | None
    distribution: dict[str, int]


class Analytics(BaseModel):
    users: UserStats
    chats: ChatStats
    requests: DailyRequests
    feedback: FeedbackSummary
    generated_at: datetime


class ProcessStats(BaseModel):
    cpu_percent: float
    memory_rss_mb: float
    threads: int


class PoolStats(BaseModel):
    size: int
    checked_out: int
    overflow: int


class RedisStats(BaseModel):
    connected: bool
    used_memory_human: str | None = None
    connected_clients: int | None = None
    keyspace_hit_ratio: float | None = None


class SystemStats(BaseModel):
    hostname: str
    app_version: str
    python_version: str
    environment: str
    bot_mode: str
    uptime_seconds: float
    process: ProcessStats
    db_pool: PoolStats
    redis: RedisStats


HealthStatus = Literal["ok", "degraded", "down"]


class DependencyHealth(BaseModel):
    status: HealthStatus
    latency_ms: float | None = None
    error: str | None = None


class ChatbotHealth(BaseModel):
    status: HealthStatus
    mode: str


class HealthChecks(BaseModel):
    database: DependencyHealth
    redis: DependencyHealth
    chatbot: ChatbotHealth


class HealthReport(BaseModel):
    status: HealthStatus
    version: str
    uptime_seconds: float
    checks: HealthChecks


class AdminUserUpdate(BaseModel):
    is_active: bool | None = None
    role: Literal["user", "admin"] | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> "AdminUserUpdate":
        if self.is_active is None and self.role is None:
            raise ValueError("provide at least one of: is_active, role")
        return self
