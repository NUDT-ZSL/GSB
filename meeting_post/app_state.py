"""Interactive application state and JSON projection for the local dashboard."""

from __future__ import annotations

import json
import threading
from functools import wraps
from pathlib import Path
from typing import Any

from .config import (
    MeetingConfig,
    checksum,
    default_config,
    migrate_config,
    parse_config,
    render_config,
)
from .demo import build_demo, migration_report, stress_config
from .errors import MeetingError
from .pipeline import PipelineResult, assert_result_fresh, process_config
from .snapshots import SnapshotStore


def _synchronized(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class DashboardState:
    def __init__(
        self,
        *,
        artifact_dir: Path | str = Path("data/demo"),
        config_dir: Path | str = Path("configs"),
        runtime_dir: Path | str = Path("data/runtime"),
        seed: int = 20261123,
        regenerate: bool = True,
    ):
        self._lock = threading.RLock()
        self.artifact_dir = Path(artifact_dir)
        self.config_dir = Path(config_dir)
        self.runtime_dir = Path(runtime_dir)
        self.seed = seed
        if regenerate:
            build_demo(
                artifact_dir=self.artifact_dir,
                config_dir=self.config_dir,
                runtime_dir=self.runtime_dir,
                seed=self.seed,
                reset_runtime=True,
            )
        self.snapshots = SnapshotStore(self.runtime_dir / "snapshots")
        self.config_texts = {
            "v1": (self.config_dir / "meeting.v1.txt").read_text(encoding="utf-8"),
            "v2": (self.config_dir / "meeting.v2.txt").read_text(encoding="utf-8"),
            "v3": (self.config_dir / "meeting.v3.json").read_text(encoding="utf-8"),
            "v3-stress": (self.config_dir / "meeting.v3-stress.json").read_text(encoding="utf-8"),
        }
        self.sample_key = "v3"
        self.current_version = "v3"
        self.config = parse_config(self.config_texts["v3"])
        self.current_text = self.config_texts["v3"]
        self.snapshots.update_from_config(self.config)
        self.result: PipelineResult = process_config(self.config, self.snapshots, seed=self.seed)
        self.errors: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        canonical = default_config("v3")
        self.migration = migration_report(canonical, stress_config(canonical))
        self._event(
            "BOOT",
            f"initialized {self.current_version}; produced result {self.result.result_digest[:16]}",
        )

    def _record_error(self, exc: MeetingError, *, action: str) -> None:
        entry = {
            "sequence": len(self.errors) + 1,
            "action": action,
            **exc.to_dict(),
        }
        self.errors.append(entry)
        self._event("ERROR", f"{action}: {exc.code}")

    def _event(self, kind: str, message: str, **details: Any) -> None:
        self.events.append(
            {
                "sequence": len(self.events) + 1,
                "kind": kind,
                "message": message,
                "details": details,
            }
        )

    def _load_config(self, version: str) -> MeetingConfig:
        if version not in self.config_texts:
            raise MeetingError(
                "E_VERSION_UNSUPPORTED",
                f"no local sample for configuration {version}",
                {"version": version},
            )
        return parse_config(self.config_texts[version])

    @_synchronized
    def switch_config(self, version: str) -> dict[str, Any]:
        try:
            config = self._load_config(version)
            previous_version = self.result.config.version
            previous_digest = self.result.result_digest
            self.result = None  # type: ignore[assignment]
            self.snapshots.update_from_config(config)
            self.config = config
            self.sample_key = version
            self.current_text = self.config_texts[version]
            self.current_version = config.version
            self.result = process_config(config, self.snapshots, seed=self.seed)
            self._event(
                "CONFIG_SWITCH",
                f"{previous_version} -> {config.version} ({version}); previous result invalidated and recomputed",
                previous_result_digest=previous_digest,
                current_result_digest=self.result.result_digest,
            )
            return {"ok": True}
        except MeetingError as exc:
            self._record_error(exc, action=f"switch_config:{version}")
            return {"ok": False, "error": exc.to_dict()}

    @_synchronized
    def reprocess(self) -> dict[str, Any]:
        try:
            previous = self.result.result_digest if self.result else None
            self.result = None  # type: ignore[assignment]
            self.result = process_config(self.config, self.snapshots, seed=self.seed)
            self._event(
                "REPROCESS",
                "recomputed all derived results from the authoritative snapshots",
                previous_result_digest=previous,
                current_result_digest=self.result.result_digest,
            )
            return {"ok": True}
        except MeetingError as exc:
            self._record_error(exc, action="reprocess")
            return {"ok": False, "error": exc.to_dict()}

    @_synchronized
    def migrate_to(self, target_version: str, lossy: bool) -> dict[str, Any]:
        try:
            migrated, audit = migrate_config(self.config, target_version, lossy=lossy)
            source_version = self.config.version
            previous_version = self.result.config.version if self.result else None
            previous = self.result.result_digest if self.result else None
            self.result = None  # type: ignore[assignment]
            self.snapshots.update_from_config(migrated)
            self.config = migrated
            self.sample_key = f"migrated:{source_version}->{target_version}"
            self.current_text = render_config(migrated, target_version)
            self.current_version = target_version
            self.result = process_config(migrated, self.snapshots, seed=self.seed)
            self._event(
                "MIGRATION",
                f"{previous_version} -> {target_version}; result invalidated and recomputed",
                lossy=lossy,
                additions=len(audit.additions),
                drops=len(audit.drops),
                previous_result_digest=previous,
                current_result_digest=self.result.result_digest,
            )
            return {"ok": True, "audit": audit.to_dict()}
        except MeetingError as exc:
            self._record_error(exc, action=f"migrate:{target_version}:lossy={lossy}")
            return {"ok": False, "error": exc.to_dict()}

    @_synchronized
    def replace_snapshots(self) -> dict[str, Any]:
        try:
            old_result = self.result
            records = self.snapshots.update_from_config(self.config, force_version=True)
            stale_error: dict[str, Any] | None = None
            try:
                assert_result_fresh(old_result, self.snapshots)
            except MeetingError as exc:
                stale_error = exc.to_dict()
                self._record_error(exc, action="snapshot_force_replace:stale_guard")
            self.result = None  # type: ignore[assignment]
            self.result = process_config(self.config, self.snapshots, seed=self.seed)
            self._event(
                "SNAPSHOT_REPLACE",
                "all three snapshot items replaced as complete documents",
                versions={key: value.version for key, value in records.items()},
                stale_guard=stale_error,
            )
            return {"ok": True, "stale_guard": stale_error}
        except MeetingError as exc:
            self._record_error(exc, action="snapshot_force_replace")
            return {"ok": False, "error": exc.to_dict()}

    @_synchronized
    def probe_rollback(self) -> dict[str, Any]:
        try:
            record = self.snapshots.read("reid")
            if record is None:
                raise MeetingError(
                    "E_SNAPSHOT_CORRUPT",
                    "reid snapshot is absent",
                    {"item": "reid", "version": None},
                )
            self.snapshots.attempt_rollback("reid", max(0, record.version - 1))
            self._record_error(
                MeetingError(
                    "E_SNAPSHOT_CORRUPT",
                    "rollback probe unexpectedly succeeded",
                    {"item": "reid", "version": record.version},
                ),
                action="probe_rollback",
            )
            return {"ok": False}
        except MeetingError as exc:
            self._record_error(exc, action="probe_rollback")
            return {"ok": True, "expected_error": exc.to_dict()}

    @_synchronized
    def probe_corruption(self) -> dict[str, Any]:
        path = self.snapshots.path_for("separation")
        backup = path.read_bytes()
        try:
            path.write_text(
                '{"item":"separation","version":999,"checksum":"tampered","payload":{}}\n',
                encoding="utf-8",
            )
            self.snapshots.read("separation")
        except MeetingError as exc:
            self._record_error(exc, action="probe_corruption")
            return {"ok": True, "expected_error": exc.to_dict()}
        finally:
            path.write_bytes(backup)
        self._record_error(
            MeetingError(
                "E_SNAPSHOT_CORRUPT",
                "corruption probe unexpectedly passed",
                {"item": "separation", "version": None},
            ),
            action="probe_corruption",
        )
        return {"ok": False}

    def _waveform(self) -> dict[str, Any]:
        pcm = self.result.session.pcm
        buckets = 640
        denominator = max(1, pcm.size)
        points: list[list[float]] = []
        for bucket in range(buckets):
            start = bucket * denominator // buckets
            end = max(start + 1, (bucket + 1) * denominator // buckets)
            chunk = pcm[start:end].astype("float64") / 32768.0
            points.append([float(chunk.min()), float(chunk.max())])
        return {
            "bucket_count": buckets,
            "points": points,
            "duration_ms": self.result.session.duration_ms,
            "sample_rate": self.result.session.sample_rate,
        }

    def _config_views(self) -> dict[str, Any]:
        views: dict[str, Any] = {}
        for version in ("v1", "v2", "v3"):
            try:
                text = render_config(self.config, version)
                parsed = parse_config(text)
                views[version] = {
                    "text": text,
                    "semantic_digest": parsed.semantic_digest(),
                    "matches_current": parsed.semantic_digest() == self.config.semantic_digest(),
                }
            except MeetingError as exc:
                views[version] = {"text": "", "error": exc.to_dict()}
        return views

    @_synchronized
    def state_payload(self) -> dict[str, Any]:
        result = self.result
        if result is None:
            raise MeetingError(
                "E_RESULT_STALE",
                "no current derived result; run has been invalidated",
                {"current_version": self.current_version},
            )
        snapshots = self.snapshots.summary()
        last_error = self.errors[-1] if self.errors else None
        return {
            "title": "会议同传后处理控制台",
            "status": {
                "self_check_ok": result.self_check["ok"],
                "current_version": self.current_version,
                "sample_key": self.sample_key,
                "semantic_digest": result.config_digest,
                "result_digest": result.result_digest,
                "snapshot_digest": result.snapshot_digest,
                "last_error_code": last_error["code"] if last_error else None,
                "last_error_sequence": last_error["sequence"] if last_error else None,
            },
            "metrics": {
                "duration_ms": result.session.duration_ms,
                "frame_count": result.session.frame_count,
                "truth_records": len(result.session.truth),
                "candidate_segments": len(result.separation.candidates),
                "separated_segments": len(result.separation.segments),
                "verified_identities": sum(
                    1 for identity in result.identities if identity.matches_truth
                ),
                "pcm_sha256": result.session.pcm_sha256,
                "feature_sha256": result.features.feature_sha256,
                "mask_sha256": result.separation.mask_sha256,
                "segment_sha256": result.separation.segment_sha256,
            },
            "waveform": self._waveform(),
            "frame_energy": [
                float(frame["energy"]) for frame in result.features.frames
            ],
            "truth_segments": result.session.truth_segments,
            "candidates": result.separation.candidates,
            "segments": result.segment_output,
            "timebase": result.self_check["timebase_rows"],
            "config": {
                "version": self.current_version,
                "sample_key": self.sample_key,
                "semantic_version": result.config.version,
                "text": self.current_text,
                "semantic_digest": result.config_digest,
                "provenance": result.config.provenance,
                "views": self._config_views(),
            },
            "migrations": self.migration,
            "snapshots": snapshots,
            "self_check": result.self_check,
            "errors": self.errors[-12:],
            "events": self.events[-16:],
        }
