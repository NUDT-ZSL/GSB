"""Non-blocking cross-process stage lock and local business error journal."""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path
from types import TracebackType
from typing import Any

from .canonical import canonical_json_bytes
from .errors import BusinessError, E_STAGE_BUSY


class StageGate:
    """Exactly one holder; contenders fail immediately with E_STAGE_BUSY."""

    def __init__(self, runtime_dir: str | Path) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.runtime_dir / "stage.lock"
        self._token: str | None = None
        self._thread_lock = threading.Lock()

    def acquire(self) -> "StageGate":
        token = uuid.uuid4().hex
        payload = canonical_json_bytes(
            {
                "pid": os.getpid(),
                "token": token,
            }
        )
        try:
            descriptor = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError as exc:
            raise BusinessError(
                E_STAGE_BUSY,
                "another scheduling stage is already active",
                stage="stage_acquire",
                details={"lock_path": str(self.path)},
            ) from exc
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            self.path.unlink(missing_ok=True)
            raise
        self._token = token
        return self

    def release(self) -> None:
        with self._thread_lock:
            if self._token is None:
                return
            try:
                raw = self.path.read_bytes()
            except OSError:
                self._token = None
                return
            try:
                stored = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                stored = {}
            if stored.get("token") == self._token:
                self.path.unlink(missing_ok=True)
            self._token = None

    def __enter__(self) -> "StageGate":
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


class ErrorJournal:
    def __init__(self, runtime_dir: str | Path) -> None:
        self.path = Path(runtime_dir) / "errors.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(
        self,
        code: str,
        stage: str,
        message: str,
        details: dict[str, Any] | None = None,
        *,
        source: str = "pipeline",
    ) -> None:
        record = {
            "business_code": code,
            "stage": stage,
            "message": message,
            "details": details or {},
            "source": source,
        }
        raw = canonical_json_bytes(record) + b"\n"
        with self._lock:
            descriptor = os.open(
                self.path,
                os.O_CREAT | os.O_APPEND | os.O_WRONLY,
                0o600,
            )
            try:
                os.write(descriptor, raw)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def append_error(self, error: BusinessError, *, source: str = "pipeline") -> None:
        self.append(
            error.code,
            error.stage,
            error.message,
            error.details,
            source=source,
        )

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        records: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                records.append(parsed)
        return records
