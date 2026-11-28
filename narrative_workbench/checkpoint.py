from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from .storage import (
    atomic_write_bytes,
    deterministic_npz_bytes,
    json_bytes,
    read_json,
    sha256_bytes,
    sha256_file,
    write_json_atomic,
)


class CheckpointError(RuntimeError):
    pass


class FrameCheckpoint:
    SCHEMA = "committed-frame-checkpoint/1"

    def __init__(
        self,
        directory: str | Path,
        *,
        version: str,
        total_frames: int,
        scene_fingerprint: str,
        camera_fingerprint: str,
    ) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "checkpoint.json"
        self.version = version
        self.total_frames = int(total_frames)
        self.scene_fingerprint = str(scene_fingerprint)
        self.camera_fingerprint = str(camera_fingerprint)
        self.state = self._load_or_create()
        self.session_start_frame = int(self.state["next_frame"])
        if 0 < self.session_start_frame < self.total_frames:
            self._record_resume()

    def _new_state(self) -> dict[str, Any]:
        return {
            "schema_version": self.SCHEMA,
            "version": self.version,
            "scene_fingerprint": self.scene_fingerprint,
            "camera_fingerprint": self.camera_fingerprint,
            "total_frames": self.total_frames,
            "next_frame": 0,
            "status": "not_started",
            "committed_frames": [],
            "resume_count": 0,
            "resume_events": [],
            "commit_sequence": 0,
        }

    def _load_or_create(self) -> dict[str, Any]:
        if not self.path.exists():
            state = self._new_state()
            write_json_atomic(self.path, state)
            return state
        state = read_json(self.path)
        if state.get("schema_version") != self.SCHEMA:
            raise CheckpointError("checkpoint schema mismatch")
        if state.get("version") != self.version:
            raise CheckpointError(
                f"checkpoint version mismatch: {state.get('version')} != {self.version}"
            )
        if int(state.get("total_frames", -1)) != self.total_frames:
            raise CheckpointError(
                "checkpoint total_frames mismatch; use --reset-version to start over"
            )
        if state.get("scene_fingerprint") != self.scene_fingerprint:
            raise CheckpointError(
                "checkpoint scene fingerprint mismatch; use --force/--reset-version"
            )
        if state.get("camera_fingerprint") != self.camera_fingerprint:
            raise CheckpointError(
                "checkpoint camera fingerprint mismatch; use --force/--reset-version"
            )
        next_frame = int(state.get("next_frame", 0))
        committed = state.get("committed_frames", [])
        if next_frame < 0 or next_frame > self.total_frames:
            raise CheckpointError("checkpoint next_frame is outside the camera track")
        if len(committed) != next_frame:
            raise CheckpointError("checkpoint committed frame count is not contiguous")
        for expected, record in enumerate(committed):
            if int(record.get("index", -1)) != expected:
                raise CheckpointError(
                    f"checkpoint frame index is not contiguous at {expected}"
                )
            for key in ("json", "data", "png"):
                if not (self.directory / str(record[key])).is_file():
                    raise CheckpointError(
                        f"committed frame {expected} is missing artifact {record[key]}"
                    )
            for hash_key, file_key in (
                ("json_sha256", "json"),
                ("data_sha256", "data"),
                ("png_sha256", "png"),
            ):
                actual = sha256_file(self.directory / str(record[file_key]))
                if actual != record.get(hash_key):
                    raise CheckpointError(
                        f"committed frame {expected} artifact {record[file_key]} "
                        "does not match its persisted hash"
                    )
        return state

    @property
    def next_frame(self) -> int:
        return int(self.state["next_frame"])

    @property
    def complete(self) -> bool:
        return self.next_frame >= self.total_frames

    def _record_resume(self) -> None:
        self.state["resume_count"] = int(self.state.get("resume_count", 0)) + 1
        events = list(self.state.get("resume_events", []))
        events.append(
            {
                "sequence": int(self.state["resume_count"]),
                "start_frame": int(self.state["next_frame"]),
                "committed_frames_skipped": int(self.state["next_frame"]),
                "policy": "continue_from_committed_next_frame",
            }
        )
        self.state["resume_events"] = events
        write_json_atomic(self.path, self.state)

    def commit(
        self,
        frame_index: int,
        *,
        frame_payload: dict[str, Any],
        arrays: dict[str, np.ndarray],
        png_bytes: bytes,
        rgba_sha256: str,
        order_digest: str,
    ) -> dict[str, Any]:
        if frame_index != self.next_frame:
            raise CheckpointError(
                f"attempted to commit frame {frame_index}, expected {self.next_frame}"
            )
        if frame_index >= self.total_frames:
            raise CheckpointError("all frames are already committed")
        stem = f"frame_{frame_index:04d}"
        json_name = f"{stem}.json"
        data_name = f"{stem}.npz"
        png_name = f"{stem}.png"
        stored_payload = dict(frame_payload)
        stored_payload["checkpoint"] = {
            "schema_version": self.SCHEMA,
            "version": self.version,
            "frame_index": int(frame_index),
            "commit_sequence": int(self.state["commit_sequence"]) + 1,
            "rgba_sha256": rgba_sha256,
            "order_digest": order_digest,
            "status": "committed",
        }
        json_payload = json_bytes(stored_payload)
        array_payload = deterministic_npz_bytes(arrays)

        atomic_write_bytes(self.directory / json_name, json_payload)
        atomic_write_bytes(self.directory / data_name, array_payload)
        atomic_write_bytes(self.directory / png_name, png_bytes)

        record = {
            "index": int(frame_index),
            "json": json_name,
            "data": data_name,
            "png": png_name,
            "json_sha256": sha256_bytes(json_payload),
            "data_sha256": sha256_bytes(array_payload),
            "png_sha256": sha256_bytes(png_bytes),
            "rgba_sha256": rgba_sha256,
            "order_digest": order_digest,
        }
        self.state["committed_frames"].append(record)
        self.state["next_frame"] = int(frame_index) + 1
        self.state["commit_sequence"] = int(self.state["commit_sequence"]) + 1
        self.state["status"] = (
            "complete" if self.next_frame >= self.total_frames else "in_progress"
        )
        write_json_atomic(self.path, self.state)
        return record

    def reset(self) -> None:
        for path in self.directory.iterdir():
            if path.is_file() and (
                path.name == "checkpoint.json"
                or path.name.startswith("frame_")
                or path.name.startswith(".frame_")
            ):
                path.unlink()
        self.state = self._new_state()
        self.session_start_frame = 0
        write_json_atomic(self.path, self.state)

    def summary(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "directory": str(self.directory),
            "next_frame": self.next_frame,
            "total_frames": self.total_frames,
            "status": self.state["status"],
            "committed_frame_count": len(self.state["committed_frames"]),
            "resume_count": int(self.state.get("resume_count", 0)),
            "session_start_frame": self.session_start_frame,
            "resume_events": list(self.state.get("resume_events", [])),
            "scene_fingerprint": self.scene_fingerprint,
            "camera_fingerprint": self.camera_fingerprint,
        }
