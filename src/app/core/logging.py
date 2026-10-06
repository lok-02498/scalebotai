"""Structured JSON logging.

Each record is a single JSON line so Logstash can parse it without grok
patterns. ``log_type`` routes the record to its Elasticsearch index:

* ``api``    - one access-log line per HTTP request
* ``user``   - authentication / account events
* ``error``  - anything at ERROR level or above
* ``system`` - startup, shutdown and dependency events
"""

import json
import logging
import os
import socket
import sys
from datetime import UTC, datetime
from logging.handlers import WatchedFileHandler

from app.core.config import Settings
from app.core.context import request_id_ctx, user_id_ctx

SERVICE_NAME = "scalebotai-api"

# Attributes present on every LogRecord; anything else was passed via ``extra=``.
# uvicorn's color_message duplicates message with ANSI escape codes.
_RESERVED = set(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
    "taskName",
    "color_message",
}


class JsonFormatter(logging.Formatter):
    def __init__(self, environment: str) -> None:
        super().__init__()
        self.environment = environment
        self.hostname = os.getenv("POD_NAME") or socket.gethostname()

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, tz=UTC)
        payload: dict[str, object] = {
            "@timestamp": timestamp.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": SERVICE_NAME,
            "environment": self.environment,
            "hostname": self.hostname,
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if "log_type" not in payload:
            payload["log_type"] = "error" if record.levelno >= logging.ERROR else "system"
        request_id = request_id_ctx.get()
        if request_id and "request_id" not in payload:
            payload["request_id"] = request_id
        user_id = user_id_ctx.get()
        if user_id and "user_id" not in payload:
            payload["user_id"] = user_id
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-8s %(name)s: %(message)s")


def setup_logging(settings: Settings) -> None:
    """Configure the root logger. Safe to call more than once."""
    formatter: logging.Formatter = (
        JsonFormatter(settings.app_env) if settings.log_format == "json" else ConsoleFormatter()
    )
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if settings.log_file:
        try:
            os.makedirs(os.path.dirname(settings.log_file) or ".", exist_ok=True)
            # WatchedFileHandler re-opens the file if logrotate moves it.
            handlers.append(WatchedFileHandler(settings.log_file, encoding="utf-8"))
        except OSError as exc:  # never fail startup because a log file is unwritable
            print(f"cannot open LOG_FILE {settings.log_file}: {exc}", file=sys.stderr)

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)
    root.setLevel(settings.log_level.upper())

    # Route uvicorn's loggers through our JSON handler; we emit our own access log.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers = []
        uv_logger.propagate = True
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
