"""Typed, immutable data model shared by the engine stages."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


CHECKPOINT_STRUCTURE_VERSION = 1
MERGE_SEMANTICS_VERSION = 1
REPORT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class EngineConfig:
    max_workers: int = 1
    pending_host_queue_limit: int = 64
    per_host_timeout_ms: int = 10_000
    max_alerts_per_host: int = 100_000
    checkpoint_every_batch: bool = True

    def validate(self) -> None:
        if isinstance(self.max_workers, bool) or self.max_workers < 1:
            raise ValueError("max_workers must be a positive integer")
        if (
            isinstance(self.pending_host_queue_limit, bool)
            or self.pending_host_queue_limit < 1
        ):
            raise ValueError("pending_host_queue_limit must be a positive integer")
        if (
            isinstance(self.per_host_timeout_ms, bool)
            or self.per_host_timeout_ms < 0
        ):
            raise ValueError("per_host_timeout_ms must be a non-negative integer")
        if (
            isinstance(self.max_alerts_per_host, bool)
            or self.max_alerts_per_host < 0
        ):
            raise ValueError("max_alerts_per_host must be a non-negative integer")


@dataclass(frozen=True)
class Alert:
    alert_id: str
    host_id: str
    alert_ts_ms: int
    rule_key: str
    raw_rule_key: str
    severity: int
    process_guid: str
    parent_process_guid: str | None
    payload_digest: str
    record_index: int


@dataclass(frozen=True)
class RuleVersion:
    rule_key: str
    source_rule_key: str
    source: str
    effective_ms: int
    generation: int
    dedup_window_ms: int
    match_strings: tuple[str, ...]
    enabled: bool
    tombstone: bool
    parent_rule_keys: tuple[str, ...]
    record_index: int
    effective_error_ms: int = 0
    window_error_ms: int = 0
    shard_id: str | None = None
    batch_id: str | None = None
    logical_ts_ms: int | None = None

    @property
    def merge_semantics(self) -> tuple[Any, ...]:
        return (
            self.dedup_window_ms,
            self.match_strings,
            self.enabled,
            self.tombstone,
            self.parent_rule_keys,
        )


@dataclass(frozen=True)
class RulePatch:
    rule_key: str
    source_rule_key: str
    effective_ms: int
    generation: int
    dedup_window_ms: int
    match_strings: tuple[str, ...]
    enabled: bool
    tombstone: bool
    parent_rule_keys: tuple[str, ...]
    record_index: int


@dataclass(frozen=True)
class PatchBatch:
    batch_id: str
    logical_ts_ms: int
    rules: tuple[RulePatch, ...]
    record_index: int
    arrival_index: int


@dataclass
class ParsedInputs:
    observation_clock_ms: int
    severity_min: int
    severity_max: int
    alerts: list[Alert]
    v1_versions: list[RuleVersion]
    v2_versions: list[RuleVersion]
    patch_batches: list[PatchBatch]
    rejections: list[dict]
    input_hashes: dict[str, str]
    v2_duplicate_versions: list[RuleVersion] = field(default_factory=list)


@dataclass(frozen=True)
class RuleCandidate:
    version: RuleVersion


@dataclass(frozen=True)
class VersionResolution:
    status: str
    version: RuleVersion | None = None
    margin_ms: int | None = None
    near_boundary: bool = False
    candidates: tuple[RuleVersion, ...] = ()
    reason_code: str | None = None


@dataclass(frozen=True)
class BoundAlert:
    alert: Alert
    version: RuleVersion
    version_margin_ms: int
    near_boundary: bool
    merge_key: str


@dataclass(frozen=True)
class Cluster:
    cluster_id: str
    host_id: str
    rule_key: str
    payload_digest: str
    merge_key: str
    representative_alert_id: str
    representative_ts_ms: int
    alerts: tuple[tuple[str, int], ...]
    window_ms: int
    window_error_ms: int
    accumulated_error_bound_ms: int
    min_boundary_margin_ms: int
    boundary_margin_total_ms: int
    boundary_checks: int
    process_guids: tuple[str, ...]

    @property
    def alert_ids(self) -> tuple[str, ...]:
        return tuple(alert_id for alert_id, _ in self.alerts)

    def as_dict(self) -> dict:
        return {
            "cluster_id": self.cluster_id,
            "host_id": self.host_id,
            "rule_key": self.rule_key,
            "payload_digest": self.payload_digest,
            "merge_key": self.merge_key,
            "representative_alert_id": self.representative_alert_id,
            "representative_ts_ms": self.representative_ts_ms,
            "alerts": [
                {"alert_id": alert_id, "alert_ts_ms": ts}
                for alert_id, ts in self.alerts
            ],
            "alert_ids": list(self.alert_ids),
            "first_ts_ms": self.alerts[0][1],
            "last_ts_ms": self.alerts[-1][1],
            "window_ms": self.window_ms,
            "window_error_ms": self.window_error_ms,
            "accumulated_error_bound_ms": self.accumulated_error_bound_ms,
            "min_boundary_margin_ms": self.min_boundary_margin_ms,
            "boundary_margin_total_ms": self.boundary_margin_total_ms,
            "boundary_checks": self.boundary_checks,
            "process_guids": list(self.process_guids),
        }


@dataclass
class SchedulerReport:
    hosts: int = 0
    processed_hosts: int = 0
    rejected_hosts: int = 0
    elapsed_ms: float = 0.0
    host_elapsed_ms: dict[str, float] = field(default_factory=dict)
    issues: list[dict] = field(default_factory=list)


@dataclass
class ComputeState:
    bindings: dict[str, dict] = field(default_factory=dict)
    clusters: dict[str, Cluster] = field(default_factory=dict)
    version_ambiguities: list[dict] = field(default_factory=list)
    window_ambiguities: list[dict] = field(default_factory=list)
    proc_cycles: list[dict] = field(default_factory=list)
    scheduler_issues: list[dict] = field(default_factory=list)
    host_elapsed_ms: dict[str, float] = field(default_factory=dict)

    def cluster_list(self) -> list[dict]:
        return [
            self.clusters[key].as_dict() for key in sorted(self.clusters)
        ]

    def sorted_bindings(self) -> list[dict]:
        return [self.bindings[key] for key in sorted(self.bindings)]

    def to_state_dict(self) -> dict:
        return {
            "bindings": self.sorted_bindings(),
            "clusters": self.cluster_list(),
            "version_ambiguities": sorted(
                self.version_ambiguities,
                key=lambda item: (item.get("alert_id", ""), item["code"]),
            ),
            "window_ambiguities": sorted(
                self.window_ambiguities,
                key=lambda item: (item.get("alert_id", ""), item["code"]),
            ),
            "proc_cycles": sorted(
                self.proc_cycles,
                key=lambda item: (item.get("host_id", ""), item["code"]),
            ),
            "scheduler_issues": sorted(
                self.scheduler_issues,
                key=lambda item: (
                    item.get("host_id", ""),
                    item.get("code", ""),
                ),
            ),
        }

    @classmethod
    def from_state_dict(cls, value: dict) -> "ComputeState":
        state = cls()
        state.bindings = {item["alert_id"]: item for item in value.get("bindings", [])}
        for item in value.get("clusters", []):
            cluster = Cluster(
                cluster_id=item["cluster_id"],
                host_id=item["host_id"],
                rule_key=item["rule_key"],
                payload_digest=item["payload_digest"],
                merge_key=item["merge_key"],
                representative_alert_id=item["representative_alert_id"],
                representative_ts_ms=item["representative_ts_ms"],
                alerts=tuple(
                    (alert["alert_id"], alert["alert_ts_ms"])
                    for alert in item["alerts"]
                ),
                window_ms=item["window_ms"],
                window_error_ms=item["window_error_ms"],
                accumulated_error_bound_ms=item["accumulated_error_bound_ms"],
                min_boundary_margin_ms=item["min_boundary_margin_ms"],
                boundary_margin_total_ms=item.get(
                    "boundary_margin_total_ms", 0
                ),
                boundary_checks=item.get("boundary_checks", 0),
                process_guids=tuple(item["process_guids"]),
            )
            state.clusters[cluster.cluster_id] = cluster
        state.version_ambiguities = list(value.get("version_ambiguities", []))
        state.window_ambiguities = list(value.get("window_ambiguities", []))
        state.proc_cycles = list(value.get("proc_cycles", []))
        state.scheduler_issues = list(value.get("scheduler_issues", []))
        return state
