from __future__ import annotations

import contextlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .errors import (
    BusinessError,
    E_NO_CHECKPOINT,
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
)
from .serialization import (
    atomic_write_json,
    canonical_json_bytes,
    content_checksum,
    sha256_bytes,
)


SNAPSHOT_SCHEMA = 1
PROGRESS_SCHEMA = 1


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            int(pid),
        )
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _lock_owner_pid(lock_path: Path) -> int | None:
    try:
        head = lock_path.read_text(encoding="ascii").splitlines()[0]
        if not head.startswith("pid="):
            return None
        return int(head[4:])
    except (OSError, ValueError, IndexError):
        return None


def _corrupt(stage: str, version: int, message: str, **details: Any) -> BusinessError:
    return BusinessError(
        E_SNAPSHOT_CORRUPT,
        message,
        stage=stage,
        version=version,
        details=details,
    )


@dataclass(frozen=True)
class Snapshot:
    stage: str
    version: int
    seq: int
    completed_layers: tuple[str, ...]
    stage_complete: bool
    next_layer_index: int
    manifest_digest: str
    payload: dict[str, Any]
    checksum: str
    path: str

    def summary(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "version": self.version,
            "seq": self.seq,
            "completed_layers": list(self.completed_layers),
            "stage_complete": self.stage_complete,
            "next_layer_index": self.next_layer_index,
            "manifest_digest": self.manifest_digest,
            "checksum": self.checksum,
            "path": self.path,
        }

    def to_result(self) -> dict[str, Any]:
        return {
            **self.summary(),
            "payload": self.payload,
        }


