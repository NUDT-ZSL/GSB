"""应用日志：按日期滚动的文本日志。

严格只允许写入 run_id、源标识、批次号、条数、错误码等运行元数据；
金额、客户名称等业务字段原始值一律不得出现在日志中（日志方法不接收记录载荷）。
"""
from __future__ import annotations

import logging
import os
import re
from logging.handlers import TimedRotatingFileHandler

# 日志行安全字符：禁止引号以外的业务自由文本，值只允许受控枚举与数字
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9_.\-]+$")
_SAFE_SOURCE = re.compile(r"^[a-z_]+$")


class RunLogger:
    def __init__(self, log_dir: str, run_id: str):
        os.makedirs(log_dir, exist_ok=True)
        self.run_id = run_id
        self._logger = logging.getLogger(f"recon.{run_id}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        if not self._logger.handlers:
            path = os.path.join(log_dir, "recon.log")
            handler = TimedRotatingFileHandler(
                path, when="midnight", backupCount=14, encoding="utf-8"
            )
            handler.suffix = "%Y-%m-%d"
            formatter = logging.Formatter(
                "%(asctime)s %(levelname)s %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
            handler.setFormatter(formatter)
            self._logger.addHandler(handler)
        self.path = os.path.join(log_dir, "recon.log")

    @staticmethod
    def _check(token: str, pattern: re.Pattern, name: str) -> str:
        if not isinstance(token, str) or not pattern.match(token):
            raise ValueError(f"不安全的日志{name}: {token!r}")
        return token

    def _emit(self, event: str, source: str | None = None,
              batch_seq: int | None = None, count: int | None = None,
              error_code: str | None = None) -> None:
        parts = [
            f"run_id={self._check(self.run_id, _SAFE_RUN_ID, 'run_id')}",
            f"event={event}",
        ]
        if source is not None:
            parts.append(f"source={self._check(source, _SAFE_SOURCE, 'source')}")
        if batch_seq is not None:
            parts.append(f"batch_seq={int(batch_seq)}")
        if count is not None:
            parts.append(f"count={int(count)}")
        if error_code is not None:
            parts.append(f"error_code={self._check(error_code, _SAFE_RUN_ID, 'error_code')}")
        self._logger.info(" ".join(parts))

    def source_start(self, source: str) -> None:
        self._emit("source_start", source=source)

    def fetch_ok(self, source: str, batch_seq: int, count: int) -> None:
        self._emit("fetch_ok", source, batch_seq, count)

    def fetch_retry(self, source: str, batch_seq: int, error_code: str) -> None:
        self._emit("fetch_retry", source, batch_seq, error_code=error_code)

    def batch_committed(self, source: str, batch_seq: int, count: int) -> None:
        self._emit("batch_committed", source, batch_seq, count)

    def source_done(self, source: str, status: str) -> None:
        self._emit("source_done", source=source, error_code=status)

    def error(self, source: str, error_code: str) -> None:
        self._emit("source_error", source=source, error_code=error_code)

    def run_event(self, event: str) -> None:
        self._emit(event)

    def close(self) -> None:
        for handler in list(self._logger.handlers):
            handler.close()
            self._logger.removeHandler(handler)


def scan_log_batches(log_dir: str, run_id: str) -> set[int]:
    """从日志文件中提取某 run_id 出现过的批次号集合。"""
    batches: set[int] = set()
    path = os.path.join(log_dir, "recon.log")
    if not os.path.exists(path):
        return batches
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            if f"run_id={run_id} " not in line and not line.rstrip().endswith(f"run_id={run_id}"):
                continue
            m = re.search(r"batch_seq=(\d+)", line)
            if m:
                batches.add(int(m.group(1)))
    return batches
