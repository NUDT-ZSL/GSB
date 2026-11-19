"""Atomic, versioned four-stage snapshot storage with recovery semantics."""

from __future__ import annotations

import json
import os
import shutil
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .canonical import atomic_write_json, checksum_value, read_json
from .errors import (
    BusinessError,
    E_CONFIG_INVALID,
    E_INCONSISTENT,
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
)


STAGE_ORDER = ("framing", "segmentation", "regression", "aggregation")
SCHEMA_VERSION = 1


class SnapshotStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.manifest_path = self.root / "manifest.json"
        self.snapshots_dir = self.root / "snapshots"
        self.cache_dir = self.root / "cache"
        self.archive_dir = self.root / "archive"
        self.lock_path = self.root / ".commit.lock"

    def initialize_or_reconcile(
        self,
        *,
        analysis_key: str,
        registry_version: int,
        registry_checksum: str,
    ) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        with self._acquire_lock("initialize"):
            if not self.manifest_path.exists():
                manifest = {
                    "schema_version": SCHEMA_VERSION,
                    "analysis_key": analysis_key,
                    "registry_version": registry_version,
                    "registry_checksum": registry_checksum,
                    "generation": 0,
                    "epoch": 1,
                    "stages": {},
                }
                self._write_manifest(manifest)
                return manifest
            manifest = self.load_manifest()
            if manifest["analysis_key"] == analysis_key:
                return manifest
            old_key = str(manifest["analysis_key"])
            old_generation = int(manifest["generation"])
            old_epoch = int(manifest["epoch"])
            self._archive_active_state(old_epoch, old_generation, old_key)
            new_manifest = {
                "schema_version": SCHEMA_VERSION,
                "analysis_key": analysis_key,
                "registry_version": registry_version,
                "registry_checksum": registry_checksum,
                "generation": old_generation,
                "epoch": old_epoch + 1,
                "stages": {},
            }
            self._write_manifest(new_manifest)
            return new_manifest

    def load_manifest(self, *, required: bool = True) -> dict[str, Any] | None:
        if not self.manifest_path.exists():
            if required:
                raise BusinessError(
                    E_CONFIG_INVALID,
                    "Snapshot manifest is missing",
                    {"field": "state", "path": str(self.manifest_path)},
                )
            return None
        try:
            wrapper = read_json(self.manifest_path)
            manifest = wrapper["manifest"]
            stored_checksum = wrapper["checksum"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "Snapshot manifest is not readable",
                {"stage": "manifest", "version": 0, "path": str(self.manifest_path)},
            ) from exc
        actual_checksum = checksum_value(manifest)
        if stored_checksum != actual_checksum:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "Snapshot manifest checksum mismatch",
                {
                    "stage": "manifest",
                    "version": manifest.get("generation", 0),
                    "expected_checksum": stored_checksum,
                    "actual_checksum": actual_checksum,
                },
            )
        if manifest.get("schema_version") != SCHEMA_VERSION:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "Unsupported snapshot manifest schema",
                {"stage": "manifest", "version": manifest.get("generation", 0)},
            )
        return manifest

    @contextmanager
    def hold_commit_lock(self, stage: str = "external") -> Iterator[None]:
        with self._acquire_lock(stage):
            yield

    def commit_stage(
        self,
        stage: str,
        payload: dict[str, Any],
        *,
        analysis_key: str,
        registry_version: int,
        registry_checksum: str,
        supplied_version: int | None = None,
        supplied_revision: int | None = None,
    ) -> dict[str, Any]:
        if stage not in STAGE_ORDER:
            raise BusinessError(
                E_CONFIG_INVALID,
                "Unknown stage",
                {"field": "stage", "actual": stage},
            )
        if "dialog_ids" not in payload or not isinstance(payload["dialog_ids"], list):
            raise BusinessError(
                E_INCONSISTENT,
                "Stage payload must declare dialog_ids",
                {"stage": stage, "field": "dialog_ids"},
            )
        with self._acquire_lock(stage):
            manifest = self.load_manifest()
            assert manifest is not None
            if manifest["analysis_key"] != analysis_key:
                raise BusinessError(
                    E_INCONSISTENT,
                    "Active store belongs to a different analysis context",
                    {"stage": stage, "expected_key": manifest["analysis_key"], "actual_key": analysis_key},
                )
            stages = manifest["stages"]
            if stage in stages:
                current = stages[stage]
                raise BusinessError(
                    E_SNAPSHOT_ROLLBACK,
                    "A committed stage may not be overwritten",
                    {
                        "stage": stage,
                        "current_version": current["version"],
                        "attempted_version": supplied_version or current["version"],
                    },
                )
            expected_stage = STAGE_ORDER[len(stages)]
            if stage != expected_stage:
                raise BusinessError(
                    E_INCONSISTENT,
                    "Stages must commit in order",
                    {
                        "stage": stage,
                        "expected_stage": expected_stage,
                        "committed_stages": list(stages),
                    },
                )
            next_generation = int(manifest["generation"]) + 1
            if supplied_version is not None and supplied_version != next_generation:
                code = E_SNAPSHOT_ROLLBACK if supplied_version < next_generation else E_INCONSISTENT
                raise BusinessError(
                    code,
                    "Supplied snapshot version is not the next monotonic generation",
                    {
                        "stage": stage,
                        "supplied_version": supplied_version,
                        "next_version": next_generation,
                    },
                )
            next_revision = 1
            if supplied_revision is not None and supplied_revision < next_revision:
                raise BusinessError(
                    E_SNAPSHOT_ROLLBACK,
                    "Stage revision may not move backwards",
                    {
                        "stage": stage,
                        "current_revision": next_revision - 1,
                        "attempted_revision": supplied_revision,
                    },
                )
            snapshot = {
                "schema_version": SCHEMA_VERSION,
                "stage": stage,
                "version": next_generation,
                "stage_revision": next_revision,
                "analysis_key": analysis_key,
                "registry_version": registry_version,
                "registry_checksum": registry_checksum,
                "payload_checksum": checksum_value(payload),
                "payload": payload,
            }
            snapshot_checksum = checksum_value(snapshot)
            wrapper = {"snapshot": snapshot, "checksum": snapshot_checksum}
            filename = f"{stage}-v{next_generation:08d}-{snapshot_checksum[:16]}.json"
            self.snapshots_dir.mkdir(parents=True, exist_ok=True)
            snapshot_path = self.snapshots_dir / filename
            atomic_write_json(snapshot_path, wrapper)

            record = {
                "version": next_generation,
                "revision": next_revision,
                "checksum": snapshot_checksum,
                "payload_checksum": snapshot["payload_checksum"],
                "snapshot": filename,
                "dialog_count": len(payload["dialog_ids"]),
                "dialog_ids": sorted(str(item) for item in payload["dialog_ids"]),
                "registry_version": registry_version,
                "registry_checksum": registry_checksum,
            }
            new_manifest = {
                **manifest,
                "generation": next_generation,
                "registry_version": registry_version,
                "registry_checksum": registry_checksum,
                "stages": {**stages, stage: record},
            }
            self._write_manifest(new_manifest)
            return {
                "manifest": new_manifest,
                "snapshot": snapshot,
                "checksum": snapshot_checksum,
                "path": str(snapshot_path),
            }

    def load_stage(self, stage: str) -> dict[str, Any]:
        if stage not in STAGE_ORDER:
            raise BusinessError(E_CONFIG_INVALID, "Unknown stage", {"field": "stage", "actual": stage})
        manifest = self.load_manifest()
        assert manifest is not None
        record = manifest["stages"].get(stage)
        if record is None:
            raise BusinessError(
                E_CONFIG_INVALID,
                "Stage has not been committed",
                {"field": "stage", "stage": stage},
            )
        path = self.snapshots_dir / record["snapshot"]
        try:
            wrapper = read_json(path)
            snapshot = wrapper["snapshot"]
            stored_checksum = wrapper["checksum"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "Committed stage snapshot is not readable",
                {"stage": stage, "version": record["version"], "path": str(path)},
            ) from exc
        actual_checksum = checksum_value(snapshot)
        if stored_checksum != actual_checksum:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "Committed stage snapshot checksum mismatch",
                {
                    "stage": stage,
                    "version": record["version"],
                    "expected_checksum": stored_checksum,
                    "actual_checksum": actual_checksum,
                },
            )
        payload = snapshot.get("payload", {})
        if checksum_value(payload) != snapshot.get("payload_checksum"):
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "Committed stage payload checksum mismatch",
                {"stage": stage, "version": record["version"]},
            )
        metadata_match = (
            snapshot.get("stage") == stage
            and snapshot.get("version") == record["version"]
            and snapshot.get("analysis_key") == manifest["analysis_key"]
            and snapshot.get("registry_version") == manifest["registry_version"]
            and snapshot.get("registry_checksum") == manifest["registry_checksum"]
            and snapshot.get("payload_checksum") == record["payload_checksum"]
        )
        if not metadata_match:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "Snapshot metadata does not match its manifest record",
                {"stage": stage, "version": record["version"]},
            )
        return payload

    def status(self) -> dict[str, Any]:
        manifest = self.load_manifest(required=False)
        if manifest is None:
            return {
                "initialized": False,
                "state_dir": str(self.root),
                "current_stage": STAGE_ORDER[0],
                "last_committed_stage": None,
                "resume_from_stage": STAGE_ORDER[0],
                "generation": 0,
                "epoch": 0,
                "stages": {},
                "report_hash": None,
            }
        stages: dict[str, Any] = {}
        for stage in STAGE_ORDER:
            record = manifest["stages"].get(stage)
            if record is None:
                stages[stage] = {"status": "pending"}
            else:
                stages[stage] = {
                    "status": "committed",
                    "version": record["version"],
                    "revision": record["revision"],
                    "checksum": record["checksum"],
                    "dialog_count": record["dialog_count"],
                    "dialog_ids": record["dialog_ids"],
                }
        committed = [stage for stage in STAGE_ORDER if stage in manifest["stages"]]
        last = committed[-1] if committed else None
        resume_from = (
            STAGE_ORDER[len(committed)] if len(committed) < len(STAGE_ORDER) else None
        )
        report_hash = None
        if "aggregation" in manifest["stages"]:
            aggregation = self.load_stage("aggregation")
            report = aggregation.get("report", {})
            report_hash = report.get("report_hash")
        return {
            "initialized": True,
            "state_dir": str(self.root),
            "analysis_key": manifest["analysis_key"],
            "registry_version": manifest["registry_version"],
            "registry_checksum": manifest["registry_checksum"],
            "generation": manifest["generation"],
            "epoch": manifest["epoch"],
            "current_stage": resume_from or "complete",
            "last_committed_stage": last,
            "resume_from_stage": resume_from,
            "stages": stages,
            "report_hash": report_hash,
        }

    def _write_manifest(self, manifest: dict[str, Any]) -> None:
        wrapper = {"manifest": manifest, "checksum": checksum_value(manifest)}
        atomic_write_json(self.manifest_path, wrapper)

    def _archive_active_state(self, epoch: int, generation: int, old_key: str) -> None:
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        base = f"epoch-{epoch:04d}-generation-{generation:08d}-{old_key[:12]}"
        target = self.archive_dir / base
        suffix = 1
        while target.exists():
            suffix += 1
            target = self.archive_dir / f"{base}-{suffix}"
        target.mkdir(parents=True)
        if self.manifest_path.exists():
            shutil.move(str(self.manifest_path), str(target / "manifest.json"))
        if self.snapshots_dir.exists():
            shutil.move(str(self.snapshots_dir), str(target / "snapshots"))
        if self.cache_dir.exists():
            shutil.move(str(self.cache_dir), str(target / "cache"))
        marker = {
            "reason": "analysis_context_or_registry_version_changed",
            "old_epoch": epoch,
            "old_generation": generation,
            "old_analysis_key": old_key,
            "archived_at_unix": int(time.time()),
        }
        atomic_write_json(target / "archive.json", marker)

    @contextmanager
    def _acquire_lock(self, stage: str) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        acquired = self._try_mkdir_lock()
        if not acquired:
            if self._clear_stale_lock():
                acquired = self._try_mkdir_lock()
        if not acquired:
            raise BusinessError(
                E_STAGE_BUSY,
                "Another stage commit is already in progress",
                {"stage": stage, "lock_path": str(self.lock_path)},
            )
        try:
            owner = {
                "pid": os.getpid(),
                "stage": stage,
                "created_unix_ns": time.time_ns(),
            }
            atomic_write_json(self.lock_path / "owner.json", owner)
            yield
        finally:
            shutil.rmtree(self.lock_path, ignore_errors=True)

    def _try_mkdir_lock(self) -> bool:
        try:
            os.mkdir(self.lock_path)
            return True
        except FileExistsError:
            return False
        except OSError as exc:
            raise BusinessError(
                E_STAGE_BUSY,
                "Commit lock could not be acquired",
                {"stage": "lock", "reason": str(exc)},
            ) from exc

    def _clear_stale_lock(self) -> bool:
        try:
            owner = read_json(self.lock_path / "owner.json")
            pid = int(owner["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            return False
        if pid == os.getpid() or _pid_is_alive(pid):
            return False
        stale_name = f"{self.lock_path.name}.stale-{pid}-{time.time_ns()}"
        stale_path = self.root / stale_name
        try:
            os.rename(self.lock_path, stale_path)
        except OSError:
            return False
        shutil.rmtree(stale_path, ignore_errors=True)
        return True


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if process:
                ctypes.windll.kernel32.CloseHandle(process)
                return True
            return False
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def dump_json_pretty(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)

