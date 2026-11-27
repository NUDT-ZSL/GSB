"""Immutable, versioned whole-field sound snapshots."""

from __future__ import annotations

import copy
import fcntl
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import numpy as np

from .geometry import (
    attenuation_amplitude,
    spherical_from_vector,
    world_to_listener,
)
from .io_utils import atomic_write_json, read_json, sha256_json
from .vbap import panning_gains, virtual_azimuth_from_gains


class SnapshotError(Exception):
    business_code = 5000
    code = "SNAPSHOT_ERROR"
    http_status = 500

    def __init__(self, message: str, **details: object):
        super().__init__(message)
        self.message = message
        self.details = details

    def as_dict(self) -> dict:
        payload = {
            "ok": False,
            "code": self.code,
            "business_code": self.business_code,
            "error": {
                "code": self.code,
                "business_code": self.business_code,
                "message": self.message,
            },
        }
        if self.details:
            payload["error"]["details"] = self.details
        return payload


class SnapshotNotFound(SnapshotError):
    business_code = 2002
    code = "SNAPSHOT_NOT_FOUND"
    http_status = 404


class SnapshotReplaced(SnapshotError):
    business_code = 2003
    code = "SNAPSHOT_REPLACED"
    http_status = 409


class SnapshotVersionRequired(SnapshotError):
    business_code = 2001
    code = "SNAPSHOT_VERSION_REQUIRED"
    http_status = 400


class SegmentNotFound(SnapshotError):
    business_code = 1001
    code = "SEGMENT_NOT_FOUND"
    http_status = 404


