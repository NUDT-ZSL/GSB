"""Business errors and the stable error envelope used by the CLI."""

from __future__ import annotations

from typing import Any


class BusinessError(Exception):
    """An expected, user-actionable engine failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        run_seq: int | None = None,
        task_name: str | None = None,
        tier: str | None = None,
        **fields: Any,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.run_seq = run_seq
        self.task_name = task_name
        self.tier = tier
        self.fields = fields

    def as_dict(self) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "run_seq": self.run_seq,
            "task_name": self.task_name,
            "tier": self.tier,
        }
        fields.update(self.fields)
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "fields": fields,
            },
        }


E_BAD_TASK = "E_BAD_TASK"
E_BAD_PIPELINE = "E_BAD_PIPELINE"
E_BAD_CONFIG = "E_BAD_CONFIG"
E_CONFIG_CONFLICT = "E_CONFIG_CONFLICT"
E_CONFIG_ROLLBACK = "E_CONFIG_ROLLBACK"
E_CLOCK_ROLLBACK = "E_CLOCK_ROLLBACK"
E_TASK_MISSING = "E_TASK_MISSING"
E_ARTIFACT_MISSING = "E_ARTIFACT_MISSING"
E_RUN_NOT_FOUND = "E_RUN_NOT_FOUND"
E_ALL_RUNS_COMMITTED = "E_ALL_RUNS_COMMITTED"
E_DIGEST_MISMATCH = "E_DIGEST_MISMATCH"
E_OBJECT_MISSING = "E_OBJECT_MISSING"
E_EVICT_CANDIDATE_MISSING = "E_EVICT_CANDIDATE_MISSING"
E_STATE_NOT_LOADED = "E_STATE_NOT_LOADED"
E_STATE_CONFLICT = "E_STATE_CONFLICT"
E_STATE_CORRUPT = "E_STATE_CORRUPT"
E_IO = "E_IO"
E_INTERNAL = "E_INTERNAL"
