"""结构化 JSON 日志（仅用标准库，按行写入 NDJSON）。"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "extra_payload", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def configure_logger(log_path: str | None = None,
                     name: str = "sre_guard") -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = JsonLineFormatter()
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    logger.addHandler(stream)
    if log_path:
        directory = os.path.dirname(os.path.abspath(log_path))
        os.makedirs(directory, exist_ok=True)
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    logger.propagate = False
    return logger


def log_governance_error(logger: logging.Logger, error: Exception) -> None:
    payload = error.to_dict() if hasattr(error, "to_dict") else {"message": str(error)}
    logger.error("governance error", extra={"extra_payload": payload})
