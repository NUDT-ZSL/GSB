"""Structured JSON logging without third-party dependencies."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import DomainError


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("event", "code", "file", "line", "detail", "context"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )


def configure_json_logging(path: str | Path, *, level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("alarm_domain")
    logger.setLevel(level)
    logger.propagate = False
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    for handler in list(logger.handlers):
        if getattr(handler, "_alarm_json", False):
            logger.removeHandler(handler)
            handler.close()
    handler = logging.FileHandler(target, encoding="utf-8")
    handler.setFormatter(JsonFormatter())
    handler._alarm_json = True  # type: ignore[attr-defined]
    logger.addHandler(handler)
    return logger


def close_json_logging(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        if getattr(handler, "_alarm_json", False):
            logger.removeHandler(handler)
            handler.close()


def log_business_error(
    logger: logging.Logger,
    error: DomainError,
    *,
    event: str,
    context: dict[str, Any] | None = None,
) -> None:
    logger.error(
        error.message,
        extra={
            "event": event,
            "code": error.code,
            "file": error.file,
            "line": error.line,
            "detail": error.detail,
            "context": context or {},
        },
    )
