from __future__ import annotations

from pathlib import Path
from typing import Any

from .canonical import BASE_LOGICAL_MS, LogicalClock, atomic_write_json, read_json, sha256_json
from .errors import Codes, WorkbenchError
from .resources import CommitLock


STAGES = [
    ("validation_plan", "校验计划"),
    ("block_accumulate", "乘加累加"),
    ("aggregate_verify", "聚合校验"),
    ("report_export", "报告导出"),
]


class SnapshotManager:
    def __init__(self, state_dir: str | Path, clock: LogicalClock | None = None) -> None:
        self.state_dir = Path(state_dir)
        self.snapshot_dir = self.state_dir / "snapshots"
        self.counter_path = self.snapshot_dir / "counter.json"
        self.run_index_path = self.snapshot_dir / "run-index.json"
        self.latest_path = self.snapshot_dir / "latest.json"
        self.lock_path = self.state_dir / "locks" / "commit.lock"
        self.clock = clock or LogicalClock()
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

    def _counter(self) -> int:
        if not self.counter_path.exists():
            return 0
        return int(read_json(self.counter_path).get("last_version", 0))

    def _run_index(self) -> dict[str, Any]:
        if not self.run_index_path.exists():
            return {"runs": {}}
        return read_json(self.run_index_path)

    def latest_for_run(self, run_id: str) -> dict[str, Any] | None:
        index = self._run_index().get("runs", {})
        summary = index.get(run_id, {}).get("latest")
        if not summary:
            return None
        return read_json(self.state_dir / summary["path"])

    def stage_snapshots_for_run(self, run_id: str) -> list[dict[str, Any]]:
        run = self._run_index().get("runs", {}).get(run_id)
        if not run:
            return []
        snapshots = [read_json(self.state_dir / item["path"]) for item in run.get("snapshots", [])]
        return sorted(snapshots, key=lambda item: item["version"])

    def committed_cache_keys(self) -> set[str]:
        keys: set[str] = set()
        index = self._run_index().get("runs", {})
        for run in index.values():
            for item in run.get("snapshots", []):
                snapshot = read_json(self.state_dir / item["path"])
                for artifact in snapshot.get("job_artifacts", {}).values():
                    for node in artifact.get("nodes", {}).values():
                        if node.get("key"):
                            keys.add(node["key"])
        return keys

    def commit_snapshot(
        self,
        *,
        run_id: str,
        stage: str,
        stage_index: int,
        completed_jobs: list[str],
        job_artifacts: dict[str, Any],
        extra: dict[str, Any] | None = None,
        pre_commit: Any | None = None,
    ) -> dict[str, Any]:
        with CommitLock(self.lock_path):
            version = self._counter() + 1
            if pre_commit is not None:
                pre_commit()
            jobs = sorted(completed_jobs)
            state_checksum = sha256_json(
                {
                    "run_id": run_id,
                    "stage": stage,
                    "version": version,
                    "completed_jobs": jobs,
                    "job_artifacts": job_artifacts,
                }
            )
            snapshot = {
                "version": version,
                "run_id": run_id,
                "stage": stage,
                "stage_label": dict(STAGES)[stage],
                "stage_index": stage_index,
                "committed_logical_ms": BASE_LOGICAL_MS + version * 1000,
                "completed_jobs": jobs,
                "job_count": len(jobs),
                "job_artifacts": job_artifacts,
                "state_checksum": state_checksum,
            }
            snapshot["snapshot_checksum"] = sha256_json(
                {key: value for key, value in snapshot.items() if key != "snapshot_checksum"}
            )
            filename = f"snapshot-{version:06d}-{stage}.json"
            relative_path = Path("snapshots") / filename
            atomic_write_json(self.snapshot_dir / filename, snapshot)
            atomic_write_json(self.counter_path, {"last_version": version})

            index = self._run_index()
            run = index.setdefault("runs", {}).setdefault(run_id, {"snapshots": []})
            summary = {
                "version": version,
                "stage": stage,
                "path": str(relative_path),
                "completed_jobs": jobs,
                "state_checksum": state_checksum,
            }
            run["snapshots"].append(summary)
            run["latest"] = summary
            atomic_write_json(self.run_index_path, index)
            atomic_write_json(
                self.latest_path,
                {"version": version, "run_id": run_id, "stage": stage, "path": str(relative_path)},
            )
            if extra:
                atomic_write_json(self.state_dir / "last-commit-extra.json", extra)
            return snapshot

    def recovery_position(self, run_id: str) -> tuple[int, str | None, dict[str, Any] | None]:
        snapshot = self.latest_for_run(run_id)
        if snapshot is None:
            return 0, None, None
        stage = snapshot["stage"]
        for index, (name, _label) in enumerate(STAGES, 1):
            if name == stage:
                return index, stage, snapshot
        raise WorkbenchError(Codes.STATE_INVALID, f"unknown committed stage {stage!r}")


def verify_snapshot_checksum(snapshot: dict[str, Any]) -> bool:
    expected = snapshot.get("snapshot_checksum")
    body = {key: value for key, value in snapshot.items() if key != "snapshot_checksum"}
    return bool(expected) and sha256_json(body) == expected
