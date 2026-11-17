"""真实资源预算控制：墙钟超时、路径数上限与文件锁串行化。"""
from __future__ import annotations

import errno
import fcntl
import time
from contextlib import contextmanager
from dataclasses import dataclass

E_TIMEOUT = "E_TIMEOUT"
E_BUDGET = "E_BUDGET"
E_LOCK = "E_LOCK"


class BudgetExceeded(Exception):
    """单位清单处理路径数超过上限。"""

    def __init__(self, limit: int, actual: int):
        self.error_code = E_BUDGET
        self.limit = limit
        self.actual = actual
        super().__init__(f"路径数 {actual} 超过单位清单上限 {limit}")


class DeadlineExceeded(Exception):
    """真实墙钟测量判定的单份清单超时。"""

    def __init__(self, limit_seconds: float, elapsed: float):
        self.error_code = E_TIMEOUT
        self.limit_seconds = limit_seconds
        self.elapsed = elapsed
        super().__init__(f"清单处理耗时 {elapsed:.4f}s 超过上限 {limit_seconds}s")


class LockConflict(Exception):
    """同工作目录已有持锁写入进程，非阻塞抢锁失败。"""

    def __init__(self, lock_path: str):
        self.error_code = E_LOCK
        self.lock_path = lock_path
        super().__init__(f"文件锁冲突，无法获取 {lock_path}")


@dataclass
class Deadline:
    """以真实墙钟（time.perf_counter 单调时钟）测量的截止时间。"""

    limit_seconds: float
    _start: float = 0.0

    def __post_init__(self) -> None:
        self._start = time.perf_counter()

    def elapsed(self) -> float:
        return time.perf_counter() - self._start

    def remaining(self) -> float:
        if self.limit_seconds <= 0:
            return float("inf")
        return self.limit_seconds - self.elapsed()

    def check(self) -> None:
        if self.limit_seconds > 0 and self.elapsed() >= self.limit_seconds:
            raise DeadlineExceeded(self.limit_seconds, self.elapsed())


@contextmanager
def file_lock(lock_path: str):
    """非阻塞排他文件锁，串行化同一工作目录的并发写入。

    锁冲突立即抛 LockConflict(E_LOCK)，不做等待。
    """
    handle = open(lock_path, "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise LockConflict(lock_path) from exc
            raise
        yield handle
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def atomic_write_text(target_path: str, content: str) -> None:
    """先写同目录临时文件，再整体 rename 替换，保证读者只见完整文件。"""
    import os
    import tempfile
    from pathlib import Path

    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
