"""Cross-platform advisory file lock (fcntl on POSIX, msvcrt on Windows)."""
import os

if os.name == "nt":  # pragma: no cover - exercised on Windows only
    import msvcrt

    def _lock(fh):
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)

    def _unlock(fh):
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(fh):
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)

    def _unlock(fh):
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class FileLock:
    """Exclusive advisory lock guarding SQLite schema setup and commits."""

    def __init__(self, path):
        self.path = path
        self._fh = None

    def __enter__(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)),
                    exist_ok=True)
        self._fh = open(self.path, "a+b")
        _lock(self._fh)
        return self

    def __exit__(self, *exc):
        try:
            _unlock(self._fh)
        finally:
            self._fh.close()
            self._fh = None
        return False
