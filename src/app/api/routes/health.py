"""Operational endpoints used by Kubernetes probes, load balancers and Prometheus."""

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.api.deps import BotDep, DatabaseDep, RedisDep, SettingsDep
from app.core.metrics import DB_POOL_CHECKED_OUT, DB_POOL_SIZE
from app.schemas.admin import HealthReport
from app.services import analytics_service

router = APIRouter(tags=["Operations"])


@router.get(
    "/health",
    response_model=HealthReport,
    responses={503: {"model": HealthReport, "description": "Database unavailable"}},
    summary="Aggregate health (503 only when the database is down)",
)
async def health(
    request: Request, settings: SettingsDep, db: DatabaseDep, redis: RedisDep, bot: BotDep
) -> JSONResponse:
    report = await analytics_service.health_report(
        settings, db, redis, bot, request.app.state.started_at
    )
    status_code = 503 if report.status == "down" else 200
    return JSONResponse(report.model_dump(mode="json"), status_code=status_code)


@router.get("/health/live", summary="Liveness probe: the process is running")
async def liveness() -> dict[str, str]:
    return {"status": "alive"}


@router.get("/health/ready", summary="Readiness probe: the database is reachable")
async def readiness(db: DatabaseDep, redis: RedisDep) -> JSONResponse:
    # Only the database gates readiness. Redis is fail-open, so marking every
    # pod unready during a Redis outage would turn a degradation into an outage.
    database = await analytics_service.check_database(db)
    cache = await analytics_service.check_redis(redis)
    ready = database.status == "ok"
    body = {
        "status": "ready" if ready else "not_ready",
        "database": database.status,
        "redis": cache.status,
    }
    return JSONResponse(body, status_code=200 if ready else 503)


@router.get("/metrics", summary="Prometheus metrics", response_class=Response)
async def metrics(db: DatabaseDep) -> Response:
    pool = db.pool_status()
    DB_POOL_SIZE.set(pool["size"])
    DB_POOL_CHECKED_OUT.set(pool["checked_out"])
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
