"""Versioned snapshot storage, integrity validation, and atomic publication."""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .artifacts import Artifact, read_artifact
from .canonical import atomic_write_json, fsync_directory, hash_object
from .errors import E_SNAPSHOT_VERSION_CONFLICT, WorkbenchError


SNAPSHOT_FORMAT = "incremental-diffusion-snapshot"
SNAPSHOT_FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
ARTIFACTS_DIR = "artifacts"


def snapshot_conflict(message: str, **details: Any) -> WorkbenchError:
    return WorkbenchError(E_SNAPSHOT_VERSION_CONFLICT, message, details=details)


@dataclass
class SnapshotInfo:
    path: Path
    manifest: dict[str, Any]
    artifacts: dict[str, Artifact] = field(default_factory=dict)
    invalid_stages: list[str] = field(default_factory=list)

    @property
    def version(self) -> int:
        return int(self.manifest["snapshot_version"])

    @property
    def config_hash(self) -> str:
        return str(self.manifest["config_hash"])

    @property
    def complete(self) -> bool:
        return bool(self.manifest.get("progress", {}).get("complete", False))

    @property
    def pipeline(self) -> list[str]:
        return list(self.manifest.get("pipeline", []))


@dataclass
class ResolvedSnapshots:
    current: SnapshotInfo | None
    target_complete: SnapshotInfo | None
    staging: SnapshotInfo | None
    staging_dir: Path | None
    target_dir: Path


def artifact_relative_path(stage_id: str, snapshot_version: int) -> str:
    return f"{ARTIFACTS_DIR}/{stage_id}.v{snapshot_version:06d}.npz"


def compute_global_content_fingerprint(
    *, snapshot_version: int, config_hash: str, stages: dict[str, Any]
) -> str:
    return hash_object(
        {
            "snapshot_version": snapshot_version,
            "config_hash": config_hash,
            "stage_content_fingerprints": {
                stage_id: str(record.get("content_fingerprint", ""))
                for stage_id, record in sorted(stages.items())
            },
        }
    )


def refresh_manifest_fingerprint(manifest: dict[str, Any]) -> None:
    manifest["global_content_fingerprint"] = compute_global_content_fingerprint(
        snapshot_version=int(manifest["snapshot_version"]),
        config_hash=str(manifest["config_hash"]),
        stages=dict(manifest.get("stages", {})),
    )


