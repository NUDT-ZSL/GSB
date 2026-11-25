from __future__ import annotations

import os
import signal
from pathlib import Path
from typing import Any

from .util import read_json


class CrashController:
    def __init__(self, root: Path, crash_id: str | None = None):
        self.root = root
        self.crash_id = crash_id
        self.spec: dict[str, Any] | None = None
        if crash_id:
            plan_path = root / "samples" / "crashes" / "crash_plan.json"
            plan = read_json(plan_path)
            for injection in plan.get("injections", []):
                if injection.get("id") == crash_id:
                    self.spec = injection
                    break
            if self.spec is None:
                raise ValueError(f"unknown crash injection id: {crash_id}")

    def matches(self, stage: str, point: str, **context: Any) -> bool:
        if not self.spec:
            return False
        if self.spec.get("stage") != stage or self.spec.get("point") != point:
            return False
        for key in ("generation", "segment_id"):
            if key in self.spec and self.spec.get(key) != context.get(key):
                return False
        return True

    def kill_if(self, stage: str, point: str, **context: Any) -> None:
        if self.matches(stage, point, **context):
            os.sync()
            os.kill(os.getpid(), signal.SIGKILL)

    def kill_after_record(self, *, segment_id: str, applied_count: int) -> None:
        if not self.spec or self.spec.get("stage") != "replay":
            return
        if self.spec.get("segment_id") != segment_id:
            return
        after_records = int(self.spec.get("after_records", 0))
        if applied_count >= after_records:
            os.sync()
            os.kill(os.getpid(), signal.SIGKILL)
