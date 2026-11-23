"""多进程并发一致性与异常链路隔离。

- FileLock: 基于 O_CREAT|O_EXCL 的跨进程互斥文件锁;
  SQLite 侧另有 BEGIN IMMEDIATE 与 busy_timeout 兜底。
- 同一规则版本 / 同一事件簇版本只提交一次: journal 幂等键保证。
- 单服务链路异常(BusinessError)被捕获并记录, 不中断其他服务的降噪。
"""

import errno
import os
import time

from . import errors as E


class FileLock:
    def __init__(self, path, timeout=30.0, poll=0.01):
        self.path = path
        self.timeout = timeout
        self.poll = poll
        self.fd = None

    def acquire(self):
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL
                                  | os.O_RDWR)
                os.write(self.fd, str(os.getpid()).encode("ascii"))
                return self
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise
                if time.monotonic() > deadline:
                    raise TimeoutError("file lock timeout: %s" % self.path)
                time.sleep(self.poll)

    def release(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *args):
        self.release()
        return False


def run_isolated(fn, label):
    """在异常隔离包装中执行一条链路; 返回结构化结果, 不向外抛业务异常。"""
    try:
        return {"label": label, "ok": True, "result": fn()}
    except E.BusinessError as exc:
        return {"label": label, "ok": False, "error": exc.to_dict()}