class SnapshotStore:
    def __init__(self, workspace: Path | str) -> None:
        self.workspace = Path(workspace).resolve()
        self.current_link = self.workspace / "current"
        self.snapshots_dir = self.workspace / "snapshots"
        self.staging_root = self.workspace / "staging"

    def target_dir(self, snapshot_version: int) -> Path:
        return self.snapshots_dir / f"v{snapshot_version:06d}"

    def staging_dir(self, snapshot_version: int, config_hash: str) -> Path:
        suffix = config_hash.removeprefix("sha256:")[:16]
        return self.staging_root / f"v{snapshot_version:06d}-{suffix}"

    def _read_manifest_path(self, path: Path) -> dict[str, Any] | None:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                value = json.load(handle)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _load_snapshot_dir(
        self,
        snapshot_dir: Path,
        *,
        strict: bool,
        expected_version: int | None = None,
        expected_config_hash: str | None = None,
    ) -> SnapshotInfo:
        manifest_path = snapshot_dir / MANIFEST_NAME
        manifest = self._read_manifest_path(manifest_path)
        if manifest is None:
            raise snapshot_conflict(
                f"snapshot manifest is missing or invalid: {manifest_path}",
                snapshot_dir=str(snapshot_dir),
            )
        if manifest.get("format") != SNAPSHOT_FORMAT:
            raise snapshot_conflict(
                f"unsupported snapshot format in {manifest_path}",
                snapshot_dir=str(snapshot_dir),
            )
        if manifest.get("format_version") != SNAPSHOT_FORMAT_VERSION:
            raise snapshot_conflict(
                f"unsupported snapshot format version in {manifest_path}",
                snapshot_dir=str(snapshot_dir),
            )
        try:
            version = int(manifest["snapshot_version"])
            config_hash = str(manifest["config_hash"])
            stage_records = dict(manifest["stages"])
            pipeline = list(manifest["pipeline"])
            progress = dict(manifest.get("progress", {}))
        except (KeyError, TypeError, ValueError) as exc:
            raise snapshot_conflict(
                f"malformed snapshot manifest: {type(exc).__name__}: {exc}",
                snapshot_dir=str(snapshot_dir),
            ) from exc
        if expected_version is not None and version != expected_version:
            raise snapshot_conflict(
                f"snapshot version {version} does not match expected {expected_version}",
                snapshot_dir=str(snapshot_dir),
            )
        if expected_config_hash is not None and config_hash != expected_config_hash:
            raise snapshot_conflict(
                "snapshot version is already associated with a different configuration hash",
                snapshot_version=version,
                existing_config_hash=config_hash,
                requested_config_hash=expected_config_hash,
            )

        artifacts: dict[str, Artifact] = {}
        invalid_stages: list[str] = []
        base = snapshot_dir.resolve()
        for stage_id, record in stage_records.items():
            problem: str | None = None
            artifact: Artifact | None = None
            try:
                if not isinstance(record, dict):
                    raise ValueError("stage record must be an object")
                relative = Path(str(record["artifact"]))
                artifact_path = (snapshot_dir / relative).resolve()
                if not artifact_path.is_relative_to(base):
                    raise ValueError("artifact path escapes snapshot directory")
                artifact = read_artifact(artifact_path, validate=True)
                if artifact.file_sha256 != record.get("artifact_sha256"):
                    raise ValueError("artifact file sha256 differs from manifest")
                if artifact.meta["stage"] != stage_id:
                    raise ValueError("artifact stage differs from manifest stage")
                if artifact.meta["kind"] != record.get("kind"):
                    raise ValueError("artifact kind differs from manifest kind")
                if artifact.meta["artifact_version"] != record.get("artifact_version"):
                    raise ValueError("artifact version differs from manifest")
                if record.get("artifact_version") != version:
                    raise ValueError("stage artifact version differs from snapshot version")
                if artifact.meta["content_fingerprint"] != record.get("content_fingerprint"):
                    raise ValueError("artifact content fingerprint differs from manifest")
                if artifact.meta["parameter_fingerprint"] != record.get("parameter_fingerprint"):
                    raise ValueError("artifact parameter fingerprint differs from manifest")
                if artifact.meta["dependency_fingerprints"] != record.get(
                    "dependency_fingerprints"
                ):
                    raise ValueError("artifact dependencies differ from manifest")
            except (KeyError, OSError, ValueError, TypeError) as exc:
                problem = f"{type(exc).__name__}: {exc}"

            if problem is not None:
                invalid_stages.append(stage_id)
                if strict:
                    raise snapshot_conflict(
                        f"snapshot artifact integrity failure at {stage_id}: {problem}",
                        snapshot_version=version,
                        stage=stage_id,
                        snapshot_dir=str(snapshot_dir),
                    )
                stage_records.pop(stage_id, None)
                continue
            assert artifact is not None
            artifacts[stage_id] = artifact

        clean_manifest = dict(manifest)
        clean_manifest["snapshot_version"] = version
        clean_manifest["config_hash"] = config_hash
        clean_manifest["pipeline"] = pipeline
        clean_manifest["stages"] = stage_records
        clean_manifest["progress"] = progress
        expected_global = compute_global_content_fingerprint(
            snapshot_version=version,
            config_hash=config_hash,
            stages=stage_records,
        )
        recorded_global = manifest.get("global_content_fingerprint")
        if recorded_global not in (None, expected_global):
            message = "snapshot global content fingerprint does not match its stage records"
            if strict:
                raise snapshot_conflict(
                    message,
                    snapshot_version=version,
                    snapshot_dir=str(snapshot_dir),
                )
            invalid_stages.append("__manifest__")
        clean_manifest["global_content_fingerprint"] = expected_global

        info = SnapshotInfo(
            path=snapshot_dir.resolve(),
            manifest=clean_manifest,
            artifacts=artifacts,
            invalid_stages=invalid_stages,
        )
        if strict and info.complete:
            missing = [stage_id for stage_id in pipeline if stage_id not in artifacts]
            if missing:
                raise snapshot_conflict(
                    "complete snapshot is missing stage artifacts",
                    snapshot_version=version,
                    missing_stages=missing,
                )
        return info

    def load_current(self) -> SnapshotInfo | None:
        try:
            exists = self.current_link.exists() or self.current_link.is_symlink()
        except OSError:
            exists = False
        if not exists:
            return None
        if self.current_link.is_symlink():
            snapshot_dir = self.current_link.resolve()
        elif self.current_link.is_dir():
            snapshot_dir = self.current_link.resolve()
        else:
            raise snapshot_conflict(
                "workspace current pointer is neither a snapshot directory nor a symlink"
            )
        info = self._load_snapshot_dir(snapshot_dir, strict=True)
        if not info.complete:
            raise snapshot_conflict(
                "workspace current pointer does not reference a complete snapshot",
                snapshot_dir=str(snapshot_dir),
            )
        return info

    def _scan_manifest_dirs(self, root: Path) -> list[tuple[Path, dict[str, Any]]]:
        found: list[tuple[Path, dict[str, Any]]] = []
        if not root.is_dir():
            return found
        try:
            entries = sorted(root.iterdir(), key=lambda path: path.name)
        except OSError:
            return found
        for entry in entries:
            if not entry.is_dir():
                continue
            manifest = self._read_manifest_path(entry / MANIFEST_NAME)
            if manifest is not None:
                found.append((entry, manifest))
        return found

    def _validate_same_version_owner(
        self,
        *,
        target_version: int,
        target_config_hash: str,
        directory: Path,
        manifest: dict[str, Any],
    ) -> None:
        try:
            version = int(manifest["snapshot_version"])
            config_hash = str(manifest["config_hash"])
        except (KeyError, TypeError, ValueError):
            return
        if version == target_version and config_hash != target_config_hash:
            raise snapshot_conflict(
                "snapshot version is already associated with different content",
                snapshot_version=target_version,
                existing_config_hash=config_hash,
                requested_config_hash=target_config_hash,
                snapshot_dir=str(directory),
            )

    def resolve(
        self,
        *,
        target_version: int,
        target_config_hash: str,
    ) -> ResolvedSnapshots:
        current = self.load_current()
        if current is not None:
            if current.version > target_version:
                raise snapshot_conflict(
                    "requested snapshot version is older than the current version",
                    current_version=current.version,
                    requested_version=target_version,
                )
            if current.version == target_version and current.config_hash != target_config_hash:
                raise snapshot_conflict(
                    "snapshot version is already associated with a different configuration hash",
                    snapshot_version=target_version,
                    existing_config_hash=current.config_hash,
                    requested_config_hash=target_config_hash,
                )

        target_complete: SnapshotInfo | None = None
        target_dir = self.target_dir(target_version)
        for directory, raw_manifest in self._scan_manifest_dirs(self.snapshots_dir):
            self._validate_same_version_owner(
                target_version=target_version,
                target_config_hash=target_config_hash,
                directory=directory,
                manifest=raw_manifest,
            )
            try:
                version = int(raw_manifest.get("snapshot_version", -1))
            except (TypeError, ValueError):
                continue
            if version != target_version:
                continue
            if current is not None and directory.resolve() == current.path:
                continue
            info = self._load_snapshot_dir(
                directory,
                strict=True,
                expected_version=target_version,
                expected_config_hash=target_config_hash,
            )
            if not info.complete:
                raise snapshot_conflict(
                    "a published snapshot directory is incomplete",
                    snapshot_version=target_version,
                    snapshot_dir=str(directory),
                )
            if target_complete is not None and target_complete.path != info.path:
                raise snapshot_conflict(
                    "multiple snapshot directories claim the same version",
                    snapshot_version=target_version,
                )
            target_complete = info

        staging: SnapshotInfo | None = None
        staging_dir: Path | None = None
        for directory, raw_manifest in self._scan_manifest_dirs(self.staging_root):
            self._validate_same_version_owner(
                target_version=target_version,
                target_config_hash=target_config_hash,
                directory=directory,
                manifest=raw_manifest,
            )
            try:
                version = int(raw_manifest.get("snapshot_version", -1))
            except (TypeError, ValueError):
                continue
            if version != target_version:
                continue
            if target_complete is not None:
                continue
            info = self._load_snapshot_dir(
                directory,
                strict=False,
                expected_version=target_version,
                expected_config_hash=target_config_hash,
            )
            if staging is None or len(info.artifacts) > len(staging.artifacts):
                staging = info
                staging_dir = directory.resolve()

        if staging is None:
            deterministic = self.staging_dir(target_version, target_config_hash)
            if deterministic.is_dir():
                manifest = self._read_manifest_path(deterministic / MANIFEST_NAME)
                if manifest is None:
                    shutil.rmtree(deterministic, ignore_errors=True)

        return ResolvedSnapshots(
            current=current,
            target_complete=target_complete,
            staging=staging,
            staging_dir=staging_dir,
            target_dir=target_dir,
        )

    def ensure_staging(
        self,
        *,
        snapshot_version: int,
        config_hash: str,
        pipeline: list[str],
        resolved: ResolvedSnapshots,
    ) -> tuple[Path, SnapshotInfo]:
        if resolved.staging is not None and resolved.staging_dir is not None:
            return resolved.staging_dir, resolved.staging
        target = self.staging_dir(snapshot_version, config_hash)
        if target.exists():
            manifest = self._read_manifest_path(target / MANIFEST_NAME)
            if manifest is not None:
                self._validate_same_version_owner(
                    target_version=snapshot_version,
                    target_config_hash=config_hash,
                    directory=target,
                    manifest=manifest,
                )
            shutil.rmtree(target)
        (target / ARTIFACTS_DIR).mkdir(parents=True, exist_ok=False)
        manifest: dict[str, Any] = {
            "format": SNAPSHOT_FORMAT,
            "format_version": SNAPSHOT_FORMAT_VERSION,
            "snapshot_version": snapshot_version,
            "config_hash": config_hash,
            "global_content_fingerprint": "",
            "pipeline": list(pipeline),
            "stages": {},
            "progress": {
                "complete": False,
                "status": "in_progress",
                "completed_stages": [],
                "pending_stages": list(pipeline),
            },
        }
        refresh_manifest_fingerprint(manifest)
        atomic_write_json(target / MANIFEST_NAME, manifest)
        fsync_directory(target)
        info = SnapshotInfo(path=target.resolve(), manifest=manifest, artifacts={})
        return target, info

    def write_manifest(self, snapshot_dir: Path, manifest: dict[str, Any]) -> None:
        refresh_manifest_fingerprint(manifest)
        stages = manifest.get("stages", {})
        pipeline = list(manifest.get("pipeline", []))
        completed = [stage_id for stage_id in pipeline if stage_id in stages]
        pending = [stage_id for stage_id in pipeline if stage_id not in stages]
        progress = dict(manifest.get("progress", {}))
        progress["completed_stages"] = completed
        progress["pending_stages"] = pending
        if progress.get("status") == "in_progress" and not pending:
            progress["status"] = "complete"
            progress["complete"] = True
        manifest["progress"] = progress
        atomic_write_json(snapshot_dir / MANIFEST_NAME, manifest)

    def publish(self, staging_dir: Path, target_version: int) -> Path:
        target_dir = self.target_dir(target_version)
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        if target_dir.exists():
            existing = self._load_snapshot_dir(
                target_dir, strict=True, expected_version=target_version
            )
            staging = self._load_snapshot_dir(
                staging_dir, strict=True, expected_version=target_version
            )
            if (
                existing.config_hash == staging.config_hash
                and existing.manifest.get("global_content_fingerprint")
                == staging.manifest.get("global_content_fingerprint")
            ):
                shutil.rmtree(staging_dir, ignore_errors=True)
                self._switch_current(target_dir)
                return target_dir.resolve()
            raise snapshot_conflict(
                "target snapshot version already exists with different content",
                snapshot_version=target_version,
                snapshot_dir=str(target_dir),
            )
        os.replace(staging_dir, target_dir)
        fsync_directory(target_dir.parent)
        self._switch_current(target_dir)
        return target_dir.resolve()

    def activate_snapshot(self, snapshot_dir: Path) -> Path:
        manifest = self._read_manifest_path(snapshot_dir / MANIFEST_NAME)
        if manifest is None:
            raise snapshot_conflict(
                "cannot activate a snapshot without a manifest",
                snapshot_dir=str(snapshot_dir),
            )
        info = self._load_snapshot_dir(snapshot_dir, strict=True)
        if not info.complete:
            raise snapshot_conflict(
                "cannot activate an incomplete snapshot", snapshot_dir=str(snapshot_dir)
            )
        self._switch_current(snapshot_dir)
        return snapshot_dir.resolve()

    def _switch_current(self, target_dir: Path) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)
        if self.current_link.exists() and not self.current_link.is_symlink():
            raise snapshot_conflict(
                "workspace current pointer is a real file or directory; refusing to replace it"
            )
        relative_target = os.path.relpath(target_dir, self.workspace)
        temp_link = self.workspace / f".current.tmp-{os.getpid()}"
        try:
            try:
                temp_link.unlink()
            except FileNotFoundError:
                pass
            os.symlink(relative_target, temp_link, target_is_directory=True)
            os.replace(temp_link, self.current_link)
            fsync_directory(self.workspace)
        finally:
            try:
                temp_link.unlink()
            except FileNotFoundError:
                pass
