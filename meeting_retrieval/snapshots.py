"""Immutable versioned snapshots, stage state, and non-blocking submission lock."""

from __future__ import annotations

import json
import os
import socket
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .canonical import atomic_write_json, digest, read_json
from .errors import (
    BusinessError,
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_CONFLICT,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
)

STAGE_ORDER: Tuple[str, ...] = (
    "visual_features",
    "text_embeddings",
    "alignment_scores",
    "retrieval_ranking",
)


def _checked_document(payload: Dict[str, Any], checksum: Optional[str] = None) -> Dict[str, Any]:
    body = dict(payload)
    expected = checksum if checksum is not None else body.pop("checksum", None)
    if checksum is not None:
        body.pop("checksum", None)
    actual = digest(body)
    if expected is not None and expected != actual:
        raise BusinessError(
            E_SNAPSHOT_CORRUPT,
            "快照内容校验和不匹配",
            details={"expected": expected, "actual": actual},
        )
    document = dict(body)
    document["checksum"] = actual
    return document


class VersionedRegistry:
    """Whole-document registry. A current pointer atomically selects an archive file."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.sequence_path = self.root / "sequence.json"
        self.high_water_path = self.root / "high_water.json"

    def _kind_dir(self, kind: str) -> Path:
        path = self.root / kind
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _archive_path(self, kind: str, version: int, checksum: str) -> Path:
        return self._kind_dir(kind) / f"v{int(version):08d}-{checksum}.json"

    def _current_path(self, kind: str) -> Path:
        return self._kind_dir(kind) / "current.json"

    def _next_revision(self) -> int:
        record = read_json(
            self.sequence_path,
            default={"revision": 0},
            error_code=E_SNAPSHOT_CORRUPT,
            field="registry_sequence",
        )
        try:
            revision = int(record.get("revision", 0)) + 1
        except (AttributeError, TypeError, ValueError):
            revision = 1
        atomic_write_json(self.sequence_path, {"revision": revision})
        return revision

    def _high_water(self) -> Dict[str, int]:
        raw = read_json(
            self.high_water_path,
            default={"versions": {}},
            error_code=E_SNAPSHOT_CORRUPT,
            field="registry_high_water",
        )
        if not isinstance(raw, dict) or not isinstance(raw.get("versions"), dict):
            raise BusinessError(E_SNAPSHOT_CORRUPT, "注册表版本高水位格式损坏")
        expected = raw.get("checksum")
        body = {"versions": raw["versions"]}
        if expected is not None and expected != digest(body):
            raise BusinessError(E_SNAPSHOT_CORRUPT, "注册表版本高水位校验和不匹配")
        versions: Dict[str, int] = {}
        for kind, version in raw["versions"].items():
            try:
                versions[str(kind)] = int(version)
            except (TypeError, ValueError) as exc:
                raise BusinessError(E_SNAPSHOT_CORRUPT, "注册表版本高水位数值损坏") from exc
        return versions

    def _raise_high_water(self, kind: str, version: int) -> None:
        high_water = self._high_water()
        if version <= high_water.get(kind, 0):
            return
        high_water[kind] = version
        body = {"versions": high_water}
        document = dict(body)
        document["checksum"] = digest(body)
        atomic_write_json(self.high_water_path, document)

    def current(self, kind: str) -> Optional[Dict[str, Any]]:
        path = self._current_path(kind)
        if not path.exists():
            return None
        raw = read_json(path, error_code=E_SNAPSHOT_CORRUPT, field=f"registry:{kind}")
        if not isinstance(raw, dict):
            raise BusinessError(E_SNAPSHOT_CORRUPT, "快照 current 指针损坏", details={"kind": kind})
        document = _checked_document(raw)
        if document.get("kind") != kind:
            raise BusinessError(E_SNAPSHOT_CORRUPT, "快照 kind 不匹配", details={"kind": kind})
        high_water = self._high_water().get(kind, 0)
        if int(document["version"]) < high_water:
            raise BusinessError(
                E_SNAPSHOT_ROLLBACK,
                "当前快照版本低于已观测高水位，疑似物理回退",
                field=kind,
                details={
                    "kind": kind,
                    "current_version": int(document["version"]),
                    "high_water_version": high_water,
                },
            )
        archive = self._archive_path(kind, int(document["version"]), str(document["payload_checksum"]))
        if not archive.exists():
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "快照归档文件缺失",
                details={"kind": kind, "path": str(archive)},
            )
        archived = _checked_document(
            read_json(archive, error_code=E_SNAPSHOT_CORRUPT, field=f"registry:{kind}")
        )
        for field in ("kind", "version", "revision", "payload_checksum", "payload"):
            if archived.get(field) != document.get(field):
                raise BusinessError(
                    E_SNAPSHOT_CORRUPT,
                    "快照 current 与归档内容不一致",
                    details={"kind": kind, "field": field},
                )
        return document

    def history(self, kind: str) -> List[Dict[str, Any]]:
        documents = []
        for path in sorted(self._kind_dir(kind).glob("v*.json")):
            try:
                documents.append(
                    _checked_document(
                        read_json(path, error_code=E_SNAPSHOT_CORRUPT, field=f"registry:{kind}")
                    )
                )
            except BusinessError:
                continue
        return documents

    def put(
        self,
        kind: str,
        payload: Dict[str, Any],
        version: Optional[int] = None,
    ) -> Dict[str, Any]:
        current = self.current(kind)
        payload_hash = digest(payload)
        if current is not None:
            if current["payload_checksum"] == payload_hash:
                if version is None or int(version) == int(current["version"]):
                    return current
                if int(version) < int(current["version"]):
                    raise BusinessError(
                        E_SNAPSHOT_ROLLBACK,
                        "禁止以较低版本覆盖当前快照",
                        field=kind,
                        details={
                            "kind": kind,
                            "new_version": int(version),
                            "current_version": int(current["version"]),
                        },
                    )
            requested_version = int(current["version"]) + 1 if version is None else int(version)
            if requested_version < int(current["version"]):
                raise BusinessError(
                    E_SNAPSHOT_ROLLBACK,
                    "禁止快照版本回退",
                    field=kind,
                    details={
                        "kind": kind,
                        "new_version": requested_version,
                        "current_version": int(current["version"]),
                    },
                )
            if requested_version == int(current["version"]):
                # A same-version content change is treated as a whole replacement
                # with a fresh monotonic version, never an in-place patch.
                requested_version = int(current["version"]) + 1
            old_hashes = {
                document["payload_checksum"]: int(document["version"])
                for document in self.history(kind)
            }
            if payload_hash in old_hashes:
                raise BusinessError(
                    E_SNAPSHOT_ROLLBACK,
                    "禁止用历史快照内容覆盖新版本",
                    field=kind,
                    details={
                        "kind": kind,
                        "historical_version": old_hashes[payload_hash],
                        "current_version": int(current["version"]),
                    },
                )
        else:
            requested_version = 1 if version is None else int(version)
            if requested_version < 1:
                raise BusinessError(
                    E_SNAPSHOT_ROLLBACK,
                    "快照版本号必须从 1 开始",
                    field=kind,
                    details={"kind": kind, "new_version": requested_version},
                )

        revision = self._next_revision()
        body = {
            "kind": kind,
            "version": requested_version,
            "revision": revision,
            "payload_checksum": payload_hash,
            "payload": payload,
        }
        document = _checked_document(body)
        archive = self._archive_path(kind, requested_version, payload_hash)
        if archive.exists():
            archived = _checked_document(
                read_json(archive, error_code=E_SNAPSHOT_CORRUPT, field=f"registry:{kind}")
            )
            if archived["payload_checksum"] != payload_hash:
                raise BusinessError(
                    E_SNAPSHOT_CONFLICT,
                    "目标快照版本文件已存在且内容不同",
                    field=kind,
                    details={"kind": kind, "version": requested_version},
                )
        else:
            atomic_write_json(archive, document)
        atomic_write_json(self._current_path(kind), document)
        self._raise_high_water(kind, requested_version)
        return document


class StageSnapshotStore:
    """Four-phase commit store. Each run gets an isolated stage directory."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.current_run_path = self.root / "current_run.json"
        self.counters_path = self.root / "stage_counters.json"
        self.runs_dir = self.root / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def _clean_temporary_files(self) -> List[str]:
        removed = []
        for path in sorted(self.root.rglob("*.part")):
            try:
                path.unlink()
                removed.append(str(path))
            except OSError:
                pass
        for path in sorted(self.root.rglob(".tmp-*")):
            try:
                path.unlink()
                removed.append(str(path))
            except OSError:
                pass
        return removed

    def current_run(self) -> Optional[Dict[str, Any]]:
        document = read_json(
            self.current_run_path,
            error_code=E_SNAPSHOT_CORRUPT,
            field="current_run",
        )
        if document is None:
            return None
        return _checked_document(document)

    def start_new_run(
        self,
        reason: str = "input_or_config_changed",
        inputs_digest: Optional[str] = None,
    ) -> Dict[str, Any]:
        run_id = f"run-{int(time.time() * 1000)}-{uuid.uuid4().hex[:12]}"
        body = {
            "run_id": run_id,
            "reason": reason,
            "created_ns": time.time_ns(),
            "inputs_digest": inputs_digest,
        }
        document = _checked_document(body)
        atomic_write_json(self.current_run_path, document)
        (self.runs_dir / run_id).mkdir(parents=True, exist_ok=True)
        return document

    def bind_inputs_digest(self, run_id: str, inputs_digest: str) -> Dict[str, Any]:
        current = self.current_run()
        if current is None or current["run_id"] != run_id:
            raise BusinessError(E_SNAPSHOT_CORRUPT, "当前运行不存在，无法绑定输入摘要")
        body = {key: value for key, value in current.items() if key != "checksum"}
        body["inputs_digest"] = inputs_digest
        document = _checked_document(body)
        atomic_write_json(self.current_run_path, document)
        return document

    def _run_dir(self, run_id: str) -> Path:
        path = self.runs_dir / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _stage_path(self, run_id: str, stage: str) -> Path:
        index = STAGE_ORDER.index(stage)
        return self._run_dir(run_id) / f"{index:02d}_{stage}.json"

    def _next_stage_version(self, stage: str) -> int:
        counters = read_json(
            self.counters_path,
            default={},
            error_code=E_SNAPSHOT_CORRUPT,
            field="stage_counters",
        )
        try:
            version = int(counters.get(stage, 0)) + 1
        except (AttributeError, TypeError, ValueError):
            version = 1
        counters[stage] = version
        atomic_write_json(self.counters_path, counters)
        return version

    def commit_stage(
        self,
        stage: str,
        completed_pair_ids: Iterable[str],
        items: Dict[str, Dict[str, Any]],
        run_id: Optional[str] = None,
        lineage: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if stage not in STAGE_ORDER:
            raise BusinessError(E_SNAPSHOT_CORRUPT, "未知阶段", details={"stage": stage})
        current_run = self.current_run()
        if current_run is None:
            current_run = self.start_new_run(reason="implicit_run")
        selected_run = run_id or current_run["run_id"]
        completed = sorted(set(str(pair_id) for pair_id in completed_pair_ids))
        body = {
            "stage": stage,
            "stage_index": STAGE_ORDER.index(stage),
            "run_id": selected_run,
            "version": self._next_stage_version(stage),
            "completed_pair_ids": completed,
            "items": items,
            "lineage": lineage or {},
            "committed_ns": time.time_ns(),
        }
        document = _checked_document(body)
        atomic_write_json(self._stage_path(selected_run, stage), document)
        return document

    def load_stage(self, run_id: str, stage: str) -> Optional[Dict[str, Any]]:
        path = self._stage_path(run_id, stage)
        if not path.exists():
            return None
        document = _checked_document(
            read_json(path, error_code=E_SNAPSHOT_CORRUPT, field=f"stage:{stage}")
        )
        if document.get("stage") != stage or document.get("run_id") != run_id:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "阶段快照标识不匹配",
                details={"path": str(path), "stage": stage, "run_id": run_id},
            )
        return document

    def recover(self) -> Dict[str, Any]:
        discarded = self._clean_temporary_files()
        current_run = self.current_run()
        if current_run is None:
            return {
                "run_id": None,
                "last_committed_stage": None,
                "resume_from_stage": STAGE_ORDER[0],
                "committed_stages": [],
                "discarded_temporary_files": discarded,
                "complete": False,
            }
        run_id = current_run["run_id"]
        committed = []
        last = None
        for stage in STAGE_ORDER:
            document = self.load_stage(run_id, stage)
            if document is None:
                break
            committed.append(
                {
                    "stage": stage,
                    "version": document["version"],
                    "completed_pair_ids": document["completed_pair_ids"],
                    "checksum": document["checksum"],
                }
            )
            last = stage
        last_index = STAGE_ORDER.index(last) if last is not None else -1
        resume_stage = STAGE_ORDER[last_index + 1] if last_index + 1 < len(STAGE_ORDER) else None
        return {
            "run_id": run_id,
            "inputs_digest": current_run.get("inputs_digest"),
            "last_committed_stage": last,
            "resume_from_stage": resume_stage,
            "committed_stages": committed,
            "discarded_temporary_files": discarded,
            "complete": last == STAGE_ORDER[-1],
        }

    def snapshots(self) -> Dict[str, Any]:
        recovery = self.recover()
        return {
            "current_run_id": recovery["run_id"],
            "last_committed_stage": recovery["last_committed_stage"],
            "resume_from_stage": recovery["resume_from_stage"],
            "committed_stages": recovery["committed_stages"],
            "schedule": list(STAGE_ORDER),
        }


