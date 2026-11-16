"""Structured JSONL logging shared by client and server processes."""

import json
import logging
import os
import sys
import threading
import time


class JsonLineFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created))
            + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        extra = getattr(record, "context", None)
        if isinstance(extra, dict):
            for key, value in extra.items():
                payload.setdefault(key, value)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


_LOG_LOCK = threading.Lock()
_CONFIGURED = {}


def configure_logger(name, log_dir=None, level=logging.INFO, echo=False):
    """Configure a process-wide logger writing one JSON object per line."""
    with _LOG_LOCK:
        logger = logging.getLogger(name)
        logger.setLevel(level)
        logger.propagate = False
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()

        if echo:
            stream = logging.StreamHandler(sys.stderr)
            stream.setFormatter(JsonLineFormatter())
            logger.addHandler(stream)

        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
            file_handler = logging.FileHandler(
                os.path.join(log_dir, f"{name}.jsonl"), encoding="utf-8"
            )
            file_handler.setFormatter(JsonLineFormatter())
            logger.addHandler(file_handler)

        _CONFIGURED[name] = logger
        return logger


def get_logger(name):
    return logging.getLogger(name)


def log_event(logger, level, event, **context):
    if logger is None:
        return
    logger.log(level, event, extra={"context": context} if context else None)
