from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import STAGE_ORDER
from .errors import BusinessError, fail
from .util import (
    atomic_write_json,
    read_json,
    sha256_json,
)


@dataclass
class Snapshot:
    stage: str
    version: int
    checksum: str
    completed_call_ids: list[str]
    config_revision: str
    input_signature: dict[str, Any]
    records: list[dict[str, Any]]
    stage_config: dict[str, Any]
    invalidation: dict[str, Any]
    raw: dict[str, Any]
    path: Path

    def record_map(self) -> dict[str, dict[str, Any]]:
        return {str(record["call_id"]): record for record in self.records}


class SnapshotStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "state.json"
        self.work_root = self.root / "work"
        self.work_root.mkdir(parents=True, exist_ok=True)
        self.versions_root = self.root / "stages"
        self.archive_root = self.root / "archive"
        self.versions_root.mkdir(parents=True, exist_ok=True)
        self.archive_root.mkdir(parents=True, exist_ok=True)

    def _default_state(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "stages": {},
            "history": {stage: [] for stage in STAGE_ORDER},
        }

    def load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return self._default_state()
        try:
            state = read_json(self.state_path)
        except (OSError, ValueError) as exc:
            raise BusinessError(
                code="E_SNAPSHOT_CORRUPT",
                context={"stage": "state", "detail": f"cannot read state: {exc}"},
            )
        if not isinstance(state, dict) or state.get("schema_version") != 1:
            fail("E_SNAPSHOT_CORRUPT", stage="state", detail="invalid state schema")
        stages = state.get("stages")
        if not isinstance(stages, dict):
            fail("E_SNAPSHOT_CORRUPT", stage="state", detail="invalid stages map")
        active_order = [
            stage for stage in STAGE_ORDER if stage in stages
        ]
        if active_order != STAGE_ORDER[: len(active_order)]:
            fail(
                "E_SNAPSHOT_CORRUPT",
                stage="state",
                detail="committed stages are not a contiguous pipeline prefix",
            )
        history = state.get("history")
        if not isinstance(history, dict):
            state["history"] = {stage: [] for stage in STAGE_ORDER}
        for stage in STAGE_ORDER:
            state["history"].setdefault(stage, [])
        return state

    def save_state(self, state: dict[str, Any]) -> None:
        atomic_write_json(self.state_path, state)

    def committed_stage_names(self) -> list[str]:
        state = self.load_state()
        return [
            stage for stage in STAGE_ORDER if stage in state.get("stages", {})
        ]

    def _snapshot_path(self, stage: str, version: int) -> Path:
        return (
            self.versions_root
            / stage
            / "versions"
            / f"v{version:06d}"
            / "snapshot.json"
        )

    def _archive_snapshot_path(self, stage: str, version: int) -> Path:
        return (
            self.archive_root
            / stage
            / f"v{version:06d}"
            / "snapshot.json"
        )

    def _all_version_numbers(self, stage: str) -> list[int]:
        values: set[int] = set()
        for root in (
            self.versions_root / stage / "versions",
            self.archive_root / stage,
        ):
            if not root.exists():
                continue
            for entry in root.iterdir():
                if entry.is_dir() and entry.name.startswith("v"):
                    try:
                        values.add(int(entry.name[1:]))
                    except ValueError:
                        continue
        return sorted(values)

    def next_version(self, stage: str) -> int:
        state = self.load_state()
        current = state.get("stages", {}).get(stage)
        current_version = int(current["version"]) if current else 0
        versions = self._all_version_numbers(stage)
        return max([current_version, *versions], default=0) + 1

    def _verify_snapshot(
        self,
        payload: dict[str, Any],
        expected_stage: str | None = None,
        expected_version: int | None = None,
        expected_checksum: str | None = None,
        path: Path | None = None,
    ) -> Snapshot:
        actual_checksum = payload.get("checksum")
        body = {key: value for key, value in payload.items() if key != "checksum"}
        if not actual_checksum or actual_checksum != sha256_json(body):
            fail(
                "E_SNAPSHOT_CORRUPT",
                stage=payload.get("stage", expected_stage or "unknown"),
                detail="checksum mismatch",
                path=str(path) if path else None,
            )
        if expected_checksum and actual_checksum != expected_checksum:
            fail(
                "E_SNAPSHOT_CORRUPT",
                stage=payload.get("stage", expected_stage or "unknown"),
                detail="state checksum does not match snapshot",
                path=str(path) if path else None,
            )
        stage = payload.get("stage")
        version = payload.get("version")
        if expected_stage and stage != expected_stage:
            fail(
                "E_SNAPSHOT_CORRUPT",
                stage=stage or expected_stage,
                detail="snapshot stage mismatch",
                path=str(path) if path else None,
            )
        if expected_version is not None and version != expected_version:
            fail(
                "E_SNAPSHOT_CORRUPT",
                stage=stage or expected_stage or "unknown",
                detail="snapshot version mismatch",
                path=str(path) if path else None,
            )
        if payload.get("all_calls_complete") is not True:
            fail(
                "E_SNAPSHOT_CORRUPT",
                stage=stage or expected_stage or "unknown",
                detail="partial snapshot was published as complete",
            )
        completed = payload.get("completed_call_ids")
        records = payload.get("records")
        if not isinstance(completed, list) or not isinstance(records, list):
            fail(
                "E_SNAPSHOT_CORRUPT",
                stage=stage or expected_stage or "unknown",
                detail="missing completed calls or records",
            )
        if len(completed) != len(set(completed)):
            fail(
                "E_SNAPSHOT_CORRUPT",
                stage=stage or expected_stage or "unknown",
                detail="duplicate completed call id",
            )
        record_ids = [str(record.get("call_id")) for record in records]
        if len(record_ids) != len(set(record_ids)):
            fail(
                "E_SNAPSHOT_CORRUPT",
                stage=stage or expected_stage or "unknown",
                detail="duplicate record call id",
            )
        if set(record_ids) != set(completed):
            fail(
                "E_SNAPSHOT_CORRUPT",
                stage=stage or expected_stage or "unknown",
                detail="record set differs from completed call set",
            )
        return Snapshot(
            stage=str(stage),
            version=int(version),
            checksum=str(actual_checksum),
            completed_call_ids=sorted(str(call_id) for call_id in completed),
            config_revision=str(payload.get("config_revision")),
            input_signature=dict(payload.get("input_signature", {})),
            records=sorted(records, key=lambda item: str(item.get("call_id"))),
            stage_config=dict(payload.get("stage_config", {})),
            invalidation=dict(payload.get("invalidation", {})),
            raw=payload,
            path=path or Path(),
        )

    def get_current(self, stage: str) -> Snapshot | None:
        state = self.load_state()
        entry = state.get("stages", {}).get(stage)
        if entry is None:
            return None
        path = (self.root / str(entry["path"])).resolve()
        if self.root not in path.parents or not path.exists():
            fail(
                "E_SNAPSHOT_CORRUPT",
                stage=stage,
                detail="committed snapshot file is missing",
                path=str(path),
            )
        try:
            payload = read_json(path)
        except (OSError, ValueError) as exc:
            fail(
                "E_SNAPSHOT_CORRUPT",
                stage=stage,
                detail=f"cannot read snapshot: {exc}",
                path=str(path),
            )
        return self._verify_snapshot(
            payload,
            expected_stage=stage,
            expected_version=int(entry["version"]),
            expected_checksum=str(entry["checksum"]),
            path=path,
        )

    def load_version(self, stage: str, version: int) -> Snapshot | None:
        candidates: list[Path] = [
            self._snapshot_path(stage, version),
            self._archive_snapshot_path(stage, version),
        ]
        for path in candidates:
            if not path.exists():
                continue
            try:
                payload = read_json(path)
            except (OSError, ValueError) as exc:
                fail(
                    "E_SNAPSHOT_CORRUPT",
                    stage=stage,
                    detail=f"cannot read archived snapshot: {exc}",
                    path=str(path),
                )
            return self._verify_snapshot(
                payload,
                expected_stage=stage,
                expected_version=version,
                path=path,
            )
        return None

    def _ensure_archive(self, snapshot: Snapshot) -> None:
        target_dir = self.archive_root / snapshot.stage / f"v{snapshot.version:06d}"
        target = target_dir / "snapshot.json"
        if target.exists():
            try:
                existing = read_json(target)
            except (OSError, ValueError):
                fail(
                    "E_SNAPSHOT_CORRUPT",
                    stage=snapshot.stage,
                    detail="archived snapshot is unreadable",
                    path=str(target),
                )
            self._verify_snapshot(
                existing,
                expected_stage=snapshot.stage,
                expected_version=snapshot.version,
                expected_checksum=snapshot.checksum,
                path=target,
            )
            return
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(prefix=".archive-", dir=str(target_dir.parent))
        )
        try:
            shutil.copy2(snapshot.path, temporary / "snapshot.json")
            if target_dir.exists():
                shutil.rmtree(temporary)
            else:
                os.replace(temporary, target_dir)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)

    def _archive_downstream(self, state: dict[str, Any], stage: str) -> list[str]:
        index = STAGE_ORDER.index(stage)
        invalidated: list[str] = []
        for downstream in STAGE_ORDER[index + 1 :]:
            entry = state.get("stages", {}).get(downstream)
            if entry is None:
                continue
            snapshot = self.get_current(downstream)
            if snapshot is None:
                fail(
                    "E_SNAPSHOT_CORRUPT",
                    stage=downstream,
                    detail="state entry cannot be loaded",
                )
            self._ensure_archive(snapshot)
            state["history"].setdefault(downstream, []).append(
                {
                    "action": "invalidated",
                    "version": snapshot.version,
                    "checksum": snapshot.checksum,
                    "reason": f"upstream stage {stage} committed",
                }
            )
            del state["stages"][downstream]
            invalidated.append(downstream)
        return invalidated

    def commit_stage(
        self,
        stage: str,
        records: list[dict[str, Any]],
        completed_call_ids: list[str],
        config_revision: str,
        input_signature: dict[str, Any],
        stage_config: dict[str, Any],
        invalidation: dict[str, Any],
        requested_version: int | None = None,
    ) -> Snapshot:
        if stage not in STAGE_ORDER:
            fail("E_CONFIG_INVALID", detail=f"unknown stage: {stage}")
        index = STAGE_ORDER.index(stage)
        state = self.load_state()
        for previous in STAGE_ORDER[:index]:
            if previous not in state.get("stages", {}):
                fail(
                    "E_INCONSISTENT",
                    stage=stage,
                    detail=f"upstream stage {previous} is not committed",
                )
        current = self.get_current(stage)
        if requested_version is not None and current is not None:
            if requested_version <= current.version:
                fail(
                    "E_SNAPSHOT_ROLLBACK",
                    stage=stage,
                    max_committed_version=current.version,
                    requested_version=requested_version,
                )
        version = (
            requested_version
            if requested_version is not None
            else self.next_version(stage)
        )
        sorted_records = sorted(
            records, key=lambda item: str(item.get("call_id"))
        )
        record_ids = [str(record.get("call_id")) for record in sorted_records]
        completed = sorted({str(call_id) for call_id in completed_call_ids})
        if len(record_ids) != len(set(record_ids)) or set(record_ids) != set(completed):
            fail(
                "E_INCONSISTENT",
                stage=stage,
                detail="completed call set does not match records",
                completed_call_ids=completed,
                record_call_ids=record_ids,
            )
        payload: dict[str, Any] = {
            "schema_version": 1,
            "stage": stage,
            "version": version,
            "all_calls_complete": True,
            "completed_call_ids": completed,
            "config_revision": config_revision,
            "input_signature": input_signature,
            "stage_config": stage_config,
            "invalidation": invalidation,
            "records": sorted_records,
        }
        checksum = sha256_json(payload)
        payload["checksum"] = checksum

        version_dir = (
            self.versions_root / stage / "versions" / f"v{version:06d}"
        )
        if version_dir.exists():
            fail(
                "E_SNAPSHOT_ROLLBACK",
                stage=stage,
                requested_version=version,
                detail="immutable snapshot version already exists",
            )
        version_dir.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(prefix=".commit-", dir=str(version_dir.parent))
        )
        try:
            atomic_write_json(temporary / "snapshot.json", payload)
            os.replace(temporary, version_dir)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)

        if current is not None:
            self._ensure_archive(current)
        invalidated = self._archive_downstream(state, stage)
        relative = (
            Path("stages")
            / stage
            / "versions"
            / f"v{version:06d}"
            / "snapshot.json"
        )
        state["stages"][stage] = {
            "version": version,
            "checksum": checksum,
            "path": relative.as_posix(),
        }
        state["history"].setdefault(stage, []).append(
            {
                "action": "commit",
                "version": version,
                "checksum": checksum,
                "config_revision": config_revision,
                "completed_call_ids": completed,
                "invalidation": invalidation,
            }
        )
        self.save_state(state)
        return self._verify_snapshot(
            payload,
            expected_stage=stage,
            expected_version=version,
            expected_checksum=checksum,
            path=self._snapshot_path(stage, version),
        )

    def attempt_rollback(self, stage: str, requested_version: int) -> None:
        current = self.get_current(stage)
        max_version = current.version if current else 0
        if requested_version <= max_version:
            fail(
                "E_SNAPSHOT_ROLLBACK",
                stage=stage,
                max_committed_version=max_version,
                requested_version=requested_version,
            )
        fail(
            "E_SNAPSHOT_CORRUPT",
            stage=stage,
            detail="requested version does not exist",
            requested_version=requested_version,
        )

    def pending_stage_dir(self, stage: str) -> Path:
        return self.work_root / stage

    def write_pending(
        self, stage: str, call_id: str, record: dict[str, Any]
    ) -> None:
        path = self.pending_stage_dir(stage) / f"{call_id}.json"
        atomic_write_json(path, record)

    def finish_stage_work(self, stage: str) -> None:
        path = self.pending_stage_dir(stage)
        if path.exists():
            shutil.rmtree(path)

    def discard_pending(self) -> list[str]:
        discarded: list[str] = []
        if not self.work_root.exists():
            return discarded
        for entry in sorted(self.work_root.iterdir()):
            if entry.is_dir():
                discarded.append(entry.relative_to(self.root).as_posix())
                shutil.rmtree(entry, ignore_errors=True)
            elif entry.is_file():
                discarded.append(entry.relative_to(self.root).as_posix())
                entry.unlink(missing_ok=True)
        return discarded