class SubmissionLock:
    """Non-blocking cross-process file lock with stale-owner recovery."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._token: Optional[str] = None
        self._held = False

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        if os.name == "nt":
            try:
                import ctypes

                process_query_limited_information = 0x1000
                still_active = 259
                kernel32 = ctypes.windll.kernel32
                handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
                if not handle:
                    return False
                try:
                    exit_code = ctypes.c_ulong()
                    if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                        return True
                    return exit_code.value == still_active
                finally:
                    kernel32.CloseHandle(handle)
            except Exception:
                pass
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False

    def _read_owner(self) -> Optional[Dict[str, Any]]:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
            return raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            return {}

    def acquire(self, operation: str = "incremental_commit") -> Dict[str, Any]:
        for attempt in range(2):
            token = uuid.uuid4().hex
            metadata = {
                "token": token,
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "operation": operation,
                "acquired_ns": time.time_ns(),
            }
            data = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
            temporary = self.path.with_name(f".{self.path.name}.{token}.tmp")
            try:
                descriptor = os.open(
                    str(temporary),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.link(str(temporary), str(self.path))
                except FileExistsError:
                    raise
                except OSError:
                    # Some filesystems do not support hard links. The fallback
                    # still uses exclusive creation, but validation below will
                    # protect against adopting a non-identical live owner.
                    descriptor = os.open(
                        str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                    )
                    with os.fdopen(descriptor, "wb") as handle:
                        handle.write(data)
                        handle.flush()
                        os.fsync(handle.fileno())
                finally:
                    try:
                        temporary.unlink()
                    except FileNotFoundError:
                        pass
                self._token = token
                self._held = True
                return metadata
            except FileExistsError:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
                owner = self._read_owner() or {}
                owner_pid = int(owner.get("pid", -1)) if str(owner.get("pid", "")).lstrip("-").isdigit() else -1
                owner_host = str(owner.get("host", ""))
                same_host = not owner_host or owner_host == socket.gethostname()
                if owner_pid > 0 and same_host and not self._pid_alive(owner_pid) and attempt == 0:
                    try:
                        self.path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                raise BusinessError(
                    E_STAGE_BUSY,
                    "已有增量提交正在生效，后到者立即拒绝",
                    field="submission_lock",
                    details={"owner": owner, "lock_path": str(self.path)},
                )
            except OSError:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
                raise
        raise BusinessError(E_STAGE_BUSY, "无法获取提交锁", field="submission_lock")

    def release(self) -> None:
        if not self._held:
            return
        owner = self._read_owner() or {}
        if owner.get("token") == self._token:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        self._held = False
        self._token = None

    def __enter__(self) -> "SubmissionLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.release()
