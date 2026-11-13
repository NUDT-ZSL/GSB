"""带时间戳的日志器，同时输出到控制台与可选日志文件。"""

from __future__ import annotations

import sys
from datetime import datetime


class TimestampLogger:
    def __init__(self, log_path: str | None = None):
        self.log_path = log_path
        self._fh = None
        if log_path:
            # 父目录由 CLI 层统一创建
            self._fh = open(log_path, "w", encoding="utf-8")

    def _emit(self, level: str, message: str) -> None:
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{level}] {message}"
        print(line, file=sys.stderr)
        if self._fh:
            self._fh.write(line + "\n")
            self._fh.flush()

    def info(self, message: str) -> None:
        self._emit("INFO", message)

    def warning(self, message: str) -> None:
        self._emit("WARN", message)

    def error(self, message: str) -> None:
        self._emit("ERROR", message)

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None
