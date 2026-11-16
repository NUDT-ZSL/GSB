"""High-level session service used by the CLI, demo and Tkinter frontend."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .errors import FormatError, ParameterError
from .estimator import (
    DEFAULT_FIXED_SCALE,
    DEFAULT_MIN_SAMPLES,
    ESTIMATOR_PARAMETERS_VERSION,
    EstimatorParameters,
    ReplayParameters,
    estimate_delays,
    estimator_parameters_from_dict,
)
from .replay import BatchCallback, CrashHook, ReplayEngine
from .segment import (
    CURRENT_FORMAT_VERSION,
    LogRecord,
    Segment,
    build_segment_bytes,
    collect_records,
    iter_segment_paths,
    parse_segment,
    read_segment,
    validate_collection,
)
from .snapshot import SnapshotManager
from .storage import ProgressStore
from .upgrade import UpgradeManager
from .util import atomic_write_bytes, canonical_json_bytes, ensure_dir, sha256_bytes


DEFAULT_SESSION_ID = "default"
DEFAULT_MASTER_ORIGIN_MS = 1_000_000


@dataclass(frozen=True)
class LabPaths:
    root: Path

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def backups(self) -> Path:
        return self.root / "backups"

    @property
    def checkpoints(self) -> Path:
        return self.root / "checkpoints"

    @property
    def snapshots(self) -> Path:
        return self.root / "snapshots"

    @property
    def archives(self) -> Path:
        return self.root / "archives"

    @property
    def database(self) -> Path:
        return self.root / "lab.sqlite3"

    @property
    def upgrade_checkpoint(self) -> Path:
        return self.checkpoints / "upgrade_checkpoint.json"

    def ensure(self) -> None:
        for path in (
            self.logs,
            self.backups,
            self.checkpoints,
            self.snapshots,
            self.archives,
        ):
            ensure_dir(path)


class LabSession:
    def __init__(
        self,
        root: str | Path,
        *,
        auto_upgrade: bool = True,
        session_id: str = DEFAULT_SESSION_ID,
        estimator_parameters: EstimatorParameters | None = None,
    ) -> None:
        self.paths = LabPaths(Path(root).resolve())
        self.paths.ensure()
        self.session_id = session_id
        self.estimator_parameters = estimator_parameters or EstimatorParameters(
            version=ESTIMATOR_PARAMETERS_VERSION,
            fixed_scale=DEFAULT_FIXED_SCALE,
            min_samples=DEFAULT_MIN_SAMPLES,
        )
        self.store = ProgressStore(self.paths.database)
        self.upgrader = UpgradeManager(
            self.paths.logs,
            self.paths.backups,
            self.paths.upgrade_checkpoint,
        )
        self.engine = ReplayEngine(
            self.store,
            session_id=self.session_id,
            estimator_parameters=self.estimator_parameters,
        )
        self.snapshot_manager = SnapshotManager(
            root=self.paths.root,
            log_dir=self.paths.logs,
            archive_dir=self.paths.archives,
            store=self.store,
            session_id=self.session_id,
            estimator_parameters=self.estimator_parameters,
        )
        self.segments: list[Segment] = []
        self.format_errors: list[FormatError] = []
        self.upgrade_report: dict[str, Any] | None = None
        if auto_upgrade:
            self.upgrade_report = self.upgrader.run()
        self.refresh()

    @classmethod
    def open(
        cls,
        root: str | Path,
        *,
        auto_upgrade: bool = True,
        session_id: str = DEFAULT_SESSION_ID,
        estimator_parameters: EstimatorParameters | None = None,
    ) -> "LabSession":
        return cls(
            root,
            auto_upgrade=auto_upgrade,
            session_id=session_id,
            estimator_parameters=estimator_parameters,
        )

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> "LabSession":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def refresh(self) -> list[Segment]:
        segments: list[Segment] = []
        errors: list[FormatError] = []
        for path in iter_segment_paths(self.paths.logs):
            try:
                segments.append(read_segment(path))
            except FormatError as exc:
                errors.append(exc)
        segments.sort(key=lambda item: (item.segment_id, item.filename))
        if not errors:
            try:
                validate_collection(segments)
            except FormatError as exc:
                errors.append(exc)
        self.segments = segments
        self.format_errors = errors
        if not errors:
            self.store.replace_operations(segments)
        return list(segments)

    def ensure_usable(self) -> None:
        if self.format_errors:
            raise self.format_errors[0]

    def version_set(self) -> list[int]:
        return sorted(
            {version for segment in self.segments for version in segment.versions}
        )

    def records(self) -> list[LogRecord]:
        return collect_records(self.segments)

    def segment_entries(self) -> list[dict[str, object]]:
        return [segment.to_manifest_entry() for segment in self.segments]

    def _next_segment_id(self) -> int:
        used = {segment.segment_id for segment in self.segments}
        candidate = max(used, default=0) + 1
        while (self.paths.logs / f"seg-{candidate:08d}.rlog").exists():
            candidate += 1
        if candidate >= (1 << 32):
            raise ParameterError("segment_id space exhausted")
        return candidate

    def write_operations(
        self,
        operations: Sequence[Mapping[str, Any]],
        *,
        records_per_segment: int = 4,
        master_origin_ms: int = DEFAULT_MASTER_ORIGIN_MS,
    ) -> dict[str, Any]:
        self.ensure_usable()
        if records_per_segment <= 0:
            raise ParameterError("records_per_segment must be a positive integer")
        if not operations:
            return {"business_code": None, "written_record_count": 0, "segments": []}
        existing = self.records()
        previous_seq = existing[-1].commit_seq if existing else 0
        previous_ts = max(
            (record.commit_ts_ms for record in existing if record.commit_ts_ms is not None),
            default=master_origin_ms - 1,
        )
        logical_now = max(previous_ts, master_origin_ms - 1)
        new_records: list[LogRecord] = []
        for index, operation in enumerate(operations):
            commit_seq = operation.get("commit_seq")
            if commit_seq is None:
                commit_seq = previous_seq + 1
            if not isinstance(commit_seq, int) or isinstance(commit_seq, bool):
                raise ParameterError("commit_seq must be an integer")
            if commit_seq <= previous_seq:
                raise ParameterError(
                    "commit_seq must be strictly increasing",
                    details={"previous": previous_seq, "requested": commit_seq},
                )
            txn_id = operation.get("txn_id")
            if txn_id is None:
                txn_id = 900_000 + commit_seq
            if not isinstance(txn_id, int) or isinstance(txn_id, bool) or txn_id <= 0:
                raise ParameterError("txn_id must be a positive integer")
            commit_ts_ms = operation.get("commit_ts_ms")
            if commit_ts_ms is None and operation.get("commit_offset_ms") is not None:
                offset = operation["commit_offset_ms"]
                if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
                    raise ParameterError("commit_offset_ms must be a non-negative integer")
                commit_ts_ms = master_origin_ms + offset
            if commit_ts_ms is None:
                delta = operation.get("commit_delta_ms", 1)
                if not isinstance(delta, int) or isinstance(delta, bool) or delta <= 0:
                    raise ParameterError("commit_delta_ms must be a positive integer")
                commit_ts_ms = logical_now + delta
            if not isinstance(commit_ts_ms, int) or isinstance(commit_ts_ms, bool):
                raise ParameterError("commit_ts_ms must be an integer")
            if commit_ts_ms <= previous_ts:
                raise ParameterError(
                    "master logical clock is not strictly increasing",
                    details={"previous": previous_ts, "requested": commit_ts_ms},
                )
            txn_size = operation.get("txn_size_bytes", 0)
            if not isinstance(txn_size, int) or isinstance(txn_size, bool) or txn_size < 0:
                raise ParameterError("txn_size_bytes must be a non-negative integer")
            record = LogRecord(
                format_version=CURRENT_FORMAT_VERSION,
                commit_seq=commit_seq,
                txn_id=txn_id,
                commit_ts_ms=commit_ts_ms,
                txn_size_bytes=txn_size,
                segment_id=None,
                segment_offset=None,
            )
            new_records.append(record)
            previous_seq = commit_seq
            previous_ts = commit_ts_ms
            logical_now = commit_ts_ms

        created: list[dict[str, Any]] = []
        segment_id = self._next_segment_id()
        for start in range(0, len(new_records), records_per_segment):
            chunk = new_records[start : start + records_per_segment]
            data = build_segment_bytes(
                chunk,
                segment_id=segment_id,
                target_version=CURRENT_FORMAT_VERSION,
                filename=f"seg-{segment_id:08d}.rlog",
            )
            filename = f"seg-{segment_id:08d}.rlog"
            path = self.paths.logs / filename
            atomic_write_bytes(path, data)
            created.append(
                {
                    "filename": filename,
                    "segment_id": segment_id,
                    "record_count": len(chunk),
                    "first_commit_seq": chunk[0].commit_seq,
                    "last_commit_seq": chunk[-1].commit_seq,
                    "format_version": CURRENT_FORMAT_VERSION,
                    "digest": sha256_bytes(data),
                }
            )
            segment_id += 1
        self.refresh()
        return {
            "business_code": None,
            "written_record_count": len(new_records),
            "segments": created,
            "format_version_set": self.version_set(),
        }

    def upgrade_all(self) -> dict[str, Any]:
        report = self.upgrader.run()
        self.upgrade_report = report
        self.refresh()
        return report

    def recover(self) -> dict[str, Any]:
        return self.engine.recover()

    def replay(
        self,
        *,
        replay_rate: int,
        concurrency: int,
        reset: bool = False,
        clock_origin_ms: int | None = None,
        crash_hook: CrashHook | None = None,
        on_batch_complete: BatchCallback | None = None,
        max_new_batches: int | None = None,
    ) -> dict[str, Any]:
        self.ensure_usable()
        return self.engine.run(
            self.segments,
            replay_rate=replay_rate,
            concurrency=concurrency,
            reset=reset,
            clock_origin_ms=clock_origin_ms,
            crash_hook=crash_hook,
            on_batch_complete=on_batch_complete,
            max_new_batches=max_new_batches,
        )

    def observations(self) -> dict[str, Any]:
        state = self.store.get_state(self.session_id)
        samples = self.store.get_samples(self.session_id)
        batches = self.store.get_batches(self.session_id)
        if state is not None:
            estimate = estimate_delays(
                samples,
                parameters=self.estimator_parameters,
                replay_parameters=ReplayParameters(
                    replay_rate=int(state["replay_rate"]),
                    concurrency=int(state["concurrency"]),
                    clock_origin_ms=int(state["clock_origin_ms"]),
                ),
                incomplete_unknown_batches=int(state["unknown_batch_count"]),
            )
        else:
            estimate = estimate_delays(
                samples, parameters=self.estimator_parameters, replay_parameters=None
            )
        return {
            "session_id": self.session_id,
            "state": state,
            "batches": batches,
            "samples": samples,
            "estimate": estimate,
        }

    def save_snapshot(self) -> dict[str, Any]:
        self.ensure_usable()
        return self.snapshot_manager.save()

    def list_snapshots(self) -> list[dict[str, Any]]:
        return self.snapshot_manager.list()

    def restore_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        result = self.snapshot_manager.restore(snapshot_id)
        estimator_data = result.get("estimator_parameters")
        if isinstance(estimator_data, dict):
            restored_parameters = estimator_parameters_from_dict(estimator_data)
            self.estimator_parameters = restored_parameters
            self.engine.estimator_parameters = restored_parameters
            self.snapshot_manager.estimator_parameters = restored_parameters
        self.refresh()
        return result

    def verify_snapshot(
        self,
        snapshot_id: str,
        *,
        replay_rate: int | None = None,
        concurrency: int | None = None,
    ) -> dict[str, Any]:
        return self.snapshot_manager.verify(
            snapshot_id,
            replay_rate=replay_rate,
            concurrency=concurrency,
        )

    def snapshot_diff(self, left_id: str, right_id: str) -> dict[str, Any]:
        return self.snapshot_manager.diff(left_id, right_id)

    def compare_snapshot_with_disk(self, snapshot_id: str) -> dict[str, Any]:
        return self.snapshot_manager.compare_with_disk(snapshot_id)

    def observation_report(self) -> dict[str, Any]:
        observations = self.observations()
        stats = observations["estimate"]
        return {
            "business_code": None,
            "session_id": self.session_id,
            "format_version_set": self.version_set(),
            "segment_count": len(self.segments),
            "record_count": sum(segment.record_count for segment in self.segments),
            "segments": [
                {
                    "filename": segment.filename,
                    "segment_id": segment.segment_id,
                    "format_version": segment.format_version,
                    "versions": list(segment.versions),
                    "original_versions": list(segment.original_versions),
                    "record_count": segment.record_count,
                    "digest": segment.digest,
                    "digest_short": segment.digest[:12],
                }
                for segment in self.segments
            ],
            "replay": {
                "state": observations["state"],
                "batches": observations["batches"],
                "sample_count": len(observations["samples"]),
                "samples": observations["samples"],
            },
            "estimation": stats,
            "sample_count": stats["sample_count"],
            "known_delay_count": stats["known_delay_count"],
            "unknown_delay_count": stats["unknown_delay_count"],
            "estimate_status": stats["estimate_status"],
            "confidence": stats["confidence"],
            "conclusion": self._conclusion(stats),
            "snapshot_count": len(self.list_snapshots()),
            "format_errors": [error.to_dict() for error in self.format_errors],
            "upgrade_report": self.upgrade_report,
        }

    @staticmethod
    def _conclusion(stats: Mapping[str, Any]) -> str:
        if stats["known_delay_count"] == 0:
            return "无已知延迟样本，估算状态 INSUFFICIENT，置信度 NONE。"
        base = (
            f"基于 {stats['known_delay_count']} 个已知样本，均值定点值 "
            f"{stats['mean_fixed']} (Q={stats['mean_fixed_scale']})，"
            f"P95={stats['p95_ms']} ms，最大延迟={stats['max_delay_ms']} ms；"
            f"状态 {stats['estimate_status']}，置信度 {stats['confidence']}。"
        )
        if stats["unknown_delay_count"] or stats["incomplete_unknown_batches"]:
            base += (
                f" 另有 {stats['unknown_delay_count']} 个未知延迟样本、"
                f"{stats['incomplete_unknown_batches']} 个未知批次，未填补也未删除。"
            )
        return base


def load_operations_file(path: str | Path) -> list[dict[str, Any]]:
    import json

    with Path(path).open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if isinstance(value, dict) and "operations" in value:
        value = value["operations"]
    if not isinstance(value, list):
        raise ParameterError("operations file must contain a JSON array or {operations: [...]}")
    return value
