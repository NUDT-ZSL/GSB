"""Stage snapshots persisted via temp file + atomic replace, and signal handling."""
from __future__ import annotations

import os
import signal
import threading

from .atomicio import SNAPSHOT_VERSION, atomic_write_json, read_json
from .errors import BenchError, E_CONFIG_MISMATCH, E_SNAPSHOT_CORRUPT


class ShutdownFlag:
    """Thread-safe cooperative shutdown flag driven by SIGINT/SIGTERM."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._previous: dict[int, object] = {}

    def _handler(self, signum, frame) -> None:  # pragma: no cover - signal path
        self._event.set()

    def install(self) -> None:
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                self._previous[signum] = signal.signal(signum, self._handler)
            except (ValueError, OSError):
                continue

    def restore(self) -> None:
        for signum, handler in self._previous.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                continue

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    def set(self) -> None:
        self._event.set()


class SnapshotStore:
    def __init__(self, work_dir: str, config_fingerprint: str):
        from .hashing import fingerprint
        namespace = fingerprint({"work_dir": os.path.abspath(work_dir),
                                 "config": config_fingerprint},
                                prefix="snap-namespace:")
        self.root = os.path.join(work_dir, "snapshots", namespace)
        self.config_fingerprint = config_fingerprint
        os.makedirs(self.root, exist_ok=True)

    def path_for(self, stage: str, scale_id: str, path_name: str) -> str:
        filename = f"{stage}__{scale_id}__{path_name}.json"
        return os.path.join(self.root, filename)

    def write(self, stage: str, scale_id: str, path_name: str, cursor: int,
              artifact_summary: dict, extra: dict | None = None) -> str:
        payload = {
            "version": SNAPSHOT_VERSION,
            "stage": stage,
            "config_fingerprint": self.config_fingerprint,
            "scale_id": scale_id,
            "path": path_name,
            "cursor": int(cursor),
            "artifact_summary": artifact_summary,
            "extra": extra or {},
        }
        path = self.path_for(stage, scale_id, path_name)
        atomic_write_json(path, payload)
        return path

    def read(self, stage: str, scale_id: str, path_name: str) -> dict | None:
        path = self.path_for(stage, scale_id, path_name)
        if not os.path.isfile(path):
            return None
        try:
            payload = read_json(path)
        except Exception as exc:
            raise BenchError(E_SNAPSHOT_CORRUPT,
                             f"snapshot unreadable at {path}: {exc}") from exc
        if payload.get("config_fingerprint") != self.config_fingerprint:
            raise BenchError(
                E_CONFIG_MISMATCH,
                f"snapshot '{path}' fingerprint {payload.get('config_fingerprint')} "
                f"does not match current {self.config_fingerprint}",
            )
        if payload.get("version") != SNAPSHOT_VERSION:
            raise BenchError(E_SNAPSHOT_CORRUPT,
                             f"snapshot version mismatch at {path}")
        return payload

    def list_versions(self) -> dict[str, int]:
        versions: dict[str, int] = {}
        if not os.path.isdir(self.root):
            return versions
        for filename in sorted(os.listdir(self.root)):
            if not filename.endswith(".json"):
                continue
            payload = read_json(os.path.join(self.root, filename))
            versions[filename[:-5]] = int(payload.get("version", 0))
        return versions
