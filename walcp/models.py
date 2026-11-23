from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class WalRow:
    segment: str
    segment_no: int
    line_no: int
    txn_id: int
    table: str
    key: str
    op: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    checkpoint_gen: int
    content_hash: str
    commit_ms: int

    def hash_payload(self) -> dict[str, Any]:
        return {
            "txn_id": self.txn_id,
            "table": self.table,
            "key": self.key,
            "op": self.op,
            "before": self.before,
            "after": self.after,
            "checkpoint_gen": self.checkpoint_gen,
            "commit_ms": self.commit_ms,
        }

    def as_record(self, deleted: bool = False) -> dict[str, Any]:
        return {
            "segment": self.segment,
            "segment_no": self.segment_no,
            "line_no": self.line_no,
            "txn_id": self.txn_id,
            "table": self.table,
            "key": self.key,
            "op": self.op,
            "before": self.before,
            "after": self.after,
            "checkpoint_gen": self.checkpoint_gen,
            "content_hash": self.content_hash,
            "commit_ms": self.commit_ms,
            "deleted": deleted,
        }

    def with_changes(self, changes: dict[str, Any]) -> "WalRow":
        values = self.as_record()
        values.update(changes)
        values["content_hash"] = ""
        from .wal import compute_content_hash

        values["content_hash"] = compute_content_hash(values)
        return WalRow(
            segment=values["segment"],
            segment_no=values["segment_no"],
            line_no=values["line_no"],
            txn_id=values["txn_id"],
            table=values["table"],
            key=values["key"],
            op=values["op"],
            before=values["before"],
            after=values["after"],
            checkpoint_gen=values["checkpoint_gen"],
            content_hash=values["content_hash"],
            commit_ms=values["commit_ms"],
        )


@dataclass(frozen=True)
class SnapshotRow:
    table: str
    key: str
    version: int
    txn_id: int
    deleted: bool
    value: dict[str, Any] | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "key": self.key,
            "version": self.version,
            "txn_id": self.txn_id,
            "deleted": self.deleted,
            "value": self.value,
        }


@dataclass
class CheckpointVersion:
    generation: int
    source_format: str
    source_path: str
    source_hash: str
    saved_at_ms: int
    replay_generation: int
    applied_watermark: int
    snapshot_rows: list[SnapshotRow]
    tables: dict[str, dict[str, Any]]
    manifest_version: int = 0
    base_generation: int = 0
    base_manifest_hash: str | None = None
    manifest_hash: str | None = None
    manifest: dict[str, Any] = field(default_factory=dict)
    conversion: dict[str, Any] | None = None
    shards: list[dict[str, Any]] = field(default_factory=list)
    selected: bool = False
    decision_reason: dict[str, Any] = field(default_factory=dict)
    invalidated: bool = False
    version_id: int | None = None


@dataclass
class MergeResult:
    versions: list[CheckpointVersion]
    selected: dict[int, CheckpointVersion]
    baseline: CheckpointVersion | None
    anchor_ms: int

    @property
    def generations(self) -> set[int]:
        return set(self.selected)

    def report(self) -> dict[str, Any]:
        return {
            "anchor_ms": self.anchor_ms,
            "baseline_generation": (
                self.baseline.generation if self.baseline is not None else 0
            ),
            "baseline_source_format": (
                self.baseline.source_format if self.baseline is not None else None
            ),
            "generations": [
                {
                    "generation": generation,
                    "source_format": version.source_format,
                    "saved_at_ms": version.saved_at_ms,
                    "replay_generation": version.replay_generation,
                    "applied_watermark": version.applied_watermark,
                    "selected": version.selected,
                    "decision_reason": version.decision_reason,
                    "v1_conversion": version.conversion,
                    "source_path": version.source_path,
                    "source_hash": version.source_hash,
                }
                for generation, version in sorted(self.selected.items())
            ],
            "source_versions": [
                {
                    "generation": version.generation,
                    "source_format": version.source_format,
                    "saved_at_ms": version.saved_at_ms,
                    "replay_generation": version.replay_generation,
                    "applied_watermark": version.applied_watermark,
                    "selected": version.selected,
                    "decision_reason": version.decision_reason,
                    "v1_conversion": version.conversion,
                    "source_path": version.source_path,
                    "source_hash": version.source_hash,
                }
                for version in self.versions
            ],
        }
