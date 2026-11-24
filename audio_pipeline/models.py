"""Shared data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class InputSpec:
    order: int
    input_id: str
    canonical_id: str
    path: Path
    layout: str
    channels: int
    weights: tuple[float, ...]
    declared_sample_rate: int | None
    declared_bit_depth: int | None
    config_errors: tuple[Any, ...] = field(compare=False, default_factory=tuple)
    raw: dict[str, Any] = field(compare=False, default_factory=dict)


@dataclass(frozen=True)
class MeasurementDefaults:
    window_ms: float
    hop_ms: float
    relative_gate_lu: float
    absolute_gate_lufs: float
    partial_window_policy: str
    dynamic_range_min_db: float
    dynamic_range_max_db: float
    target_lufs: float
    compression_threshold_lufs: float
    compression_ratio: float
    target_sample_rate: int
    output_bit_depth: int
    max_workers: int
    decode_buffer_bytes: int
    sample_buffer_bytes: int
    checkpoint_windows: int
    decode_block_frames: int


@dataclass(frozen=True)
class SnapshotConfig:
    v1_path: Path
    v1_sha256: str
    v1_expected_entries: int | None
    v1_logical_origin_ms: int
    v2_db_path: Path
    v2_db_sha256: str
    v2_shard_root: Path


@dataclass(frozen=True)
class TaskSpec:
    path: Path
    workspace_root: Path
    task_id: str
    task_revision: str
    allowed_sample_rates: frozenset[int]
    allowed_bit_depths: frozenset[int]
    inputs: tuple[InputSpec, ...]
    measurement: MeasurementDefaults
    snapshots: SnapshotConfig
    anomaly_file: Path | None
    anomaly_items: tuple[dict[str, Any], ...]
    report_schema: str
    raw: dict[str, Any] = field(compare=False, default_factory=dict)


@dataclass(frozen=True)
class CriterionRecord:
    canonical_id: str
    criterion: str
    generation: int
    effective_ms: int
    tombstone: bool
    data: dict[str, Any]
    source: dict[str, Any]
    sequence: int

    @property
    def threshold_signature(self) -> tuple[Any, ...]:
        names = (
            "threshold",
            "threshold_db",
            "threshold_lufs",
            "target_lufs",
            "min_db",
            "max_db",
            "window_ms",
            "hop_ms",
            "policy",
            "ratio",
            "enabled",
        )
        signature = tuple(self.data.get(name) for name in names if name in self.data)
        return signature

    @property
    def rank(self) -> tuple[int, int, int, int]:
        return (self.generation, self.effective_ms, int(self.tombstone), self.sequence)


@dataclass(frozen=True)
class UnifiedView:
    records: dict[str, dict[str, CriterionRecord]]
    conflicts: tuple[dict[str, Any], ...]
    issues: tuple[dict[str, Any], ...]
    snapshot_summary: dict[str, Any]
    measurement_revision: str
    decision_revision: str

    def get(self, canonical_id: str, criterion: str,
            default: CriterionRecord | None = None) -> CriterionRecord | None:
        return self.records.get(canonical_id, {}).get(criterion, default)


@dataclass
class InputValidation:
    spec: InputSpec
    info: Any = None
    errors: list[Any] = field(default_factory=list)
    anomaly_ids: list[str] = field(default_factory=list)


@dataclass
class Measurement:
    input_id: str
    canonical_id: str
    source_sample_rate: int
    channels: int
    source_bit_depth: int
    frames: int
    window_frames: int
    hop_frames: int
    window_ms: float
    hop_ms: float
    partial_window_policy: str
    relative_gate_lu: float
    absolute_gate_lufs: float
    total_windows: int
    windows: list[dict[str, Any]]
    integrated_lufs: float | None
    short_term_peak_lufs: float | None
    short_term_floor_lufs: float | None
    dynamic_range_db: float | None
    gated_window_count: int
    abs_gated_window_count: int
    silent: bool
    code: str | None
    measurement_reused: bool = False


@dataclass
class Decision:
    action: str
    gain_db: float
    limiter: bool
    target_lufs: float
    dynamic_range_min_db: float
    dynamic_range_max_db: float
    dynamic_range_verdict: str
    compression_threshold_lufs: float
    compression_ratio: float
    reasons: list[str]
    decision_revision: str


@dataclass
class InputResult:
    order: int
    input_id: str
    canonical_id: str
    path: str
    status: str = "pending"
    codes: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    anomaly_ids: list[str] = field(default_factory=list)
    measurement: Measurement | None = None
    decision: Decision | None = None
    output: dict[str, Any] | None = None
    reused: bool = False
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunOutcome:
    exit_code: int
    report: dict[str, Any]
    report_path: Path | None
    interrupted: bool = False
