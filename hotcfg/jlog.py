"""结构化 JSONL 日志。"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path


class JsonlLogger:
    """线程安全的 JSONL 结构化日志写入器。"""

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def emit(self, event: str, **fields) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **fields,
        }
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with self._lock:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")


class MemoryLogger(JsonlLogger):
    """不落盘的日志器，用于测试。"""

    def __init__(self):
        self.records: list[dict] = []
        self._lock = threading.Lock()

    @property
    def path(self):
        return None

    def emit(self, event: str, **fields) -> None:
        with self._lock:
            self.records.append({"event": event, **fields})
