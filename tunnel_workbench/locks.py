from __future__ import annotations

import hashlib
from pathlib import Path

from .errors import fail

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None


class TunnelFileLock:
    """Cross-process per-tunnel advisory lock with a Windows fallback."""

    def __init__(self, lock_dir: str | Path, tunnel: str) -> None:
        self.lock_dir = Path(lock_dir)
        digest = hashlib.sha256(tunnel.encode("utf-8")).hexdigest()[:24]
        self.path = self.lock_dir / f"{digest}.lock"
        self._file = None

    def acquire(self, *, blocking: bool = False) -> None:
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self._file = open(self.path, "a+b")
        try:
            if fcntl is not None:
                flags = fcntl.LOCK_EX
                if not blocking:
                    flags |= fcntl.LOCK_NB
                fcntl.flock(self._file.fileno(), flags)
            elif msvcrt is not None:
                mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
                self._file.seek(0, 2)
                if self._file.tell() == 0:
                    self._file.write(b"\0")
                    self._file.flush()
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), mode, 1)
            else:  # pragma: no cover - unsupported platform
                fail("E_LOCK_UNSUPPORTED", "no file locking primitive is available")
        except (BlockingIOError, OSError):
            self._close()
            if not blocking:
                fail(
                    "E_TUNNEL_LOCKED",
                    "another forwarding instance owns this tunnel",
                    details={"tunnel_path": str(self.path)},
                    http_status=409,
                )
            raise

    def release(self) -> None:
        if self._file is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover - Windows
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self._close()

    def _close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    def __enter__(self) -> "TunnelFileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

