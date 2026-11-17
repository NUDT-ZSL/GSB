from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from .io_utils import (
    atomic_write_json,
    ensure_dir,
    move_directory_atomic,
    read_json,
    remove_tree,
    sha256_file,
    utc_now_iso,
)


REQUIRED_STAGES = ("features", "scores", "summary")
STAGE_CONTENT_NAMES = {
    "features": "feature_extraction",
    "scores": "scoring_discrimination",
    "summary": "summary_statistics",
}


class SnapshotStore:
    def __init__(self, state_dir: str | os.PathLike[str]) -> None:
        self.state_dir = Path(state_dir)
        self.versions_dir = self.state_dir / "versions"
        self.manifest_path = self.state_dir / "manifest.json"
        ensure_dir(self.versions_dir)

    def _relative_to_state(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.state_dir.resolve())
            return True
        except ValueError:
            return False

    def load(self) -> dict[str, Any]:
        if not self.manifest_path.exists():
            return {
                "valid": False,
                "present": False,
                "reason": "manifest_absent",
                "orphan_versions": self._orphan_versions(None),
            }
        try:
            manifest = read_json(self.manifest_path)
        except (OSError, ValueError) as exc:
            return {
                "valid": False,
                "present": True,
                "reason": f"manifest_unreadable:{type(exc).__name__}",
                "orphan_versions": self._orphan_versions(None),
            }
        try:
            if not isinstance(manifest, dict) or manifest.get("format_version") != 1:
                raise ValueError("unsupported manifest format")
            version_dir = manifest.get("version_dir")
            if not isinstance(version_dir, str):
                raise ValueError("version_dir missing")
            version_path = self.versions_dir / version_dir
            if not version_path.is_dir() or not self._relative_to_state(version_path):
                raise ValueError("version_dir missing or outside state")
            stages = manifest.get("stages")
            if not isinstance(stages, dict) or set(stages) != set(REQUIRED_STAGES):
                raise ValueError("stage set mismatch")
            payloads: dict[str, Any] = {}
            for name in REQUIRED_STAGES:
                stage = stages[name]
                if not isinstance(stage, dict):
                    raise ValueError(f"stage {name} is invalid")
                relative_path = stage.get("path")
                expected_hash = stage.get("sha256")
                if not isinstance(relative_path, str) or not isinstance(expected_hash, str):
                    raise ValueError(f"stage {name} path/hash missing")
                stage_path = self.state_dir / relative_path
                if not self._relative_to_state(stage_path):
                    raise ValueError(f"stage {name} path escapes state directory")
                if stage_path.parent.resolve() != version_path.resolve():
                    raise ValueError(f"stage {name} belongs to another version")
                if not stage_path.is_file():
                    raise ValueError(f"stage {name} file missing")
                if sha256_file(stage_path) != expected_hash:
                    raise ValueError(f"stage {name} hash mismatch")
                payload = read_json(stage_path)
                if not isinstance(payload, dict) or payload.get("stage") != STAGE_CONTENT_NAMES[name]:
                    raise ValueError(f"stage {name} payload identity mismatch")
                payloads[name] = payload
            stage_versions = manifest.get("stage_versions")
            if not isinstance(stage_versions, dict) or set(stage_versions) != set(
                REQUIRED_STAGES
            ):
                raise ValueError("stage_versions mismatch")
            return {
                "valid": True,
                "present": True,
                "reason": "valid",
                "manifest": manifest,
                "payloads": payloads,
                "orphan_versions": self._orphan_versions(version_dir),
            }
        except (OSError, ValueError, TypeError) as exc:
            return {
                "valid": False,
                "present": True,
                "reason": f"manifest_invalid:{str(exc)}",
                "orphan_versions": self._orphan_versions(
                    manifest.get("version_dir") if isinstance(manifest, dict) else None
                ),
            }

    def _orphan_versions(self, referenced: str | None) -> list[str]:
        if not self.versions_dir.exists():
            return []
        referenced_name = Path(referenced).name if referenced else None
        orphans: list[str] = []
        for child in sorted(self.versions_dir.iterdir(), key=lambda item: item.name):
            if not child.is_dir():
                continue
            if referenced_name and child.name == referenced_name:
                continue
            orphans.append(child.name)
        return orphans

    def commit(
        self,
        *,
        global_version: int,
        cursor: int,
        completed_batches: list[str],
        batch_hashes: list[str],
        config_hash: str,
        feature_config_hash: str,
        data_fingerprint: str,
        effective_config: dict[str, Any],
        stages: dict[str, dict[str, Any]],
        stage_versions: dict[str, int],
        commit_interval: int,
        crash_after_stage: str | None = None,
    ) -> dict[str, Any]:
        if set(stages) != set(REQUIRED_STAGES):
            raise ValueError("exactly features, scores and summary stages are required")
        if set(stage_versions) != set(REQUIRED_STAGES):
            raise ValueError("stage_versions must contain all stages")
        target_name = f"v{global_version:06d}-{config_hash[:8]}-{data_fingerprint[:8]}"
        target_dir = self.versions_dir / target_name
        staging = Path(
            tempfile.mkdtemp(
                prefix=f".staging-{global_version:06d}-",
                dir=str(self.versions_dir),
            )
        )
        stage_manifest: dict[str, Any] = {}
        try:
            for name in REQUIRED_STAGES:
                payload = dict(stages[name])
                payload["stage"] = STAGE_CONTENT_NAMES[name]
                payload["stage_version"] = stage_versions[name]
                stage_path = staging / f"{name}.json"
                atomic_write_json(stage_path, payload)
                if crash_after_stage == name:
                    os._exit(137)
                stage_manifest[name] = {
                    "path": f"versions/{target_name}/{name}.json",
                    "sha256": sha256_file(stage_path),
                    "version": stage_versions[name],
                }
            if target_dir.exists():
                remove_tree(target_dir)
            move_directory_atomic(staging, target_dir)
            if crash_after_stage == "directory":
                os._exit(137)
            manifest = {
                "format_version": 1,
                "global_version": global_version,
                "cursor": cursor,
                "completed_batches": list(completed_batches),
                "batch_hashes": list(batch_hashes),
                "config_hash": config_hash,
                "feature_config_hash": feature_config_hash,
                "data_fingerprint": data_fingerprint,
                "effective_config": effective_config,
                "version_dir": target_name,
                "stage_versions": dict(stage_versions),
                "stages": stage_manifest,
                "commit_interval": commit_interval,
                "created_at": utc_now_iso(),
            }
            atomic_write_json(self.manifest_path, manifest)
            if crash_after_stage == "manifest":
                os._exit(137)
            return manifest
        except BaseException:
            if staging.exists():
                remove_tree(staging)
            raise
