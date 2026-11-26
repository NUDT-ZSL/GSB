from __future__ import annotations

import contextlib
import fcntl
import os
import socket
from pathlib import Path
from typing import Iterator, Optional

from .errors import BizError, ErrorCode


DEFAULT_CLOCK_SKEW_TOLERANCE_MS = 5_000


def host_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


@contextlib.contextmanager
def file_lock(lock_path: Path, blocking: bool = True) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(fd, flags)
        except BlockingIOError as exc:
            raise BizError(
                ErrorCode.LEASE_BUSY,
                "block/config lock is held by another live process",
                {"lock_path": str(lock_path)},
            ) from exc
        os.ftruncate(fd, 0)
        os.write(fd, f"{host_owner()}\n".encode("utf-8"))
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def maybe_crash(point: str, target: Optional[str] = None) -> None:
    """Kill the process at a deterministic fault point.

    ``DP_CRASH_AT=point`` or ``DP_CRASH_AT=point:target``.  Multiple specs can
    be separated by ``,``.  ``os._exit`` intentionally bypasses exception
    handlers and buffered cleanup to emulate SIGKILL/power loss.
    """

    spec = os.environ.get("DP_CRASH_AT", "")
    if not spec:
        return
    for raw in spec.split(","):
        raw = raw.strip()
        if not raw:
            continue
        wanted_point, sep, wanted_target = raw.partition(":")
        if wanted_point != point:
            continue
        if sep and wanted_target and target != wanted_target:
            continue
        os._exit(97)


def inject_operator_failure(target: str) -> None:
    spec = os.environ.get("DP_FAIL_AT", "")
    wanted = {item.strip() for item in spec.split(",") if item.strip()}
    if target in wanted or "*" in wanted:
        raise BizError(
            ErrorCode.INJECTED_FAILURE,
            "deterministic operator failure injected",
            {"target": target},
        )
