from __future__ import annotations

import json
from dataclasses import dataclass, field as dataclass_field
from typing import Any


@dataclass
class PipelineError(Exception):
    code: str
    message: str
    job_id: str | None = None
    field: str | None = None
    stage: str | None = None
    version: int | None = None
    details: dict[str, Any] = dataclass_field(default_factory=dict)

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.job_id is not None:
            payload["job_id"] = self.job_id
        if self.field is not None:
            payload["field"] = self.field
        if self.stage is not None:
            payload["stage"] = self.stage
        if self.version is not None:
            payload["version"] = self.version
        if self.details:
            payload["details"] = self.details
        return payload

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )


def error(
    code: str,
    message: str,
    *,
    job_id: str | None = None,
    field: str | None = None,
    stage: str | None = None,
    version: int | None = None,
    details: dict[str, Any] | None = None,
) -> PipelineError:
    return PipelineError(
        code=code,
        message=message,
        job_id=job_id,
        field=field,
        stage=stage,
        version=version,
        details=details or {},
    )
