from __future__ import annotations

import json
import os
import socket
import time
import uuid
from typing import Any

from .errors import BusinessError
from .workspace import Workspace


class NonBlockingCommitLock:
    """Cross-platform O_EXCL directory lock; never waits for a competing commit."""

    def __init__(self, workspace: Workspace, *, stale_after_seconds: float = 3600.0) -> None:
        self.workspace = workspace
        self.path = workspace.paths.lock_dir
        self.stale_after_seconds = stale_after_seconds
        self.token = uuid.uuid4().hex
        self.acquired = False

    def __enter__(self) -> "NonBlockingCommitLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.mkdir(self.path)
        except FileExistsError as exc:
            owner = self._read_owner()
            self._maybe_clear_stale(owner)
            try:
                os.mkdir(self.path)
            except FileExistsError:
                raise BusinessError(
                    "E_STAGE_BUSY",
                    "another incremental commit is active; this request was not queued",
                    field="commit.lock",
                    stage="commit",
                    details={"owner": self._read_owner()},
                ) from exc
        owner = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "token": self.token,
            "acquired_at": time.time(),
        }
        try:
            (self.path / "owner.json").write_text(json.dumps(owner), encoding="utf-8")
        except OSError:
            try:
                os.rmdir(self.path)
            except OSError:
                pass
            raise
        self.acquired = True

    def _read_owner(self) -> dict[str, Any]:
        try:
            raw = (self.path / "owner.json").read_text(encoding="utf-8")
            value = json.loads(raw)
            return value if isinstance(value, dict) else {"raw": raw}
        except (OSError, json.JSONDecodeError):
            return {}

    def _maybe_clear_stale(self, owner: dict[str, Any]) -> None:
        try:
            age = time.time() - float(owner.get("acquired_at", 0))
        except (TypeError, ValueError):
            age = float("inf")
        pid = owner.get("pid")
        alive = isinstance(pid, int)
        if isinstance(pid, int) and os.name != "nt":
            try:
                os.kill(pid, 0)
            except (OSError, ProcessLookupError):
                alive = False
            except PermissionError:
                alive = True
        if alive or age < self.stale_after_seconds:
            return
        owner_path = self.path / "owner.json"
        try:
            owner_path.unlink(missing_ok=True)
            os.rmdir(self.path)
        except OSError:
            pass

    def release(self) -> None:
        if not self.acquired:
            return
        owner = self._read_owner()
        if owner.get("token") == self.token:
            try:
                (self.path / "owner.json").unlink(missing_ok=True)
                os.rmdir(self.path)
            except OSError:
                pass
        self.acquired = False

    def wait_for_release(self, timeout_seconds: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if not self.path.exists():
                return True
            time.sleep(0.01)
        return not self.path.exists()

