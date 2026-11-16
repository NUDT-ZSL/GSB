"""Atomic stage snapshots, recovery validation and graceful shutdown."""

from __future__ import annotations

import os
import signal
from dataclasses import dataclass
from typing import Any

from .errors import E_CONFIG_MISMATCH, E_SNAPSHOT_VERSION
from .util import atomic_write_json, read_json, stable_hash

SNAPSHOT_VERSION = 1
STAGES = ("recall", "fusion", "evaluation")


class SnapshotMismatch(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ShutdownController:
    def __init__(self) -> None:
        self.requested = False
        self._install_handlers()

    def _install_handlers(self) -> None:
        if not hasattr(signal, "SIGTERM"):
            return
        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(signum, self._handle)
            except (ValueError, OSError):
                # Not running in the main thread: signals are optional.
                pass

    def _handle(self, signum: int, frame: Any) -> None:
        self.requested = True

    def should_stop(self) -> bool:
        return self.requested


@dataclass
class SnapshotStore:
    root: str
    config_hash: str
    scale_id: str

    def __post_init__(self) -> None:
        os.makedirs(self.root, exist_ok=True)

    def path(self, stage: str) -> str:
        return os.path.join(self.root, f"snapshot_{stage}_{self.scale_id}.json")

    def save(
        self,
        stage: str,
        cursor: int,
        total: int,
        artifact_keys: dict[str, str],
        digest: str,
    ) -> str:
        payload = {
            "version": SNAPSHOT_VERSION,
            "stage": stage,
            "config_hash": self.config_hash,
            "scale_id": self.scale_id,
            "cursor": cursor,
            "total": total,
            "artifact_keys": artifact_keys,
            "digest": digest,
        }
        path = self.path(stage)
        atomic_write_json(path, payload)
        return path

    def load(self, stage: str) -> dict[str, Any] | None:
        path = self.path(stage)
        if not os.path.exists(path):
            return None
        payload = read_json(path)
        if payload.get("version") != SNAPSHOT_VERSION:
            raise SnapshotMismatch(
                E_SNAPSHOT_VERSION,
                f"snapshot version {payload.get('version')} != {SNAPSHOT_VERSION}",
            )
        if payload.get("config_hash") != self.config_hash:
            raise SnapshotMismatch(
                E_CONFIG_MISMATCH,
                f"config hash mismatch for stage {stage}: "
                f"{payload.get('config_hash')} != {self.config_hash}",
            )
        if payload.get("scale_id") != self.scale_id:
            raise SnapshotMismatch(
                E_CONFIG_MISMATCH,
                f"scale mismatch for stage {stage}: "
                f"{payload.get('scale_id')} != {self.scale_id}",
            )
        return payload

    def clear(self) -> None:
        for stage in STAGES:
            path = self.path(stage)
            if os.path.exists(path):
                os.unlink(path)


def digest_payload(payload: Any) -> str:
    return stable_hash(payload)
