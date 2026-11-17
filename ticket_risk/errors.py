"""Business error codes used by loaders, workers, and the CLI."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


E_EMPTY_TEXT = "E_EMPTY_TEXT"
E_BAD_RECORD = "E_BAD_RECORD"
E_UNKNOWN_CHANNEL = "E_UNKNOWN_CHANNEL"
E_RULE_CYCLE = "E_RULE_CYCLE"
E_CONFIG_MISMATCH = "E_CONFIG_MISMATCH"
E_SNAPSHOT_INVALID = "E_SNAPSHOT_INVALID"
E_INTERACT_BUDGET_PENDING = "E_INTERACT_BUDGET_PENDING"
E_INCONSISTENT = "E_INCONSISTENT"
E_STAGE_EXCEPTION = "E_STAGE_EXCEPTION"
E_COMMAND_INVALID = "E_COMMAND_INVALID"
E_COMMAND_UNKNOWN = "E_COMMAND_UNKNOWN"
E_WAIT_TIMEOUT = "E_WAIT_TIMEOUT"


@dataclass(frozen=True)
class Failure:
    """A deterministic failure-list entry.

    ``sample_id`` is absent for global failures.  ``line`` is one-based for
    sample-input failures.  Ordering is defined by :meth:`sort_key` so loader,
    worker, and serial runs all produce the same list order.
    """

    business_code: str
    detail: str
    sample_id: str | None = None
    line: int | None = None
    stage: str | None = None
    exception_type: str | None = None
    scope: str = "sample"

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}

    def sort_key(self) -> tuple[Any, ...]:
        return (
            1 if self.sample_id is None else 0,
            self.sample_id or "",
            self.line if self.line is not None else -1,
            self.stage or "",
            self.business_code,
            self.detail,
        )


@dataclass
class LoadBundle:
    samples: list[Any] = field(default_factory=list)
    rules: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    failures: list[Failure] = field(default_factory=list)
    resource_fingerprint: str = ""
    config_hash: str = ""
    samples_fingerprint: str = ""
    fatal: bool = False


class SnapshotError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail

