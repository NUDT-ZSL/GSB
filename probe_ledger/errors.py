from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


EXIT_OK = 0
EXIT_USAGE = 2
EXIT_BUSINESS = 3
EXIT_VERIFY = 4
EXIT_INTERNAL = 5
EXIT_INTERRUPTED = 137


@dataclass
class BusinessError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    exit_code: int = EXIT_BUSINESS

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.details:
            payload["details"] = self.details
        return payload


def diagnostic(
    code: str,
    message: str,
    *,
    instance_id: str | None = None,
    obs_seq: int | None = None,
    shard_id: str | None = None,
    digest_a: str | None = None,
    digest_b: str | None = None,
    arrival_a: int | None = None,
    arrival_b: int | None = None,
    **extra: Any,
) -> dict[str, Any]:
    result: dict[str, Any] = {"code": code, "message": message}
    fields = {
        "instance_id": instance_id,
        "obs_seq": obs_seq,
        "shard_id": shard_id,
        "digest_a": digest_a,
        "digest_b": digest_b,
        "arrival_a": arrival_a,
        "arrival_b": arrival_b,
    }
    result.update({key: value for key, value in fields.items() if value is not None})
    result.update(extra)
    return result
