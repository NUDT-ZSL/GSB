"""Shared immutable domain models."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Event:
    """One normalized KLP key-operation event."""

    session_id: str
    stage: str
    algorithm: str
    seq: int
    iterations: int
    salt: bytes
    output_length: int
    level: int
    sync_marker: int
    record_index: int
    offset: int

    @property
    def salt_length(self) -> int:
        return len(self.salt)

    def semantic_key(self) -> tuple[object, ...]:
        return (
            self.stage,
            self.algorithm,
            self.seq,
            self.iterations,
            self.salt,
            self.output_length,
            self.level,
            self.sync_marker,
        )

    def params(self) -> dict[str, int]:
        return {
            "iterations": self.iterations,
            "salt_length": self.salt_length,
            "output_length": self.output_length,
            "level": self.level,
        }


@dataclass(frozen=True)
class RejectedRecord:
    record_index: int
    offset: int | None
    code: str
    message: str

    def public_dict(self) -> dict[str, object]:
        return {
            "record_index": self.record_index,
            "offset": self.offset,
            "code": self.code,
            "message": self.message,
        }


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    stage: str | None = None
    seq: int | None = None
    offset: int | None = None
    node: str | None = None
    parent_node: str | None = None
    chain_id: str | None = None
    path: tuple[str, ...] = ()
    details: dict[str, object] = field(default_factory=dict)

    def public_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "code": self.code,
            "message": self.message,
            "stage": self.stage,
            "stage_seq": self.seq,
            "offset": self.offset,
        }
        if self.node is not None:
            data["node"] = self.node
        if self.parent_node is not None:
            data["parent_node"] = self.parent_node
        if self.chain_id is not None:
            data["chain_id"] = self.chain_id
        if self.path:
            data["path"] = list(self.path)
        if self.details:
            data["details"] = dict(sorted(self.details.items()))
        return data


@dataclass(frozen=True)
class ParsedFile:
    data_size: int
    file_sha256: str
    max_iterations: int
    record_count: int
    events: tuple[Event, ...]
    rejected: tuple[RejectedRecord, ...]
    fatal_code: str | None = None
    fatal_message: str | None = None

    @property
    def ok(self) -> bool:
        return self.fatal_code is None


@dataclass
class SessionData:
    session_id: str
    events: list[Event]
    deduplicated_count: int = 0
    normalization_issues: list[Issue] = field(default_factory=list)

