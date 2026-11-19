from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .canonical import LogicalClock
from .crash import CrashInjector
from .errors import fail
from .files import JsonlSpool, digest_file
from .locking import exclusive_file_lock
from .storage import StateStore


Emitter = Callable[[dict[str, Any]], None]
Producer = Callable[[Emitter], None]


@dataclass
class JoinMetrics:
    requested_algorithm: str
    actual_algorithms: dict[int, str] = field(default_factory=dict)
    fallback_events: list[dict[str, Any]] = field(default_factory=list)
    rows_built: int = 0
    rows_probed: int = 0
    peak_partition_rows: int = 0
    spill_bytes: int = 0
    result_rows_new: int = 0
    partitions_processed: int = 0
    partitions_skipped: int = 0
    external_sort_runs: int = 0

    def record_actual(self, partition_id: int, actual: str) -> None:
        self.actual_algorithms[int(partition_id)] = actual
        if actual != self.requested_algorithm:
            self.fallback_events.append(
                {
                    "partition": int(partition_id),
                    "requested": self.requested_algorithm,
                    "actual": actual,
                    "reason": "partition build side exceeded in-memory hash budget"
                    if actual == "sort_merge"
                    else "broadcast build side exceeded broadcast budget",
                }
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_algorithm": self.requested_algorithm,
            "actual_algorithms": {
                str(key): value for key, value in sorted(self.actual_algorithms.items())
            },
            "fallback_events": self.fallback_events,
            "rows_built": self.rows_built,
            "rows_probed": self.rows_probed,
            "peak_partition_rows": self.peak_partition_rows,
            "spill_bytes": self.spill_bytes,
            "result_rows_new": self.result_rows_new,
            "partitions_processed": self.partitions_processed,
            "partitions_skipped": self.partitions_skipped,
            "external_sort_runs": self.external_sort_runs,
        }


@dataclass
class SinkResult:
    commit: dict[str, Any]
    newly_committed: bool
    rows_written: int


class StoredResultSink:
    def __init__(
        self,
        state: StateStore,
        *,
        query_id: str,
        version: int,
        requested_algorithm: str,
        clock: LogicalClock,
        crash: CrashInjector,
        metrics: JoinMetrics,
    ) -> None:
        self.state = state
        self.query_id = query_id
        self.version = version
        self.requested_algorithm = requested_algorithm
        self.clock = clock
        self.crash = crash
        self.metrics = metrics
        self.newly_committed = 0

    def existing(self, partition_id: int) -> dict[str, Any] | None:
        commit = self.state.get_result_commit(
            self.query_id, self.version, partition_id
        )
        if commit is None:
            return None
        path = self.state.path_for(commit["relative_path"])
        if not path.exists():
            fail(
                "E_STATE_CORRUPT",
                "committed result partition file is missing",
                details={"partition_id": partition_id, "path": commit["relative_path"]},
                exit_code=3,
            )
        digest, size = digest_file(path)
        if digest != commit["digest"] or size != int(commit["bytes"]):
            fail(
                "E_STATE_CORRUPT",
                "committed result partition file is corrupt",
                details={"partition_id": partition_id, "path": commit["relative_path"]},
                exit_code=3,
            )
        return commit

    def process(
        self,
        partition_id: int,
        actual_algorithm: str,
        producer: Producer,
    ) -> SinkResult:
        lock_path = self.state.partition_lock_path(
            self.query_id, self.version, "result", partition_id
        )
        with exclusive_file_lock(lock_path):
            existing = self.existing(partition_id)
            if existing is not None:
                self.metrics.partitions_skipped += 1
                return SinkResult(existing, False, int(existing["rows"]))
            directory = (
                self.state.root
                / "data"
                / f"v{self.version}"
                / "results"
                / self.requested_algorithm
            )
            final_path = directory / f"part-{partition_id:05d}.jsonl"
            spool = JsonlSpool(final_path, label=f"result:{partition_id}")
            start_clock = self.clock.current
            try:
                spool.open()
                producer(spool.write)
                digest, size, row_count, _ = spool.commit()
            except BaseException:
                spool.discard()
                raise
            relative_path = self.state.relative(final_path)
            self.state.commit_result_partition(
                self.query_id,
                self.version,
                partition_id,
                requested_algorithm=self.requested_algorithm,
                actual_algorithm=actual_algorithm,
                relative_path=relative_path,
                rows=row_count,
                size_bytes=size,
                digest=digest,
                clock_cost=self.clock.current - start_clock,
                logical_clock=self.clock.current,
            )
            commit = self.state.get_result_commit(
                self.query_id, self.version, partition_id
            )
            assert commit is not None
            self.metrics.result_rows_new += row_count
            self.metrics.partitions_processed += 1
            self.newly_committed += 1
            self.crash.maybe("join_after", self.newly_committed)
            return SinkResult(commit, True, row_count)


class LocalResultSink:
    def __init__(
        self,
        root: Path,
        *,
        requested_algorithm: str,
        clock: LogicalClock,
        metrics: JoinMetrics,
    ) -> None:
        self.root = root
        self.requested_algorithm = requested_algorithm
        self.clock = clock
        self.metrics = metrics
        self.commits: dict[int, dict[str, Any]] = {}

    def existing(self, partition_id: int) -> dict[str, Any] | None:
        return self.commits.get(partition_id)

    def process(
        self,
        partition_id: int,
        actual_algorithm: str,
        producer: Producer,
    ) -> SinkResult:
        directory = self.root / self.requested_algorithm
        final_path = directory / f"part-{partition_id:05d}.jsonl"
        spool = JsonlSpool(final_path, label=f"local-result:{partition_id}")
        try:
            spool.open()
            producer(spool.write)
            digest, size, row_count, _ = spool.commit()
        except BaseException:
            spool.discard()
            raise
        commit = {
            "partition_id": partition_id,
            "relative_path": str(final_path),
            "rows": row_count,
            "bytes": size,
            "digest": digest,
            "requested_algorithm": self.requested_algorithm,
            "actual_algorithm": actual_algorithm,
        }
        self.commits[partition_id] = commit
        self.metrics.result_rows_new += row_count
        self.metrics.partitions_processed += 1
        return SinkResult(commit, True, row_count)

