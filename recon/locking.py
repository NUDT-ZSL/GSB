"""操作系统级单实例文件锁（覆盖整个运行期）。

实现要点（跨平台稳健互斥）：
- 以 O_CREAT|O_EXCL 排他创建锁文件：创建成功即持锁，文件已存在则检查
  持有者进程是否仍存活；持有者已死则接管，仍存活则在超时内重试后报 E_LOCKED。
- 持锁句柄在整个运行期保持打开，进程正常退出或被终止时由 OS 回收句柄。
- Windows 额外对字节区间加 msvcrt 排他锁，POSIX 额外加 flock，作为内核级辅助。
锁文件可能长期存在，但进程结束后可被新进程重新加锁。
"""
from __future__ import annotations

import os
import sys
import time

from .errors import LockError


def _pid_alive(pid: int) -> bool:
    if pid == -1:
        # 文件被锁/拒绝访问：持有者进程仍存活
        return True
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False  # 进程不存在
        try:
            still_active = 259
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == still_active
            return False
        finally:
            kernel32.CloseHandle(handle)
    else:  # pragma: no cover
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True


class FileLock:
    def __init__(self, path: str, timeout_s: float = 5.0):
        self.path = path
        self.timeout_s = timeout_s
        self._fh = None
        self._locked = False
        self._remove_on_release = True

    def _try_create_exclusive(self) -> bool:
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            self._fh = os.fdopen(fd, "a+b")
        except FileExistsError:
            return False
        self._write_owner()
        self._kernel_lock()
        self._locked = True
        return True

    def _write_owner(self) -> None:
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(f"{os.getpid()}\n".encode("utf-8"))
        self._fh.flush()

    def _kernel_lock(self) -> None:
        if sys.platform == "win32":
            import msvcrt  # type: ignore
            self._fh.seek(0)
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:  # pragma: no cover
            import fcntl  # type: ignore
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _kernel_unlock(self) -> None:
        if self._fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt  # type: ignore
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover
                import fcntl  # type: ignore
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass

    def _read_owner_pid(self) -> int:
        """读取持有者 pid。

        返回值约定：正整数=存活候选 pid；-1=文件被锁/拒绝访问（持有者存活，
        不可接管）；0=内容损坏或无法判定。
        """
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                return int(fh.readline().strip() or "0")
        except (OSError, ValueError):
            # 持锁进程对文件加了字节锁，其他进程读取会被拒绝 -> 持有者存活
            return -1

    def acquire(self, remove_on_release: bool = True) -> None:
        self._remove_on_release = remove_on_release
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        deadline = time.monotonic() + self.timeout_s
        while True:
            if self._try_create_exclusive():
                return
            # 锁文件已存在：判断持有者是否存活
            owner_pid = self._read_owner_pid()
            if not _pid_alive(owner_pid):
                # 持有者已死（残留锁文件），接管
                try:
                    os.remove(self.path)
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                # 确保失败时不携带任何半开句柄，release() 也不会误删他人锁
                self._fh = None
                self._locked = False
                raise LockError(
                    f"无法在 {self.timeout_s}s 内获取锁，持有者 pid={owner_pid}")
            time.sleep(0.1)

    def release(self) -> None:
        if not self._locked or self._fh is None:
            return
        try:
            self._kernel_unlock()
        finally:
            try:
                self._fh.close()
            finally:
                if getattr(self, "_remove_on_release", True):
                    try:
                        os.remove(self.path)
                    except OSError:
                        pass
            self._fh = None
            self._locked = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False
