"""Outermost middleware: request id, timing, Prometheus metrics, access log.

Written as pure ASGI middleware rather than ``BaseHTTPMiddleware``: it adds
no extra task per request and lets context variables flow to the handlers.
"""

import logging
import re
import time
import uuid

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.routing import Match
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.context import request_id_ctx, user_id_ctx
from app.core.errors import error_body
from app.core.metrics import HTTP_IN_PROGRESS, HTTP_LATENCY, HTTP_REQUESTS
from app.services.cache import increment_daily_requests

logger = logging.getLogger("app.access")

UNTRACKED_PREFIXES = ("/metrics", "/health")
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _route_template(scope: Scope) -> str:
    """Templated route path, e.g. /api/v1/chat/conversations/{conversation_id}.

    Using the template rather than the raw path keeps metric label cardinality bounded.
    """
    route = scope.get("route")
    if route is not None and hasattr(route, "path"):
        return route.path
    app = scope.get("app")
    for candidate in getattr(getattr(app, "router", None), "routes", []):
        match, _ = candidate.matches(scope)
        if match == Match.FULL:
            return candidate.path
    return "unmatched"


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        incoming = Headers(scope=scope).get("x-request-id", "")
        request_id = incoming if _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        rid_token = request_id_ctx.set(request_id)
        uid_token = user_id_ctx.set(None)
        state = scope.setdefault("state", {})
        state["request_id"] = request_id

        path: str = scope["path"]
        method: str = scope["method"]
        tracked = not path.startswith(UNTRACKED_PREFIXES)
        status_code = 500
        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                headers["X-Process-Time-ms"] = f"{(time.perf_counter() - start) * 1000:.2f}"
            await send(message)

        if tracked:
            HTTP_IN_PROGRESS.inc()
        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            logger.exception(
                "Unhandled exception while processing %s %s",
                method,
                path,
                extra={"log_type": "error", "event": "unhandled_exception"},
            )
            status_code = 500
            if not response_started:
                response = JSONResponse(
                    error_body("INTERNAL_ERROR", "Internal server error"), status_code=500
                )
                await response(scope, receive, send_wrapper)
        finally:
            duration = time.perf_counter() - start
            if tracked:
                HTTP_IN_PROGRESS.dec()
                route = _route_template(scope)
                HTTP_REQUESTS.labels(method, route, str(status_code)).inc()
                HTTP_LATENCY.labels(method, route).observe(duration)
                self._access_log(scope, state, route, status_code, duration)
                # Runs after the response was sent, so it adds no client latency.
                app_state = getattr(scope.get("app"), "state", None)
                await increment_daily_requests(getattr(app_state, "redis", None))
            request_id_ctx.reset(rid_token)
            user_id_ctx.reset(uid_token)

    @staticmethod
    def _access_log(
        scope: Scope, state: dict, route: str, status_code: int, duration: float
    ) -> None:
        level = (
            logging.ERROR
            if status_code >= 500
            else logging.WARNING
            if status_code >= 400
            else logging.INFO
        )
        client = scope.get("client")
        headers = Headers(scope=scope)
        logger.log(
            level,
            "%s %s %d",
            scope["method"],
            scope["path"],
            status_code,
            extra={
                "log_type": "api",
                "event": "http_request",
                "method": scope["method"],
                "path": scope["path"],
                "route": route,
                "status_code": status_code,
                "latency_ms": round(duration * 1000, 2),
                "client_ip": client[0] if client else None,
                "user_agent": headers.get("user-agent"),
                "user_id": state.get("user_id"),
            },
        )
