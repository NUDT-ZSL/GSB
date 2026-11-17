"""Atomic stage snapshots and working-graph persistence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import SnapshotInvalidError
from .util import atomic_write_json


@dataclass
class SnapshotState:
    path: Path
    data: dict[str, Any]


class SessionStore:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir = self.directory / "snapshots"
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir = self.directory / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.latest_path = self.snapshot_dir / "latest.json"
        self.working_graph_path = self.directory / "working_graph.json"
        self._version = 0
        self._loaded = self._read_latest()
        if self._loaded is not None:
            self._version = int(self._loaded.get("snapshot_version", 0))

    def _read_latest(self) -> dict[str, Any] | None:
        if not self.latest_path.exists():
            return None
        try:
            data = json.loads(self.latest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SnapshotInvalidError(f"completed snapshot cannot be decoded: {exc}") from exc
        if not isinstance(data, dict):
            raise SnapshotInvalidError("snapshot root must be an object")
        if not isinstance(data.get("snapshot_version"), int):
            raise SnapshotInvalidError("snapshot_version must be an integer")
        if not isinstance(data.get("config_hash"), str):
            raise SnapshotInvalidError("snapshot config_hash must be a string")
        return data

    @property
    def loaded_snapshot(self) -> SnapshotState | None:
        if self._loaded is None:
            return None
        return SnapshotState(path=self.latest_path, data=self._loaded)

    @property
    def snapshot_version(self) -> int:
        return self._version

    def commit(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._version += 1
        value = dict(payload)
        value["snapshot_version"] = self._version
        atomic_write_json(self.latest_path, value)
        self._loaded = value
        return value

    def write_working_graph(
        self,
        base_graph_hash: str,
        graph: dict[str, Any],
        runtime: dict[str, Any] | None = None,
    ) -> None:
        atomic_write_json(
            self.working_graph_path,
            {
                "schema_version": 1,
                "base_graph_hash": base_graph_hash,
                "graph": graph,
                "runtime": runtime or {},
            },
        )

    def read_working_graph(self) -> dict[str, Any] | None:
        if not self.working_graph_path.exists():
            return None
        try:
            data = json.loads(self.working_graph_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("graph"), dict):
            return None
        return data
