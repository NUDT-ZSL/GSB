"""Non-blocking exclusive plan commit gate."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

from .errors import E_STAGE_BUSY, fail


class StageCoordinator:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._owner: str | None = None

    @property
    def owner(self) -> str | None:
        return self._owner

    def try_acquire(self, owner: str) -> bool:
        acquired = self._lock.acquire(blocking=False)
        if acquired:
            self._owner = owner
        return acquired

    def release(self, owner: str | None = None) -> None:
        if owner is not None and self._owner not in (None, owner):
            return
        self._owner = None
        try:
            self._lock.release()
        except RuntimeError:
            pass

    def busy_error(self, attempted_plan: str | None = None) -> None:
        fail(
            E_STAGE_BUSY,
            "another chunk plan commit currently owns the stage",
            field="stage",
            data={
                "stage": "chunk_plan_commit",
                "owner": self._owner,
                "attempted_plan": attempted_plan,
                "waited": False,
            },
        )

    @contextmanager
    def hold(self, owner: str) -> Iterator[None]:
        if not self.try_acquire(owner):
            self.busy_error(attempted_plan=owner)
        try:
            yield
        finally:
            self.release(owner)