class SnapshotStore:
    """Atomic, versioned, checksum-protected progress storage.

    The progress index is the authoritative commit view. Snapshot files are
    written first and become visible only after the index is atomically replaced.
    """

    def __init__(self, state_dir: Path, temp_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        self.temp_dir = Path(temp_dir)
        self.snapshots_dir = self.state_dir / "snapshots"
        self.progress_path = self.state_dir / "progress.json"
        self.lock_path = self.state_dir / ".commit.lock"
        for directory in (self.state_dir, self.temp_dir, self.snapshots_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self._assert_writable(self.state_dir)
        self._assert_writable(self.temp_dir)

    @staticmethod
    def _assert_writable(path: Path) -> None:
        probe: Path | None = None
        try:
            fd, name = tempfile.mkstemp(prefix=".write_probe_", dir=path)
            probe = Path(name)
            os.close(fd)
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
        except OSError as exc:
            raise BusinessError(
                "E_IO",
                f"目录不可写: {path}",
                details={"path": str(path), "reason": str(exc)},
            ) from exc
        finally:
            if probe is not None:
                probe.unlink(missing_ok=True)

    def clear(self) -> None:
        if self.state_dir.exists():
            shutil.rmtree(self.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)

    @contextlib.contextmanager
    def hold_commit_lock(self, *, stage: str = "commit") -> Iterator[None]:
        acquired = False
        fd: int | None = None
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            owner = _lock_owner_pid(self.lock_path)
            if owner is not None and not _pid_alive(owner):
                self.lock_path.unlink(missing_ok=True)
                try:
                    fd = os.open(
                        self.lock_path,
                        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    )
                except FileExistsError as exc:
                    raise BusinessError(
                        E_STAGE_BUSY,
                        "已有阶段正在提交，后到者不等待",
                        stage=stage,
                        details={"lock_path": str(self.lock_path)},
                    ) from exc
            else:
                raise BusinessError(
                    E_STAGE_BUSY,
                    "已有阶段正在提交，后到者不等待",
                    stage=stage,
                    details={"lock_path": str(self.lock_path)},
                )
        assert fd is not None
        try:
            os.write(fd, f"pid={os.getpid()}\n".encode("ascii"))
            os.fsync(fd)
            acquired = True
            yield
        finally:
            os.close(fd)
            if acquired:
                self.lock_path.unlink(missing_ok=True)

    def _empty_progress(self) -> dict[str, Any]:
        view = {"schema": PROGRESS_SCHEMA, "entries": []}
        return {**view, "view_checksum": content_checksum(view)}

    def _read_progress(self) -> dict[str, Any]:
        if not self.progress_path.exists():
            return self._empty_progress()
        version = 0
        try:
            raw_bytes = self.progress_path.read_bytes()
            raw = json.loads(raw_bytes)
            entries = raw.get("entries", [])
            if entries:
                versions = [
                    int(entry.get("version", 0))
                    for entry in entries
                    if isinstance(entry, dict)
                ]
                version = max(versions, default=0)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise _corrupt(
                "progress",
                version,
                "进度索引无法读取",
                reason=str(exc),
                path=str(self.progress_path),
            ) from exc
        if not isinstance(raw, dict) or raw.get("schema") != PROGRESS_SCHEMA:
            raise _corrupt(
                "progress",
                version,
                "进度索引 schema 非法",
                path=str(self.progress_path),
            )
        stored = raw.get("view_checksum")
        unsigned = {key: value for key, value in raw.items() if key != "view_checksum"}
        if not isinstance(stored, str) or content_checksum(unsigned) != stored:
            raise _corrupt(
                "progress",
                version,
                "进度索引校验和不一致",
                path=str(self.progress_path),
                expected=stored,
                actual=content_checksum(unsigned),
            )
        if not isinstance(entries, list):
            raise _corrupt("progress", version, "进度索引 entries 非法")
        self._validate_entry_order(entries)
        return raw

    @staticmethod
    def _validate_entry_order(entries: list[dict[str, Any]]) -> None:
        previous = 0
        stage_versions: dict[str, int] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                raise _corrupt("progress", previous, "进度条目必须是对象")
            version = entry.get("version")
            stage = entry.get("stage")
            if not isinstance(version, int) or version <= previous:
                raise _corrupt(
                    "progress",
                    int(version) if isinstance(version, int) else previous,
                    "进度版本号必须严格单调递增",
                    stage=stage,
                )
            if not isinstance(stage, str) or not stage:
                raise _corrupt("progress", version, "进度条目 stage 非法")
            if version <= stage_versions.get(stage, 0):
                raise _corrupt(stage, version, "同一阶段进度版本必须单调递增")
            completed = entry.get("completed_layers")
            if (
                not isinstance(completed, list)
                or any(not isinstance(layer_id, str) for layer_id in completed)
                or len(set(completed)) != len(completed)
            ):
                raise _corrupt(stage, version, "进度点 completed_layers 非法或重复")
            stage_versions[stage] = version
            previous = version

    def _write_progress(self, progress: dict[str, Any]) -> None:
        unsigned = {"schema": PROGRESS_SCHEMA, "entries": progress["entries"]}
        value = {**unsigned, "view_checksum": content_checksum(unsigned)}
        atomic_write_json(self.progress_path, value, temp_dir=self.temp_dir, indent=2)

    def progress(self) -> dict[str, Any]:
        return self._read_progress()

    def progress_view(self) -> dict[str, Any]:
        progress = self._read_progress()
        entries = progress["entries"]
        latest_by_stage: dict[str, dict[str, Any]] = {}
        for entry in entries:
            latest_by_stage[entry["stage"]] = entry
        latest = entries[-1] if entries else None
        return {
            "schema": SNAPSHOT_SCHEMA,
            "view_checksum": progress["view_checksum"],
            "next_version": (entries[-1]["version"] + 1) if entries else 1,
            "entries": entries,
            "latest_by_stage": latest_by_stage,
            "latest": latest,
        }

    def _snapshot_path(self, stage: str, version: int) -> Path:
        return self.snapshots_dir / stage / f"v{version:08d}.json"

    def _read_snapshot_entry(
        self,
        entry: dict[str, Any],
        *,
        expected_stage: str | None = None,
    ) -> Snapshot:
        stage = str(entry.get("stage", "unknown"))
        version = int(entry.get("version", 0) or 0)
        path = self.state_dir / str(entry.get("path", ""))
        if expected_stage is not None and stage != expected_stage:
            raise _corrupt(stage, version, "快照 stage 与索引不一致")
        try:
            raw_bytes = path.read_bytes()
            raw = json.loads(raw_bytes)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise _corrupt(
                stage,
                version,
                "快照文件缺失或 JSON 损坏",
                path=str(path),
                reason=str(exc),
            ) from exc
        if not isinstance(raw, dict):
            raise _corrupt(stage, version, "快照根节点必须是对象", path=str(path))
        stored = raw.get("checksum")
        unsigned = {key: value for key, value in raw.items() if key != "checksum"}
        try:
            actual = content_checksum(unsigned)
        except (TypeError, ValueError) as exc:
            raise _corrupt(stage, version, "快照内容无法规范化", reason=str(exc)) from exc
        if not isinstance(stored, str) or stored != actual:
            raise _corrupt(
                stage,
                version,
                "快照内容校验和不一致",
                path=str(path),
                expected=stored,
                actual=actual,
            )
        if raw.get("schema") != SNAPSHOT_SCHEMA:
            raise _corrupt(stage, version, "快照 schema 非法", path=str(path))
        if raw.get("stage") != stage or int(raw.get("version", -1)) != version:
            raise _corrupt(stage, version, "快照 stage/version 与索引不一致")
        progress_checksum = entry.get("checksum")
        if progress_checksum != stored:
            raise _corrupt(
                stage,
                version,
                "索引中的快照校验和与文件不一致",
                index_checksum=progress_checksum,
                file_checksum=stored,
            )
        completed = raw.get("completed_layers")
        if not isinstance(completed, list) or any(not isinstance(x, str) for x in completed):
            raise _corrupt(stage, version, "completed_layers 非法")
        payload = raw.get("payload")
        if not isinstance(payload, dict):
            raise _corrupt(stage, version, "快照 payload 非法")
        return Snapshot(
            stage=stage,
            version=version,
            seq=int(raw.get("seq", version)),
            completed_layers=tuple(completed),
            stage_complete=bool(raw.get("stage_complete", False)),
            next_layer_index=int(raw.get("next_layer_index", len(completed))),
            manifest_digest=str(raw.get("manifest_digest", "")),
            payload=payload,
            checksum=stored,
            path=str(entry.get("path", "")),
        )

    def latest_entry(self) -> dict[str, Any] | None:
        entries = self._read_progress()["entries"]
        return entries[-1] if entries else None

    def latest(self, *, required: bool = True) -> Snapshot | None:
        entry = self.latest_entry()
        if entry is None:
            if required:
                raise BusinessError(
                    E_NO_CHECKPOINT,
                    "未找到任何已提交进度点，拒绝猜测续算位置",
                    stage="resume",
                    details={"state_dir": str(self.state_dir)},
                )
            return None
        return self._read_snapshot_entry(entry)

    def stage_entry(self, stage: str) -> dict[str, Any] | None:
        latest: dict[str, Any] | None = None
        for entry in self._read_progress()["entries"]:
            if entry["stage"] == stage:
                latest = entry
        return latest

    def stage_snapshot(self, stage: str, *, required: bool = False) -> Snapshot | None:
        entry = self.stage_entry(stage)
        if entry is None:
            if required:
                raise BusinessError(
                    E_NO_CHECKPOINT,
                    f"阶段 {stage} 没有已提交进度点",
                    stage=stage,
                    details={"state_dir": str(self.state_dir)},
                )
            return None
        return self._read_snapshot_entry(entry, expected_stage=stage)

    def all_stage_snapshots(self) -> dict[str, Snapshot]:
        snapshots: dict[str, Snapshot] = {}
        for entry in self._read_progress()["entries"]:
            snapshots[entry["stage"]] = self._read_snapshot_entry(entry)
        return snapshots

    def commit(
        self,
        *,
        stage: str,
        completed_layers: list[str],
        stage_complete: bool,
        next_layer_index: int,
        manifest_digest: str,
        payload: dict[str, Any],
        requested_version: int | None = None,
    ) -> Snapshot:
        if not stage:
            raise BusinessError(E_SNAPSHOT_ROLLBACK, "提交必须指定阶段", stage="commit")
        if len(set(completed_layers)) != len(completed_layers):
            raise BusinessError(
                E_SNAPSHOT_ROLLBACK,
                "completed_layers 中存在重复层",
                stage=stage,
                details={"completed_layers": completed_layers},
            )
        with self.hold_commit_lock(stage=stage):
            progress = self._read_progress()
            entries = list(progress["entries"])
            current_max = int(entries[-1]["version"]) if entries else 0
            stage_versions = [
                int(entry["version"])
                for entry in entries
                if entry["stage"] == stage
            ]
            stage_max = max(stage_versions, default=0)
            version = int(requested_version) if requested_version is not None else current_max + 1
            if requested_version is not None and (
                version <= current_max or version <= stage_max
            ):
                raise BusinessError(
                    E_SNAPSHOT_ROLLBACK,
                    "回退或复用旧进度版本被拒绝",
                    stage=stage,
                    version=version,
                    details={
                        "requested_version": version,
                        "current_max_version": current_max,
                        "stage_max_version": stage_max,
                    },
                )
            if version <= current_max:
                raise BusinessError(
                    E_SNAPSHOT_ROLLBACK,
                    "进度版本必须单调递增",
                    stage=stage,
                    version=version,
                    details={"current_max_version": current_max},
                )
            snapshot_path = self._snapshot_path(stage, version)
            relative_path = str(snapshot_path.relative_to(self.state_dir))
            snapshot_unsigned = {
                "schema": SNAPSHOT_SCHEMA,
                "stage": stage,
                "version": version,
                "seq": version,
                "completed_layers": list(completed_layers),
                "stage_complete": bool(stage_complete),
                "next_layer_index": int(next_layer_index),
                "manifest_digest": str(manifest_digest),
                "payload": payload,
            }
            checksum = content_checksum(snapshot_unsigned)
            snapshot_value = {**snapshot_unsigned, "checksum": checksum}
            atomic_write_json(
                snapshot_path,
                snapshot_value,
                temp_dir=self.temp_dir,
            )
            entry = {
                "stage": stage,
                "version": version,
                "seq": version,
                "completed_layers": list(completed_layers),
                "stage_complete": bool(stage_complete),
                "next_layer_index": int(next_layer_index),
                "manifest_digest": str(manifest_digest),
                "checksum": checksum,
                "path": relative_path,
            }
            new_entries = entries + [entry]
            self._write_progress({"entries": new_entries})
            return Snapshot(
                stage=stage,
                version=version,
                seq=version,
                completed_layers=tuple(completed_layers),
                stage_complete=bool(stage_complete),
                next_layer_index=int(next_layer_index),
                manifest_digest=str(manifest_digest),
                payload=payload,
                checksum=checksum,
                path=relative_path,
            )

    def file_checksum(self, snapshot: Snapshot) -> str:
        return sha256_bytes((self.state_dir / snapshot.path).read_bytes())
