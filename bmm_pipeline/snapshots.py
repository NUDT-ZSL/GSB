from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .errors import PipelineError, error


SCHEMA_VERSION = 1
STAGES = ("validation", "planning", "accumulation", "aggregation")
STAGE_INDEX = {stage: index + 1 for index, stage in enumerate(STAGES)}
SNAPSHOT_NAME_RE = re.compile(
    r"^(?P<version>\d{4})-(?P<stage>validation|planning|accumulation|aggregation)\.json$"
)


def canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def sha256_bytes(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def content_checksum(document: dict[str, Any]) -> str:
    body = {key: value for key, value in document.items() if key != "content_checksum"}
    return sha256_bytes(canonical_json(body).encode("utf-8"))


def atomic_write_bytes(path: str | Path, data: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
        try:
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def atomic_write_json(path: str | Path, payload: Any) -> None:
    text = json.dumps(
        payload,
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
    )
    atomic_write_bytes(path, (text + "\n").encode("utf-8"))


def encode_array(array: np.ndarray) -> dict[str, Any]:
    contiguous = np.ascontiguousarray(array, dtype="<f8")
    raw = contiguous.tobytes(order="C")
    return {
        "shape": [int(value) for value in contiguous.shape],
        "dtype": "<f8",
        "data": base64.b64encode(raw).decode("ascii"),
        "checksum": sha256_bytes(raw),
    }


def decode_array(payload: dict[str, Any]) -> np.ndarray:
    raw = base64.b64decode(payload["data"], validate=True)
    checksum = sha256_bytes(raw)
    if checksum != payload["checksum"]:
        raise error(
            "E_SNAPSHOT_CORRUPT",
            "快照内数组校验和不一致",
            field="array",
            details={"expected": payload["checksum"], "actual": checksum},
        )
    dtype = np.dtype(payload.get("dtype", "<f8"))
    array = np.frombuffer(raw, dtype=dtype).copy()
    return array.reshape(tuple(int(value) for value in payload["shape"])).astype(
        np.float64, copy=False
    )


@dataclass
class CommittedSnapshot:
    stage: str
    stage_index: int
    version: int
    completed_jobs: list[str]
    job_count: int
    config_fingerprint: str
    jobset_fingerprint: str
    previous_snapshot_checksum: str | None
    payload: dict[str, Any]
    content_checksum: str
    path: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "stage": self.stage,
            "stage_index": self.stage_index,
            "version": self.version,
            "completed_jobs": self.completed_jobs,
            "job_count": self.job_count,
            "config_fingerprint": self.config_fingerprint,
            "jobset_fingerprint": self.jobset_fingerprint,
            "previous_snapshot_checksum": self.previous_snapshot_checksum,
            "payload": self.payload,
            "content_checksum": self.content_checksum,
        }

    def summary(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "version": self.version,
            "content_checksum": self.content_checksum,
            "completed_jobs": len(self.completed_jobs),
            "snapshot_file": self.path.name,
        }


class SnapshotStore:
    def __init__(self, run_dir: str | Path, *, lock_stale_seconds: float = 60.0):
        self.run_dir = Path(run_dir)
        self.snapshot_dir = self.run_dir / "snapshots"
        self.current_path = self.run_dir / "CURRENT.json"
        self.lock_path = self.run_dir / "commit.lock"
        self.progress_path = self.run_dir / "progress.json"
        self.results_dir = self.run_dir / "results"
        self.error_log_path = self.run_dir / "errors.jsonl"
        self.lock_stale_seconds = lock_stale_seconds

    def ensure_layout(self) -> None:
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)

    def _acquire_lock(self, stage: str) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            descriptor = os.open(self.lock_path, flags)
        except FileExistsError as exc:
            try:
                age = time.time() - self.lock_path.stat().st_mtime
            except FileNotFoundError:
                age = 0.0
            if age > self.lock_stale_seconds:
                try:
                    self.lock_path.unlink()
                except FileNotFoundError:
                    pass
                try:
                    descriptor = os.open(self.lock_path, flags)
                except FileExistsError as retry_exc:
                    raise self._busy_error(stage) from retry_exc
            else:
                raise self._busy_error(stage) from exc
        payload = canonical_json(
            {"pid": os.getpid(), "created_ns": time.time_ns(), "stage": stage}
        ).encode("utf-8")
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _release_lock(self) -> None:
        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            pass

    def _busy_error(self, stage: str) -> PipelineError:
        return error(
            "E_STAGE_BUSY",
            "另一个阶段提交正在进行，后到提交不等待",
            stage=stage,
            details={"lock_file": str(self.lock_path)},
        )

    def _snapshot_path(self, version: int, stage: str) -> Path:
        return self.snapshot_dir / f"{version:04d}-{stage}.json"

    def load_committed(self) -> list[CommittedSnapshot]:
        if not self.snapshot_dir.exists():
            if self.current_path.exists():
                raise error(
                    "E_SNAPSHOT_CORRUPT",
                    "CURRENT.json 存在但快照目录为空",
                    field="snapshot",
                )
            return []
        snapshots: list[CommittedSnapshot] = []
        paths = sorted(self.snapshot_dir.glob("*.json"))
        for path in paths:
            snapshots.append(self._load_snapshot(path))
        snapshots.sort(key=lambda item: item.version)
        if not snapshots:
            if self.current_path.exists():
                raise error(
                    "E_SNAPSHOT_CORRUPT",
                    "CURRENT.json 存在但没有有效快照",
                    field="snapshot",
                )
            return []
        self._validate_chain(snapshots)
        self._validate_current(snapshots)
        return snapshots

    def _load_snapshot(self, path: Path) -> CommittedSnapshot:
        inferred_stage: str | None = None
        inferred_version: int | None = None
        match = SNAPSHOT_NAME_RE.match(path.name)
        if match:
            inferred_stage = match.group("stage")
            inferred_version = int(match.group("version"))
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise error(
                "E_SNAPSHOT_CORRUPT",
                "阶段快照无法完整解析",
                stage=inferred_stage,
                version=inferred_version,
                details={"snapshot_file": path.name, "reason": str(exc)},
            ) from exc
        if not isinstance(document, dict):
            raise error(
                "E_SNAPSHOT_CORRUPT",
                "阶段快照根节点必须是对象",
                stage=inferred_stage,
                version=inferred_version,
                details={"snapshot_file": path.name},
            )
        stage = document.get("stage")
        version = document.get("version")
        if not isinstance(stage, str) or stage not in STAGE_INDEX:
            raise error(
                "E_SNAPSHOT_CORRUPT",
                "阶段快照的 stage 非法",
                stage=str(stage) if stage is not None else inferred_stage,
                version=int(version) if isinstance(version, int) else inferred_version,
                details={"snapshot_file": path.name},
            )
        if not isinstance(version, int) or version <= 0:
            raise error(
                "E_SNAPSHOT_CORRUPT",
                "阶段快照的 version 非法",
                stage=stage,
                version=inferred_version,
                details={"snapshot_file": path.name},
            )
        if inferred_stage is not None and (
            inferred_stage != stage or inferred_version != version
        ):
            raise error(
                "E_SNAPSHOT_CORRUPT",
                "快照文件名与内容中的阶段/版本不一致",
                stage=stage,
                version=version,
                details={
                    "snapshot_file": path.name,
                    "filename_stage": inferred_stage,
                    "filename_version": inferred_version,
                },
            )
        required = {
            "schema_version",
            "stage",
            "stage_index",
            "version",
            "completed_jobs",
            "job_count",
            "config_fingerprint",
            "jobset_fingerprint",
            "previous_snapshot_checksum",
            "payload",
            "content_checksum",
        }
        missing = required.difference(document)
        if missing:
            raise error(
                "E_SNAPSHOT_CORRUPT",
                "阶段快照缺少必要字段",
                stage=stage,
                version=version,
                details={"missing": sorted(missing), "snapshot_file": path.name},
            )
        if document["schema_version"] != SCHEMA_VERSION:
            raise error(
                "E_SNAPSHOT_CORRUPT",
                "快照 schema_version 不受支持",
                stage=stage,
                version=version,
                details={
                    "schema_version": document["schema_version"],
                    "supported": SCHEMA_VERSION,
                },
            )
        actual_checksum = content_checksum(document)
        if actual_checksum != document["content_checksum"]:
            raise error(
                "E_SNAPSHOT_CORRUPT",
                "阶段快照内容校验和不一致，拒绝使用部分可解析数据",
                stage=stage,
                version=version,
                details={
                    "expected": document["content_checksum"],
                    "actual": actual_checksum,
                    "snapshot_file": path.name,
                },
            )
        completed_jobs = document["completed_jobs"]
        if (
            not isinstance(completed_jobs, list)
            or not all(isinstance(value, str) for value in completed_jobs)
            or completed_jobs != sorted(set(completed_jobs))
        ):
            raise error(
                "E_SNAPSHOT_CORRUPT",
                "completed_jobs 非法或未规范化",
                stage=stage,
                version=version,
                details={"snapshot_file": path.name},
            )
        if document["job_count"] != len(completed_jobs):
            raise error(
                "E_SNAPSHOT_CORRUPT",
                "快照 job_count 与已完成任务数不一致",
                stage=stage,
                version=version,
                details={
                    "job_count": document["job_count"],
                    "completed": len(completed_jobs),
                },
            )
        if not isinstance(document["payload"], dict):
            raise error(
                "E_SNAPSHOT_CORRUPT",
                "快照 payload 必须是对象",
                stage=stage,
                version=version,
            )
        return CommittedSnapshot(
            stage=stage,
            stage_index=int(document["stage_index"]),
            version=version,
            completed_jobs=list(completed_jobs),
            job_count=int(document["job_count"]),
            config_fingerprint=str(document["config_fingerprint"]),
            jobset_fingerprint=str(document["jobset_fingerprint"]),
            previous_snapshot_checksum=document["previous_snapshot_checksum"],
            payload=dict(document["payload"]),
            content_checksum=str(document["content_checksum"]),
            path=path,
        )

    def _validate_chain(self, snapshots: list[CommittedSnapshot]) -> None:
        for expected_version, snapshot in enumerate(snapshots, start=1):
            if snapshot.version != expected_version:
                raise error(
                    "E_SNAPSHOT_CORRUPT",
                    "快照版本号不连续",
                    stage=snapshot.stage,
                    version=snapshot.version,
                    details={"expected_version": expected_version},
                )
            expected_stage = STAGES[expected_version - 1]
            if (
                snapshot.stage != expected_stage
                or snapshot.stage_index != expected_version
            ):
                raise error(
                    "E_SNAPSHOT_CORRUPT",
                    "快照阶段顺序非法",
                    stage=snapshot.stage,
                    version=snapshot.version,
                    details={"expected_stage": expected_stage},
                )
            expected_previous = (
                None
                if expected_version == 1
                else snapshots[expected_version - 2].content_checksum
            )
            if snapshot.previous_snapshot_checksum != expected_previous:
                raise error(
                    "E_SNAPSHOT_CORRUPT",
                    "快照前序校验和链断裂",
                    stage=snapshot.stage,
                    version=snapshot.version,
                    details={"expected_previous": expected_previous},
                )

    def _validate_current(self, snapshots: list[CommittedSnapshot]) -> None:
        if not self.current_path.exists():
            return
        try:
            pointer = json.loads(self.current_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise error(
                "E_SNAPSHOT_CORRUPT",
                "CURRENT.json 无法解析",
                field="CURRENT.json",
                details={"reason": str(exc)},
            ) from exc
        version = pointer.get("version")
        stage = pointer.get("stage")
        checksum = pointer.get("content_checksum")
        if (
            not isinstance(version, int)
            or not isinstance(stage, str)
            or not isinstance(checksum, str)
        ):
            raise error(
                "E_SNAPSHOT_CORRUPT",
                "CURRENT.json 缺少合法的版本、阶段或校验和字段",
                stage=stage if isinstance(stage, str) else None,
                version=version if isinstance(version, int) else None,
            )
        matching = next(
            (snapshot for snapshot in snapshots if snapshot.version == version), None
        )
        if matching is None:
            if isinstance(version, int) and version > snapshots[-1].version:
                raise error(
                    "E_SNAPSHOT_CORRUPT",
                    "CURRENT.json 指向不存在的更新版本",
                    stage=stage if isinstance(stage, str) else None,
                    version=version,
                )
            return
        if matching.stage != stage or matching.content_checksum != checksum:
            raise error(
                "E_SNAPSHOT_CORRUPT",
                "CURRENT.json 的阶段或内容校验和与快照不一致",
                stage=matching.stage,
                version=matching.version,
                details={
                    "pointer_stage": stage,
                    "pointer_checksum": checksum,
                    "snapshot_checksum": matching.content_checksum,
                },
            )

    def commit_stage(
        self,
        stage: str,
        payload: dict[str, Any],
        completed_jobs: Iterable[str],
        *,
        expected_jobs: Iterable[str],
        config_fingerprint: str,
        jobset_fingerprint: str,
        version: int | None = None,
    ) -> CommittedSnapshot:
        if stage not in STAGE_INDEX:
            raise error(
                "E_STAGE_INVALID",
                f"未知阶段: {stage}",
                stage=stage,
            )
        expected_source = list(expected_jobs)
        expected = sorted(set(expected_source))
        completed = sorted(set(completed_jobs))
        if len(expected) != len(expected_source):
            raise error(
                "E_STAGE_INCOMPLETE",
                "期望任务集合包含重复 job_id",
                stage=stage,
            )
        if completed != expected:
            raise error(
                "E_STAGE_INCOMPLETE",
                "阶段任务未全部完成，禁止提交",
                stage=stage,
                details={
                    "completed_count": len(completed),
                    "expected_count": len(expected),
                    "missing": sorted(set(expected).difference(completed)),
                },
            )
        self.ensure_layout()
        self._acquire_lock(stage)
        try:
            existing = self.load_committed()
            expected_version = existing[-1].version + 1 if existing else 1
            requested_version = expected_version if version is None else version
            expected_stage = STAGES[expected_version - 1]
            if requested_version != expected_version:
                if requested_version < expected_version:
                    raise error(
                        "E_SNAPSHOT_ROLLBACK",
                        "拒绝以旧版本覆盖已提交的新版本快照",
                        stage=stage,
                        version=requested_version,
                        details={
                            "latest_version": existing[-1].version if existing else 0,
                            "expected_version": expected_version,
                        },
                    )
                raise error(
                    "E_SNAPSHOT_ROLLBACK",
                    "快照版本必须单调递增且不能跳过版本",
                    stage=stage,
                    version=requested_version,
                    details={"expected_version": expected_version},
                )
            if stage != expected_stage:
                if STAGE_INDEX[stage] <= (existing[-1].stage_index if existing else 0):
                    raise error(
                        "E_SNAPSHOT_ROLLBACK",
                        "拒绝重复提交或回退已提交阶段",
                        stage=stage,
                        version=requested_version,
                        details={"expected_stage": expected_stage},
                    )
                raise error(
                    "E_STAGE_INVALID",
                    "阶段提交顺序非法",
                    stage=stage,
                    version=requested_version,
                    details={"expected_stage": expected_stage},
                )
            path = self._snapshot_path(requested_version, stage)
            if path.exists():
                raise error(
                    "E_SNAPSHOT_ROLLBACK",
                    "目标快照已存在，禁止覆盖",
                    stage=stage,
                    version=requested_version,
                )
            document: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "stage": stage,
                "stage_index": STAGE_INDEX[stage],
                "version": requested_version,
                "completed_jobs": completed,
                "job_count": len(completed),
                "config_fingerprint": config_fingerprint,
                "jobset_fingerprint": jobset_fingerprint,
                "previous_snapshot_checksum": (
                    existing[-1].content_checksum if existing else None
                ),
                "payload": payload,
            }
            document["content_checksum"] = content_checksum(document)
            atomic_write_json(path, document)
            pointer = {
                "stage": stage,
                "stage_index": STAGE_INDEX[stage],
                "version": requested_version,
                "content_checksum": document["content_checksum"],
                "completed_count": len(completed),
                "job_count": len(completed),
                "snapshot_file": path.name,
            }
            atomic_write_json(self.current_path, pointer)
            return CommittedSnapshot(
                stage=stage,
                stage_index=STAGE_INDEX[stage],
                version=requested_version,
                completed_jobs=completed,
                job_count=len(completed),
                config_fingerprint=config_fingerprint,
                jobset_fingerprint=jobset_fingerprint,
                previous_snapshot_checksum=document["previous_snapshot_checksum"],
                payload=dict(payload),
                content_checksum=document["content_checksum"],
                path=path,
            )
        finally:
            self._release_lock()

    def sync_current(self) -> None:
        self.ensure_layout()
        self._acquire_lock("sync")
        try:
            snapshots = self.load_committed()
            if not snapshots:
                return
            latest = snapshots[-1]
            pointer = {
                "stage": latest.stage,
                "stage_index": latest.stage_index,
                "version": latest.version,
                "content_checksum": latest.content_checksum,
                "completed_count": len(latest.completed_jobs),
                "job_count": latest.job_count,
                "snapshot_file": latest.path.name,
            }
            atomic_write_json(self.current_path, pointer)
        finally:
            self._release_lock()

    def needs_current_sync(self, latest: CommittedSnapshot) -> bool:
        if not self.current_path.is_file():
            return True
        try:
            pointer = json.loads(self.current_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return True
        return (
            pointer.get("stage") != latest.stage
            or pointer.get("version") != latest.version
            or pointer.get("content_checksum") != latest.content_checksum
        )

    def read_progress(self) -> dict[str, Any] | None:
        if not self.progress_path.exists():
            return None
        try:
            value = json.loads(self.progress_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def write_progress(self, payload: dict[str, Any]) -> None:
        atomic_write_json(self.progress_path, payload)

    def clear_progress(self) -> None:
        try:
            self.progress_path.unlink()
        except FileNotFoundError:
            pass

    def snapshot_for_stage(
        self, snapshots: list[CommittedSnapshot], stage: str
    ) -> CommittedSnapshot | None:
        return next((item for item in snapshots if item.stage == stage), None)
