from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from .util import (
    atomic_write_bytes,
    atomic_write_json,
    decode_npz,
    encode_npz,
    now_iso,
    payload_fingerprint,
    read_json,
    sha256_bytes,
    sha256_json,
)


ARTIFACT_SCHEMA_VERSION = 1
MANIFEST_SCHEMA_VERSION = 1


@dataclass
class Artifact:
    stage: str
    stage_kind: str
    step: int | None
    frame: int | None
    version: int
    cache_key: str
    content_fingerprint: str
    dependencies: list[dict[str, str]]
    metadata: dict[str, Any]
    arrays: dict[str, np.ndarray]
    path: Path
    file_sha256: str
    reused: bool = False


class ArtifactStore:
    """Versioned stage-artifact store with atomically replaced manifest metadata."""

    def __init__(self, output_dir: str | Path) -> None:
        self.output_dir = Path(output_dir).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.stage_dir = self.output_dir / "stages"
        self.stage_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.output_dir / "manifest.json"
        self.report_path = self.output_dir / "diagnostic_report.json"
        self._lock = threading.RLock()
        self._manifest: dict[str, Any] = {}
        self._loaded: dict[str, Artifact] = {}
        self._invalid_reasons: dict[str, str] = {}
        self._reused: dict[str, dict[str, Any]] = {}
        self._recomputed: dict[str, dict[str, Any]] = {}
        self._stage_order: dict[str, int] = {}
        self._release_pid = os.getpid()
        self._run_started = time.monotonic()
        self.previous_manifest: dict[str, Any] = {}
        self.recovery: dict[str, Any] = {}
        self.snapshot_version = 1

    # ------------------------------------------------------------------ lifecycle
    def begin_run(self, *, config_fingerprint: str, stage_order: list[str], total_tasks: int) -> dict[str, Any]:
        with self._lock:
            self._stage_order = {stage: index for index, stage in enumerate(stage_order)}
            previous, manifest_error = self._read_manifest()
            self.previous_manifest = previous
            previous_version = int(previous.get("snapshot_version", 0) or 0)
            self.snapshot_version = previous_version + 1
            previous_status = previous.get("status")
            previous_pid = previous.get("pid")
            recovered_crash = previous_status == "running" and previous_pid not in (None, self._release_pid)
            previous_progress_names = []
            if isinstance(previous.get("progress"), dict):
                previous_progress_names = [
                    stage
                    for stage in previous["progress"].get("completed_stage_names", [])
                    if stage in stage_order
                ]
            last_stage = (
                previous_progress_names[-1]
                if recovered_crash and previous_progress_names
                else self._last_current_stage(previous.get("artifacts", {}), stage_order)
            )
            points = self._validated_artifact_points(previous.get("artifacts", {}), stage_order)
            if recovered_crash and previous_progress_names:
                points = [stage for stage in points if stage in set(previous_progress_names)]
            self.recovery = {
                "kind": "crash_recovery" if recovered_crash else ("incremental" if previous_version > 0 else "fresh"),
                "detected": bool(previous_version > 0),
                "from_snapshot_version": previous_version if previous_version > 0 else None,
                "from_status": previous_status,
                "from_pid": previous_pid,
                "manifest_error": manifest_error,
                "last_completed_stage": last_stage,
                "complete_artifact_stages": points,
                "previous_progress": previous.get("progress"),
            }
            self._manifest = {
                "schema_version": MANIFEST_SCHEMA_VERSION,
                "snapshot_version": self.snapshot_version,
                "run_id": f"v{self.snapshot_version:06d}-{config_fingerprint[:12]}-{self._release_pid}",
                "config_fingerprint": config_fingerprint,
                "status": "running",
                "pid": self._release_pid,
                "started_at": now_iso(),
                "updated_at": now_iso(),
                "stage_order": list(stage_order),
                "artifacts": dict(previous.get("artifacts", {})) if isinstance(previous.get("artifacts"), dict) else {},
                "progress": {
                    "completed_stages": 0,
                    "total_stages": total_tasks,
                    "completed_stage_names": [],
                    "current_stage": None,
                    "reused_count": 0,
                    "recomputed_count": 0,
                },
                "recovery": self.recovery,
            }
            self._write_manifest()
            return self.recovery

    def finalize(self, *, status: str, business_codes: list[str], report_path: str | None = None, extra: Mapping[str, Any] | None = None) -> None:
        with self._lock:
            self._manifest["status"] = status
            self._manifest["business_codes"] = list(business_codes)
            self._manifest["finished_at"] = now_iso()
            self._manifest["updated_at"] = now_iso()
            if report_path is not None:
                self._manifest["report_path"] = report_path
            if extra:
                self._manifest["final"] = dict(extra)
            current_completed = set(self._reused) | set(self._recomputed)
            self._manifest["artifacts"] = {
                stage: record
                for stage, record in self._ordered_artifacts().items()
                if stage in current_completed
            }
            self._manifest["progress"]["completed_stage_names"] = self._completed_stage_names()
            self._manifest["progress"]["current_stage"] = None
            self._manifest["progress"]["reused_count"] = len(self._reused)
            self._manifest["progress"]["recomputed_count"] = len(self._recomputed)
            self._write_manifest()

    # ------------------------------------------------------------------ reuse
    def lookup(
        self,
        *,
        stage: str,
        stage_kind: str,
        step: int | None,
        frame: int | None,
        cache_key: str,
        dependencies: list[dict[str, str]],
    ) -> Artifact | None:
        with self._lock:
            record = self._manifest.get("artifacts", {}).get(stage)
            if not isinstance(record, dict):
                self._invalid_reasons[stage] = "missing"
                return None
            if record.get("cache_key") != cache_key:
                self._invalid_reasons[stage] = "stage_inputs_or_dependencies_changed"
                return None
            if self._normalized_dependencies(record.get("dependencies")) != self._normalized_dependencies(dependencies):
                self._invalid_reasons[stage] = "dependency_fingerprint_changed"
                return None
            path = self._record_path(record)
            if path is None:
                self._invalid_reasons[stage] = "artifact_path_missing"
                return None
            try:
                data = path.read_bytes()
            except OSError:
                self._invalid_reasons[stage] = "artifact_file_missing"
                return None
            file_hash = sha256_bytes(data)
            if file_hash != record.get("file_sha256"):
                self._invalid_reasons[stage] = "artifact_file_fingerprint_mismatch"
                return None
            try:
                metadata, arrays = decode_npz(data)
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                self._invalid_reasons[stage] = "artifact_container_invalid"
                return None
            expected = {
                "stage": stage,
                "stage_kind": stage_kind,
                "step": step,
                "frame": frame,
                "snapshot_version": record.get("snapshot_version"),
                "cache_key": cache_key,
            }
            for key, expected_value in expected.items():
                if metadata.get(key) != expected_value:
                    self._invalid_reasons[stage] = f"artifact_metadata_{key}_mismatch"
                    return None
            if metadata.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
                self._invalid_reasons[stage] = "artifact_schema_mismatch"
                return None
            if metadata.get("dependencies") != dependencies:
                self._invalid_reasons[stage] = "artifact_dependency_metadata_mismatch"
                return None
            stored_fingerprint = metadata.get("content_fingerprint")
            metadata_base = dict(metadata)
            metadata_base.pop("content_fingerprint", None)
            try:
                recomputed = payload_fingerprint(metadata_base, arrays)
            except (TypeError, ValueError):
                self._invalid_reasons[stage] = "artifact_payload_unfingerprintable"
                return None
            if stored_fingerprint != recomputed:
                self._invalid_reasons[stage] = "artifact_content_fingerprint_mismatch"
                return None
            if stored_fingerprint != record.get("content_fingerprint"):
                self._invalid_reasons[stage] = "manifest_content_fingerprint_mismatch"
                return None
            artifact = Artifact(
                stage=stage,
                stage_kind=stage_kind,
                step=step,
                frame=frame,
                version=int(metadata["snapshot_version"]),
                cache_key=cache_key,
                content_fingerprint=stored_fingerprint,
                dependencies=dependencies,
                metadata=metadata,
                arrays=arrays,
                path=path,
                file_sha256=file_hash,
                reused=True,
            )
            self._loaded[stage] = artifact
            self._reused[stage] = self._event(artifact, reused=True, timing_ms=0.0)
            self._invalid_reasons.pop(stage, None)
            self._update_progress(stage, reused=True)
            return artifact

    def persist(
        self,
        *,
        stage: str,
        stage_kind: str,
        step: int | None,
        frame: int | None,
        cache_key: str,
        dependencies: list[dict[str, str]],
        local_inputs: Mapping[str, Any],
        arrays: Mapping[str, np.ndarray],
        timing_ms: float,
        extra_metadata: Mapping[str, Any] | None = None,
    ) -> Artifact:
        with self._lock:
            metadata_base: dict[str, Any] = {
                "schema_version": ARTIFACT_SCHEMA_VERSION,
                "stage": stage,
                "stage_kind": stage_kind,
                "step": step,
                "frame": frame,
                "snapshot_version": self.snapshot_version,
                "cache_key": cache_key,
                "dependencies": dependencies,
                "local_inputs": dict(local_inputs),
            }
            if extra_metadata:
                metadata_base["extra"] = dict(extra_metadata)
            content_fingerprint = payload_fingerprint(metadata_base, arrays)
            metadata = dict(metadata_base)
            metadata["content_fingerprint"] = content_fingerprint
            safe_stage = stage.replace("/", "_")
            path = self.stage_dir / safe_stage / f"{cache_key[:20]}.v{self.snapshot_version:06d}.npz"
            data = encode_npz(metadata, arrays)
            atomic_write_bytes(path, data)
            record = {
                "stage": stage,
                "stage_kind": stage_kind,
                "step": step,
                "frame": frame,
                "path": str(path.relative_to(self.output_dir)),
                "snapshot_version": self.snapshot_version,
                "cache_key": cache_key,
                "content_fingerprint": content_fingerprint,
                "file_sha256": sha256_bytes(data),
                "dependencies": dependencies,
            }
            self._manifest.setdefault("artifacts", {})[stage] = record
            artifact = Artifact(
                stage=stage,
                stage_kind=stage_kind,
                step=step,
                frame=frame,
                version=self.snapshot_version,
                cache_key=cache_key,
                content_fingerprint=content_fingerprint,
                dependencies=dependencies,
                metadata=metadata,
                arrays={key: np.asarray(value) for key, value in arrays.items()},
                path=path,
                file_sha256=record["file_sha256"],
                reused=False,
            )
            self._loaded[stage] = artifact
            self._recomputed[stage] = self._event(artifact, reused=False, timing_ms=timing_ms)
            self._update_progress(stage, reused=False)
            return artifact

    def record_failure_artifact(
        self,
        *,
        stage: str,
        stage_kind: str,
        step: int | None,
        frame: int | None,
        cache_key: str,
        dependencies: list[dict[str, str]],
        local_inputs: Mapping[str, Any],
        arrays: Mapping[str, np.ndarray],
        timing_ms: float,
        extra_metadata: Mapping[str, Any] | None = None,
    ) -> Artifact:
        """Persist a deterministic partial artifact and still advance progress."""

        return self.persist(
            stage=stage,
            stage_kind=stage_kind,
            step=step,
            frame=frame,
            cache_key=cache_key,
            dependencies=dependencies,
            local_inputs=local_inputs,
            arrays=arrays,
            timing_ms=timing_ms,
            extra_metadata=extra_metadata,
        )

    # ------------------------------------------------------------------ progress/report data
    def mark_skipped(self, stage: str, reason: str) -> None:
        with self._lock:
            self._invalid_reasons.setdefault(stage, reason)

    def artifact(self, stage: str) -> Artifact | None:
        with self._lock:
            if stage in self._loaded:
                return self._loaded[stage]
            record = self._manifest.get("artifacts", {}).get(stage)
            if not isinstance(record, dict):
                return None
            path = self._record_path(record)
            if path is None:
                return None
            try:
                data = path.read_bytes()
                if sha256_bytes(data) != record.get("file_sha256"):
                    return None
                metadata, arrays = decode_npz(data)
            except (OSError, ValueError):
                return None
            artifact = Artifact(
                stage=stage,
                stage_kind=metadata.get("stage_kind", ""),
                step=metadata.get("step"),
                frame=metadata.get("frame"),
                version=int(metadata.get("snapshot_version", 0)),
                cache_key=metadata.get("cache_key", ""),
                content_fingerprint=metadata.get("content_fingerprint", ""),
                dependencies=metadata.get("dependencies", []),
                metadata=metadata,
                arrays=arrays,
                path=path,
                file_sha256=record.get("file_sha256", ""),
                reused=True,
            )
            self._loaded[stage] = artifact
            return artifact

    @property
    def reused_stages(self) -> list[str]:
        return sorted(self._reused, key=self._stage_sort)

    @property
    def recomputed_stages(self) -> list[str]:
        return sorted(self._recomputed, key=self._stage_sort)

    @property
    def invalidated_stages(self) -> list[str]:
        current = set(self._stage_order)
        return sorted(
            (
                stage
                for stage, reason in self._invalid_reasons.items()
                if stage in current and reason != "missing"
            ),
            key=self._stage_sort,
        )

    def invalidation_reasons(self) -> dict[str, str]:
        return {stage: self._invalid_reasons[stage] for stage in self.invalidated_stages}

    def stage_timings(self) -> dict[str, float]:
        values: dict[str, float] = {}
        for stage, event in {**self._reused, **self._recomputed}.items():
            values[stage] = round(float(event.get("timing_ms", 0.0)), 6)
        return {stage: values[stage] for stage in sorted(values, key=self._stage_sort)}

    def events(self) -> list[dict[str, Any]]:
        combined = {**self._reused, **self._recomputed}
        return [combined[stage] for stage in sorted(combined, key=self._stage_sort)]

    def artifact_records(self) -> list[dict[str, Any]]:
        records = self._ordered_artifacts()
        return [dict(records[stage], reused=(stage in self._reused)) for stage in sorted(records, key=self._stage_sort)]

    def latest_preview_record(self, *, stage_order: list[str] | None = None) -> dict[str, Any] | None:
        preview_dir = self.output_dir / "previews"
        if not preview_dir.exists():
            return None
        candidates: list[tuple[int, Path]] = []
        for metadata_path in preview_dir.glob("preview_v*.json"):
            try:
                version = int(metadata_path.stem.split("_v", 1)[1])
            except (IndexError, ValueError):
                continue
            candidates.append((version, metadata_path))
        for _, metadata_path in sorted(candidates, reverse=True):
            try:
                data = read_json(metadata_path)
            except (OSError, json.JSONDecodeError):
                continue
            image_path = metadata_path.with_suffix(".png")
            if image_path.exists():
                data["path"] = str(metadata_path.with_suffix(".png"))
                data["metadata_path"] = str(metadata_path)
                return data
        return None

    def manifest_snapshot(self) -> dict[str, Any]:
        with self._lock:
            payload = json.loads(json.dumps(self._manifest))
            payload["artifacts"] = self._ordered_artifacts()
            payload["progress"]["completed_stage_names"] = self._completed_stage_names()
            return payload

    def progress_summary(self) -> dict[str, Any]:
        with self._lock:
            progress = dict(self._manifest.get("progress", {}))
            progress["snapshot_version"] = self.snapshot_version
            progress["status"] = self._manifest.get("status")
            progress["artifact_count"] = len(self._manifest.get("artifacts", {}))
            progress["current_stage"] = progress.get("current_stage")
            return progress

    def update_current_stage(self, stage: str | None) -> None:
        with self._lock:
            self._manifest.setdefault("progress", {})["current_stage"] = stage
            self._manifest["updated_at"] = now_iso()
            self._write_manifest()

    # ------------------------------------------------------------------ internals
    def _update_progress(self, stage: str, *, reused: bool) -> None:
        progress = self._manifest.setdefault("progress", {})
        names = set(progress.get("completed_stage_names", []))
        names.add(stage)
        progress["completed_stage_names"] = self._completed_stage_names(names)
        progress["completed_stages"] = len(names)
        progress["current_stage"] = stage
        progress["reused_count"] = len(self._reused)
        progress["recomputed_count"] = len(self._recomputed)
        self._manifest["updated_at"] = now_iso()
        self._manifest["artifacts"] = self._ordered_artifacts()
        self._write_manifest()

    def _completed_stage_names(self, names: set[str] | None = None) -> list[str]:
        if names is None:
            names = set(self._manifest.get("progress", {}).get("completed_stage_names", []))
            names.update(self._reused)
            names.update(self._recomputed)
        return sorted((name for name in names if name in self._stage_order), key=self._stage_sort)

    def _ordered_artifacts(self) -> dict[str, dict[str, Any]]:
        artifacts = self._manifest.get("artifacts", {})
        if not isinstance(artifacts, dict):
            return {}
        valid = {stage: value for stage, value in artifacts.items() if isinstance(value, dict)}
        return {stage: valid[stage] for stage in sorted(valid, key=self._stage_sort)}

    def _stage_sort(self, stage: str) -> tuple[int, str]:
        return (self._stage_order.get(stage, 10**9), stage)

    def _event(self, artifact: Artifact, *, reused: bool, timing_ms: float) -> dict[str, Any]:
        return {
            "stage": artifact.stage,
            "stage_kind": artifact.stage_kind,
            "step": artifact.step,
            "frame": artifact.frame,
            "snapshot_version": artifact.version,
            "cache_key": artifact.cache_key,
            "content_fingerprint": artifact.content_fingerprint,
            "dependencies": artifact.dependencies,
            "reused": reused,
            "timing_ms": round(float(timing_ms), 6),
        }

    def _read_manifest(self) -> tuple[dict[str, Any], str | None]:
        if not self.manifest_path.exists():
            return {}, None
        try:
            value = read_json(self.manifest_path)
        except (OSError, json.JSONDecodeError) as exc:
            return {}, f"manifest_unreadable:{type(exc).__name__}"
        if not isinstance(value, dict):
            return {}, "manifest_not_object"
        if value.get("schema_version") != MANIFEST_SCHEMA_VERSION:
            return value, "manifest_schema_mismatch"
        return value, None

    def _write_manifest(self) -> None:
        self._manifest["updated_at"] = now_iso()
        atomic_write_json(self.manifest_path, self._manifest)

    def _record_path(self, record: Mapping[str, Any]) -> Path | None:
        value = record.get("path")
        if not isinstance(value, str):
            return None
        path = (self.output_dir / value).resolve()
        try:
            path.relative_to(self.output_dir)
        except ValueError:
            return None
        return path

    def _normalized_dependencies(self, dependencies: Any) -> list[dict[str, str]]:
        if not isinstance(dependencies, list):
            return []
        return sorted(
            [{"stage": str(item.get("stage")), "fingerprint": str(item.get("fingerprint"))} for item in dependencies if isinstance(item, dict)],
            key=lambda item: (item["stage"], item["fingerprint"]),
        )

    def _last_current_stage(self, artifacts: Any, stage_order: list[str]) -> str | None:
        if not isinstance(artifacts, dict):
            return None
        candidates = [stage for stage in stage_order if stage in artifacts]
        return candidates[-1] if candidates else None

    def _validated_artifact_points(self, artifacts: Any, stage_order: list[str]) -> list[str]:
        if not isinstance(artifacts, dict):
            return []
        result: list[str] = []
        for stage in stage_order:
            record = artifacts.get(stage)
            if not isinstance(record, dict):
                continue
            path = self._record_path(record)
            if path is None or not path.exists():
                continue
            try:
                if sha256_bytes(path.read_bytes()) == record.get("file_sha256"):
                    result.append(stage)
            except OSError:
                continue
        return result


def stage_cache_key(stage: str, local_inputs: Mapping[str, Any], dependencies: list[dict[str, str]]) -> str:
    return sha256_json(
        {
            "pipeline_semantics": 1,
            "stage": stage,
            "local_inputs": local_inputs,
            "dependencies": sorted(dependencies, key=lambda item: (item["stage"], item["fingerprint"])),
        }
    )


def dependency(stage: str, artifact_or_fingerprint: Artifact | str) -> dict[str, str]:
    fingerprint = artifact_or_fingerprint.content_fingerprint if isinstance(artifact_or_fingerprint, Artifact) else artifact_or_fingerprint
    return {"stage": stage, "fingerprint": fingerprint}


def load_artifact(path: str | Path) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    data = Path(path).read_bytes()
    return decode_npz(data)
