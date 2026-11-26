from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Directory:
    dir_id: str
    parent_id: str | None
    base_quota: int | None
    ext_quotas: dict[str, int]
    name: str | None = None
    tombstoned: bool = False

    @property
    def display_name(self) -> str:
        return self.name or self.dir_id


@dataclass(frozen=True)
class WriteEvent:
    seq: int
    dir_id: str
    file_key: str
    bytes: int
    digest: str
    clock_ms: int
    content_b64: str | None = None


@dataclass(frozen=True)
class Rule:
    rule_key: str
    quota_bytes: int
    effective_at_ms: int
    generation: int
    source: str
    source_version: str
    conversion: dict[str, Any] | None = None


@dataclass(frozen=True)
class V1Snapshot:
    manifest_version: str
    base_clock_ms: int
    rules: tuple[Rule, ...]


@dataclass(frozen=True)
class V2Snapshot:
    version: str
    min_generation: int
    key_digest: str
    shard_hashes: dict[str, str]
    keys: tuple[str, ...]
    rules: tuple[Rule, ...]

    def signature(self) -> dict[str, Any]:
        return {
            "key_digest": self.key_digest,
            "min_generation": self.min_generation,
            "shard_hashes": dict(sorted(self.shard_hashes.items())),
        }


@dataclass
class Attempt:
    dir_id: str
    file_key: str
    bytes: int
    quota: int | None
    consumed_before: int
    available_before: int | None
    status: str
    reason: str | None = None
    rolled_back: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "dir_id": self.dir_id,
            "file_key": self.file_key,
            "bytes": self.bytes,
            "quota": self.quota,
            "consumed_before": self.consumed_before,
            "available_before": self.available_before,
            "status": self.status,
            "reason": self.reason,
            "rolled_back": self.rolled_back,
        }


@dataclass
class AuditDecision:
    seq: int
    dir_id: str
    file_key: str
    bytes: int
    digest: str
    clock_ms: int
    status: str
    fail_dir: str | None = None
    reason: str | None = None
    deducted_nodes: int = 0
    deducted_bytes: int = 0
    attempts: list[Attempt] = field(default_factory=list)

    def attempts_json(self) -> str:
        import json

        return json.dumps(
            [attempt.to_dict() for attempt in self.attempts],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


@dataclass
class SimulationResult:
    ledger: dict[tuple[str, str], dict[str, Any]]
    audit: list[AuditDecision]
    max_clock_ms: int

