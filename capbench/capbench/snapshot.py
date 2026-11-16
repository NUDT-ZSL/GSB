"""Named snapshot persistence and crash-recovery state.

A snapshot is the single durable truth of a session. It stores:
  * the full scene definition (load parameters at definition time)
  * load parameter version + fingerprint
  * every FINISHED stage result (observation + metrics + verdict)
  * progress markers, including `unknown` stages that were interrupted and
    therefore have no exit report (and must be re-executed after recovery)
  * server configuration used (for replay reproducibility checks)

Writes are atomic (tmp file + os.replace). On restart we only trust what a
snapshot actually contains; unfinished stages are never reconstructed.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

from .errors import SceneError
from .models import StageResult

SNAPSHOT_FORMAT = "snapshot-v1"


@dataclass
class ProgressMarker:
    stage_index: int
    state: str                 # finished | running | unknown
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"stage_index": self.stage_index, "state": self.state,
                "note": self.note}


@dataclass
class Snapshot:
    name: str
    snapshot_format: str
    created_at_wall: str
    scene: dict[str, Any]
    params_version: int
    params_fingerprint: str
    server_config: dict[str, Any]
    stage_results: list[dict[str, Any]] = field(default_factory=list)
    progress: list[dict[str, Any]] = field(default_factory=list)
    status: str = "running"    # running | completed | interrupted

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_format": self.snapshot_format,
            "name": self.name,
            "created_at_wall": self.created_at_wall,
            "status": self.status,
            "params_version": self.params_version,
            "params_fingerprint": self.params_fingerprint,
            "server_config": self.server_config,
            "scene": self.scene,
            "stage_results": self.stage_results,
            "progress": self.progress,
        }

    def result_indexes(self) -> list[int]:
        return sorted(r["stage_index"] for r in self.stage_results)

    def unknown_indexes(self) -> list[int]:
        return sorted(p["stage_index"] for p in self.progress
                      if p["state"] == "unknown")


class SnapshotStore:
    def __init__(self, directory: str) -> None:
        self.directory = directory
        os.makedirs(directory, exist_ok=True)

    def path_of(self, name: str) -> str:
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_"
                       for ch in name)
        return os.path.join(self.directory, f"{safe}.json")

    def save(self, snapshot: Snapshot) -> str:
        path = self.path_of(snapshot.name)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(snapshot.to_dict(), fh, ensure_ascii=False,
                      sort_keys=True, indent=2)
        os.replace(tmp, path)
        return path

    def load(self, name: str) -> Snapshot:
        path = self.path_of(name)
        if not os.path.isfile(path):
            raise FileNotFoundError(f"snapshot not found: {name}")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise SceneError(
                f"snapshot {name} is corrupted",
                field_paths=["<snapshot>"],
                details={"parse_error": str(exc)},
            ) from exc
        return Snapshot(
            name=data["name"],
            snapshot_format=data.get("snapshot_format", SNAPSHOT_FORMAT),
            created_at_wall=data.get("created_at_wall", ""),
            scene=data["scene"],
            params_version=data["params_version"],
            params_fingerprint=data["params_fingerprint"],
            server_config=data.get("server_config", {}),
            stage_results=list(data.get("stage_results", [])),
            progress=list(data.get("progress", [])),
            status=data.get("status", "running"),
        )

    def list_names(self) -> list[str]:
        names = [f[:-5] for f in os.listdir(self.directory)
                 if f.endswith(".json")]
        return sorted(names)

    @staticmethod
    def new(name: str, scene_dict: dict[str, Any], version: int,
            fp: str, server_config: dict[str, Any]) -> Snapshot:
        return Snapshot(
            name=name,
            snapshot_format=SNAPSHOT_FORMAT,
            created_at_wall=time.strftime("%Y-%m-%dT%H:%M:%S"),
            scene=scene_dict,
            params_version=version,
            params_fingerprint=fp,
            server_config=server_config,
        )

    def append_result(self, snapshot: Snapshot, result: StageResult,
                      state: str = "finished", note: str = "") -> str:
        snapshot.stage_results = [
            r for r in snapshot.stage_results
            if r["stage_index"] != result.stage_index
        ]
        snapshot.stage_results.append(result.to_dict())
        snapshot.stage_results.sort(key=lambda r: r["stage_index"])
        snapshot.progress = [
            p for p in snapshot.progress
            if p["stage_index"] != result.stage_index
        ]
        snapshot.progress.append(
            ProgressMarker(result.stage_index, state, note).to_dict())
        snapshot.progress.sort(key=lambda p: p["stage_index"])
        return self.save(snapshot)

    def mark_unknown(self, snapshot: Snapshot, stage_index: int,
                     note: str) -> str:
        snapshot.progress = [
            p for p in snapshot.progress if p["stage_index"] != stage_index
        ]
        snapshot.progress.append(
            ProgressMarker(stage_index, "unknown", note).to_dict())
        snapshot.progress.sort(key=lambda p: p["stage_index"])
        snapshot.status = "interrupted"
        return self.save(snapshot)