class InvalidSnapshotVersion(SnapshotError):
    business_code = 2004
    code = "INVALID_SNAPSHOT_VERSION"
    http_status = 400


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_immutable_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            import json

            json.dump(
                value,
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
    finally:
        if temporary.exists():
            temporary.unlink()


class SnapshotStore:
    """Filesystem store where version files never change after creation."""

    def __init__(
        self,
        root: str | Path,
        speaker_layout: dict | None = None,
    ):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.speaker_layout = (
            copy.deepcopy(speaker_layout) if speaker_layout is not None else None
        )

    def _segment_dir(self, segment_id: str) -> Path:
        if not segment_id or any(char in segment_id for char in "/\\."):
            raise SegmentNotFound(f"invalid segment id: {segment_id}")
        return self.root / segment_id

    def _manifest_path(self, segment_id: str) -> Path:
        return self._segment_dir(segment_id) / "manifest.json"

    def _lock_path(self, segment_id: str) -> Path:
        return self._segment_dir(segment_id) / ".snapshot.lock"

    def _empty_manifest(self, segment_id: str) -> dict:
        return {
            "schema_version": 1,
            "segment_id": segment_id,
            "active_version": None,
            "versions": [],
        }

    def _read_manifest(self, segment_id: str) -> dict:
        path = self._manifest_path(segment_id)
        if not path.exists():
            return self._empty_manifest(segment_id)
        return read_json(path)

    def build_payload(
        self,
        project: dict,
        segment: dict,
        analysis: dict,
        audio_metadata: dict,
    ) -> dict:
        """Create the complete field state written as one immutable file."""
        layout = self.speaker_layout or project["speaker_layout"]
        render = analysis["render_output"]
        payload = {
            "schema_version": 1,
            "snapshot_format": "whole_field_state_v1",
            "project_id": project["project_id"],
            "project_seed": project["seed"],
            "segment": {
                "segment_id": segment["segment_id"],
                "boundary_type": segment["boundary_type"],
                "description": segment["description"],
                "seed": segment["seed"],
                "duration_s": segment["duration_s"],
                "frame_rate_hz": segment["frame_rate_hz"],
                "sample_rate_hz": segment["sample_rate_hz"],
                "samples_per_frame": segment["samples_per_frame"],
                "frame_count": segment["frame_count"],
                "source_digest": segment["source_digest"],
            },
            "coordinate_system": project["coordinate_system"],
            "source_tables": {
                "source_categories": copy.deepcopy(project["source_categories"]),
                "intensity_mapping_curves": copy.deepcopy(
                    project["intensity_mapping_curves"]
                ),
            },
            "speaker_layout": copy.deepcopy(layout),
            "field": {
                "state_intervals": copy.deepcopy(analysis["state_intervals"]),
                "source_objects": copy.deepcopy(analysis["source_objects"]),
                "strength_formulas": copy.deepcopy(
                    analysis["strength_formulas"]
                ),
                "strength_frames": copy.deepcopy(analysis["strength_frames"]),
                "object_trajectories": copy.deepcopy(
                    analysis["object_trajectories"]
                ),
                "spatter_dense_intervals": copy.deepcopy(
                    analysis["spatter_dense_intervals"]
                ),
                "distance_jumps": copy.deepcopy(analysis["distance_jumps"]),
                "frames": copy.deepcopy(render["frames"]),
                "channel_rms_curves": copy.deepcopy(
                    render["channel_rms_curves"]
                ),
                "channel_gain_curves": copy.deepcopy(
                    render["channel_gain_curves"]
                ),
                "object_gain_curves": copy.deepcopy(
                    render["object_gain_curves"]
                ),
            },
            "audio": copy.deepcopy(audio_metadata),
            "render_path": render["path"],
        }
        payload["content_sha256"] = sha256_json(payload)
        return payload

    def save(self, segment_id: str, payload: dict) -> tuple[str, dict, str | None]:
        """Atomically persist a new active version and mark the prior one replaced."""
        content_sha = payload.get("content_sha256") or sha256_json(
            {key: value for key, value in payload.items() if key != "content_sha256"}
        )
        base = copy.deepcopy(payload)
        base.pop("content_sha256", None)
        content_sha = sha256_json(base)

        with _file_lock(self._lock_path(segment_id)):
            manifest = self._read_manifest(segment_id)
            versions = list(manifest["versions"])
            same = next(
                (
                    record
                    for record in versions
                    if record["content_sha256"] == content_sha
                    and record["status"] == "active"
                ),
                None,
            )
            if same is not None:
                return same["version"], same, None

            ordinals = []
            for record in versions:
                version = record["version"]
                if version.startswith("v") and version[1:].isdigit():
                    ordinals.append(int(version[1:]))
            next_ordinal = max(ordinals, default=0) + 1
            version = f"v{next_ordinal:04d}"

            replaced_version = None
            for record in versions:
                if record["status"] == "active":
                    record["status"] = "replaced"
                    record["replaced_by"] = version
                    replaced_version = record["version"]

            stored = copy.deepcopy(base)
            stored["snapshot_version"] = version
            version_path = self._segment_dir(segment_id) / "versions" / f"{version}.json"
            _write_immutable_json(version_path, stored)

            record = {
                "version": version,
                "status": "active",
                "content_sha256": content_sha,
                "path": f"versions/{version}.json",
                "segment_id": segment_id,
                "audio_sha256": base.get("audio", {}).get("sha256"),
            }
            versions.append(record)
            manifest = {
                "schema_version": 1,
                "segment_id": segment_id,
                "active_version": version,
                "versions": versions,
            }
            atomic_write_json(self._manifest_path(segment_id), manifest)

            return version, record, replaced_version

    def list_versions(self, segment_id: str) -> dict:
        return self._read_manifest(segment_id)

    def active(self, segment_id: str) -> dict:
        manifest = self._read_manifest(segment_id)
        active_version = manifest.get("active_version")
        if not active_version:
            raise SnapshotNotFound(
                "segment has no active snapshot", segment_id=segment_id
            )
        record = next(
            (
                item
                for item in manifest["versions"]
                if item["version"] == active_version
            ),
            None,
        )
        if record is None:
            raise SnapshotNotFound(
                "active snapshot record is missing",
                segment_id=segment_id,
                version=active_version,
            )
        return record

    def load(self, segment_id: str, version: str | None) -> dict:
        if version is None or str(version).strip() == "":
            raise SnapshotVersionRequired(
                "snapshot version is required",
                segment_id=segment_id,
            )
        version = str(version).strip()
        if not version.startswith("v") or not version[1:].isdigit():
            raise InvalidSnapshotVersion(
                "snapshot version must look like vNNNN",
                segment_id=segment_id,
                requested_version=version,
            )
        manifest = self._read_manifest(segment_id)
        record = next(
            (
                item
                for item in manifest["versions"]
                if item["version"] == version
            ),
            None,
        )
        if record is None:
            raise SnapshotNotFound(
                "requested snapshot version does not exist",
                segment_id=segment_id,
                requested_version=version,
                active_version=manifest.get("active_version"),
            )
        if record["status"] == "replaced":
            raise SnapshotReplaced(
                "requested snapshot version has been replaced",
                segment_id=segment_id,
                requested_version=version,
                replaced_by=record.get("replaced_by"),
                active_version=manifest.get("active_version"),
            )
        path = self._segment_dir(segment_id) / record["path"]
        if not path.exists():
            raise SnapshotNotFound(
                "snapshot file is missing",
                segment_id=segment_id,
                requested_version=version,
            )
        snapshot = read_json(path)
        stored_base = copy.deepcopy(snapshot)
        stored_base.pop("snapshot_version", None)
        if sha256_json(stored_base) != record["content_sha256"]:
            raise SnapshotError(
                "snapshot integrity check failed",
                segment_id=segment_id,
                requested_version=version,
            )
        snapshot["_manifest_record"] = record
        snapshot["_active_version"] = manifest["active_version"]
        return snapshot

    def replay_frames(self, snapshot: dict) -> list[dict]:
        """Reproduce every frame from the immutable snapshot alone."""
        layout = snapshot["speaker_layout"]
        categories = {
            category["source_type"]: category
            for category in snapshot["source_tables"]["source_categories"]
        }
        minimum_distance = float(layout["minimum_distance_m"])
        fields = snapshot["field"]["frames"]
        replay_frames = []
        for frame in fields:
            listener = np.asarray(frame["listener_position_m"], dtype=float)
            replay_sources = []
            total_channel_power = np.zeros(len(layout["channels"]), dtype=float)
            for source_state in frame["sources"]:
                world = np.asarray(source_state["world_position_m"], dtype=float)
                relative_world = world - listener
                local, azimuth, elevation, distance = world_to_listener(
                    relative_world,
                    float(frame["head_yaw_deg"]),
                    float(frame["head_pitch_deg"]),
                )
                del local
                category = categories[source_state["source_type"]]
                attenuation = attenuation_amplitude(
                    distance,
                    float(category["reference_distance_m"]),
                    minimum_distance,
                )
                gains = panning_gains(azimuth, layout)
                channel_amplitudes = (
                    np.sqrt(max(float(source_state["strength_power"]), 0.0))
                    * attenuation
                    * gains
                )
                channel_power = channel_amplitudes * channel_amplitudes
                total_channel_power += channel_power
                replay_sources.append(
                    {
                        "object_id": source_state["object_id"],
                        "source_type": source_state["source_type"],
                        "azimuth_deg": azimuth,
                        "elevation_deg": elevation,
                        "distance_m": distance,
                        "distance_attenuation_amplitude": attenuation,
                        "panning_gains": [float(value) for value in gains],
                        "channel_amplitudes": [
                            float(value) for value in channel_amplitudes
                        ],
                        "rendered_source_energy": float(np.sum(channel_power)),
                        "reconstructed_azimuth_deg": virtual_azimuth_from_gains(
                            gains, layout
                        ),
                    }
                )
            replay_frames.append(
                {
                    "frame_index": int(frame["frame_index"]),
                    "time_s": float(frame["time_s"]),
                    "sources": replay_sources,
                    "channel_rms": [
                        float(value)
                        for value in np.sqrt(total_channel_power)
                    ],
                    "total_source_energy": float(np.sum(total_channel_power)),
                }
            )
        return replay_frames
