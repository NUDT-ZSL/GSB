from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .hashing import canonical_hash, read_json, write_json_atomic
from .workspace import CaseWorkspace


STAGES = (
    (1, "parse_validate"),
    (2, "migrate_semantic"),
    (3, "incremental_recompute"),
    (4, "result_summary"),
)


@dataclass
class StateStore:
    ws: CaseWorkspace

    def __post_init__(self) -> None:
        self.ws.ensure_dirs()

    @property
    def latest_path(self) -> Path:
        return self.ws.latest_snapshot_path

    def latest(self) -> dict[str, Any] | None:
        if not self.latest_path.exists():
            return None
        return read_json(self.latest_path)

    def next_version(self) -> int:
        latest = self.latest()
        return 1 if latest is None else int(latest["snapshot_version"]) + 1

    def reset_uncommitted_work(self) -> None:
        if self.ws.work_dir.exists():
            shutil.rmtree(self.ws.work_dir)
        self.ws.work_dir.mkdir(parents=True, exist_ok=True)

    def commit_stage(
        self,
        *,
        stage_index: int,
        stage: str,
        write_version: int,
        all_intervals: list[str],
        completed_intervals: list[str],
        csr_digest: str,
        cache_keys: list[str] | None = None,
        report: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if set(completed_intervals) != set(all_intervals) or len(completed_intervals) != len(
            all_intervals
        ):
            raise ValueError("a stage snapshot can commit only when all intervals completed")
        previous = self.latest()
        snapshot_version = self.next_version()
        payload: dict[str, Any] = {
            "snapshot_version": snapshot_version,
            "stage_index": stage_index,
            "stage": stage,
            "write_version": int(write_version),
            "all_intervals": list(all_intervals),
            "completed_intervals": list(completed_intervals),
            "completed_interval_count": len(completed_intervals),
            "row_interval_set_hash": canonical_hash(all_intervals),
            "csr_digest": csr_digest,
            "cache_keys": sorted(cache_keys or []),
            "report": report or {},
        }
        if extra:
            payload.update(extra)
        if previous is not None:
            for carried_field in ("csr_dict", "lineage"):
                if carried_field not in payload and carried_field in previous:
                    payload[carried_field] = previous[carried_field]
        payload["content_checksum"] = canonical_hash(
            {key: value for key, value in payload.items() if key != "content_checksum"}
        )
        snapshot_path = (
            self.ws.snapshots_dir
            / f"snapshot_{snapshot_version:05d}_{stage}.json"
        )
        write_json_atomic(snapshot_path, payload)
        write_json_atomic(self.latest_path, payload)
        return payload

    def append_stage_record(self, record: dict[str, Any]) -> None:
        self.ws.root.mkdir(parents=True, exist_ok=True)
        with self.ws.stage_records_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def load_all_snapshots(ws: CaseWorkspace) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    for path in sorted(ws.snapshots_dir.glob("snapshot_*.json")):
        snapshots.append(read_json(path))
    return snapshots
