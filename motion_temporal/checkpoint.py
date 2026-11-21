"""Atomic stage checkpoints with monotonic versions and content checksums."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from .canonical import atomic_write_bytes, canonical_bytes, checksum, sha256_bytes
from .codes import E_CHECKPOINT_CORRUPT, STAGES
from .errors import BusinessError

_CHECKPOINT_PATTERN = re.compile(r"checkpoint_v(?P<version>\d{6})_(?P<stage>[a-z]+)\.json$")


class CheckpointStore:
    def __init__(self, root: str | Path, run_id: str):
        self.root = Path(root) / run_id
        self.run_id = run_id
        self.root.mkdir(parents=True, exist_ok=True)

    def _files(self) -> list[tuple[int, str, Path]]:
        values: list[tuple[int, str, Path]] = []
        for path in self.root.glob("checkpoint_v*.json"):
            match = _CHECKPOINT_PATTERN.match(path.name)
            if match:
                values.append((int(match.group("version")), match.group("stage"), path))
        values.sort(key=lambda item: (item[0], item[1]))
        return values

    def max_version(self) -> int:
        return max((version for version, _, _ in self._files()), default=0)

    def _read(self, path: Path, *, expected_version: int | None = None, expected_stage: str | None = None) -> dict[str, Any]:
        version = expected_version
        stage = expected_stage
        try:
            raw = path.read_bytes()
            envelope = json.loads(raw.decode("utf-8"))
            version = int(envelope["checkpoint_version"])
            stage = str(envelope["stage"])
            if envelope.get("run_id") != self.run_id:
                raise ValueError("run_id mismatch")
            if expected_version is not None and version != expected_version:
                raise ValueError("checkpoint version differs from filename")
            if expected_stage is not None and stage != expected_stage:
                raise ValueError("checkpoint stage differs from filename")
            payload = envelope["payload"]
            expected_content = sha256_bytes(canonical_bytes(payload))
            expected_envelope = checksum(
                {
                    "run_id": envelope["run_id"],
                    "stage": stage,
                    "checkpoint_version": version,
                    "frame_indices": envelope["frame_indices"],
                    "payload_checksum": envelope["payload_checksum"],
                    "complete": envelope["complete"],
                }
            )
            if envelope.get("payload_checksum") != expected_content:
                raise ValueError("payload checksum mismatch")
            if envelope.get("checkpoint_checksum") != expected_envelope:
                raise ValueError("checkpoint checksum mismatch")
            if envelope.get("complete") is not True:
                raise ValueError("checkpoint is not marked complete")
        except Exception as exc:
            raise BusinessError(
                E_CHECKPOINT_CORRUPT,
                f"checkpoint corrupt: {exc}",
                frame_index=None,
                stage=stage,
                details={
                    "path": str(path),
                    "checkpoint_version": version,
                    "stage": stage,
                },
            ) from exc
        return envelope

    def load_prefix(self) -> dict[str, dict[str, Any]]:
        """Load the contiguous committed stage prefix, validating checksums."""

        latest: dict[str, tuple[int, Path]] = {}
        for version, stage, path in self._files():
            if stage in latest and latest[stage][0] > version:
                continue
            latest[stage] = (version, path)
        loaded: dict[str, dict[str, Any]] = {}
        for stage in STAGES:
            if stage not in latest:
                break
            version, path = latest[stage]
            loaded[stage] = self._read(path, expected_version=version, expected_stage=stage)
        return loaded

    def commit(
        self,
        stage: str,
        payload: dict[str, Any],
        frame_indices: Iterable[int],
    ) -> dict[str, Any]:
        if stage not in STAGES:
            raise ValueError(f"unknown stage {stage}")
        for version, existing_stage, _ in self._files():
            if existing_stage == stage:
                existing = self._read(self._path_for(version, stage), expected_version=version, expected_stage=stage)
                if existing["payload_checksum"] != sha256_bytes(canonical_bytes(payload)):
                    raise BusinessError(
                        E_CHECKPOINT_CORRUPT,
                        f"existing {stage} checkpoint differs from recomputed payload",
                        stage=stage,
                        details={"checkpoint_version": version, "stage": stage},
                    )
                return existing
        version = self.max_version() + 1
        payload_checksum = sha256_bytes(canonical_bytes(payload))
        envelope = {
            "run_id": self.run_id,
            "stage": stage,
            "stage_order": STAGES.index(stage) + 1,
            "checkpoint_version": version,
            "complete": True,
            "frame_indices": sorted(int(index) for index in frame_indices),
            "payload_checksum": payload_checksum,
            "payload": payload,
        }
        envelope["checkpoint_checksum"] = checksum(
            {
                "run_id": envelope["run_id"],
                "stage": envelope["stage"],
                "checkpoint_version": envelope["checkpoint_version"],
                "frame_indices": envelope["frame_indices"],
                "payload_checksum": envelope["payload_checksum"],
                "complete": envelope["complete"],
            }
        )
        path = self._path_for(version, stage)
        atomic_write_bytes(path, canonical_bytes(envelope) + b"\n")
        return envelope

    def _path_for(self, version: int, stage: str) -> Path:
        return self.root / f"checkpoint_v{version:06d}_{stage}.json"

    def summary(self) -> dict[str, Any]:
        files: list[dict[str, Any]] = []
        for version, stage, path in self._files():
            envelope = self._read(path, expected_version=version, expected_stage=stage)
            files.append(
                {
                    "stage": stage,
                    "checkpoint_version": version,
                    "checkpoint_checksum": envelope["checkpoint_checksum"],
                    "payload_checksum": envelope["payload_checksum"],
                    "frame_indices": envelope["frame_indices"],
                }
            )
        return {"run_id": self.run_id, "directory": self.root.name, "checkpoints": files}

