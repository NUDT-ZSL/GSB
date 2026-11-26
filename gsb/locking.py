"""基于 ``fcntl.flock`` 的跨进程文件锁。

与 SQLite 事务/唯一约束共同构成双保险：文件锁减少竞争与忙等，数据库
``PRIMARY KEY`` / 唯一索引保证同一段同一策略版本最终只提交一次。
"""
from __future__ import annotations

import fcntl
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .errors import Code, GsbError


@contextmanager
def file_lock(lock_path: str | Path, timeout_ms: int = 10_000) -> Iterator[None]:
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    deadline = time.monotonic() + timeout_ms / 1000.0
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise GsbError(
                        Code.E_LOCK_BUSY,
                        f"timed out waiting for file lock: {path}",
                        {"lock_path": str(path), "timeout_ms": timeout_ms},
                    )
                time.sleep(0.02)
        yield
    finally:
        if acquired:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        fh.close()
