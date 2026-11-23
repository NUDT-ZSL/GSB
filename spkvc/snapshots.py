"""Atomic, checksummed stage snapshots and non-blocking commit control."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .config import STAGES
from .errors import fail
from .models import artifact_hash, canonical_bytes, read_json


_PROCESS_LOCKS: set[str] = set()


class NonBlockingLock:
    """Cross-platform advisory lock that fails immediately when already held."""

    def __init__(self, path: str | Path, *, name: str = "stage"):
        self.path = Path(path)
        self.name = name
        self._fd: int | None = None
        self._key = str(self.path.resolve())
        self._held = False

    def __enter__(self) -> "NonBlockingLock":
        if self._key in _PROCESS_LOCKS:
            fail(
                "E_STAGE_BUSY",
                "已有阶段或增量提交持有提交锁",
                field=f"{self.name}_lock",
                details={"lock_path": str(self.path)},
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(self._fd, 0, os.SEEK_SET)
                if os.fstat(self._fd).st_size == 0:
                    os.write(self._fd, b"\0")
                    os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            os.close(self._fd)
            self._fd = None
            fail(
                "E_STAGE_BUSY",
                "已有阶段或增量提交持有提交锁",
                field=f"{self.name}_lock",
                details={"lock_path": str(self.path), "reason": str(exc)},
            )
        _PROCESS_LOCKS.add(self._key)
        self._held = True
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._fd is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None
            self._held = False
            _PROCESS_LOCKS.discard(self._key)


@dataclass
class SnapshotEntry:
    stage: str
    stage_index: int
    version: int
    completed_utterance_ids: list[str]
    content_checksum: str
    payload_file: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "stage_index": self.stage_index,
            "version": self.version,
            "completed_utterance_ids": list(self.completed_utterance_ids),
            "content_checksum": self.content_checksum,
            "payload_file": self.payload_file,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SnapshotEntry":
        return cls(
            stage=str(value["stage"]),
            stage_index=int(value["stage_index"]),
            version=int(value["version"]),
            completed_utterance_ids=[
                str(item) for item in value["completed_utterance_ids"]
            ],
            content_checksum=str(value["content_checksum"]),
            payload_file=str(value["payload_file"]),
        )


@dataclass
class SnapshotState:
    signature: str
    version_counter: int
    entries: list[SnapshotEntry]


class SnapshotStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.snapshot_path = self.root / "snapshots.json"
        self.payload_dir = self.root / "payloads"
        self.payload_dir.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.root / "commit.lock"

    def commit_lock(self) -> NonBlockingLock:
        return NonBlockingLock(self.lock_path, name="commit")

    def _payload_path(self, filename: str) -> Path:
        return self.payload_dir / filename

    def _bundle_checksum(self, bundle: dict[str, Any]) -> str:
        unsigned = dict(bundle)
        unsigned.pop("bundle_checksum", None)
        return artifact_hash(unsigned)

    def load(self) -> SnapshotState:
        if not self.snapshot_path.is_file():
            return SnapshotState(signature="", version_counter=0, entries=[])
        try:
            bundle = read_json(self.snapshot_path)
            if not isinstance(bundle, dict):
                raise ValueError("snapshot bundle must be an object")
        except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
            fail(
                "E_SNAPSHOT_CORRUPT",
                "阶段快照索引损坏",
                field="snapshots",
                details={"path": str(self.snapshot_path), "reason": str(exc)},
            )
        if bundle.get("schema_version") != 1:
            fail(
                "E_SNAPSHOT_CORRUPT",
                "阶段快照 schema 版本未知",
                field="schema_version",
                details={"actual": bundle.get("schema_version")},
            )
        expected_checksum = self._bundle_checksum(bundle)
        if expected_checksum != bundle.get("bundle_checksum"):
            fail(
                "E_SNAPSHOT_CORRUPT",
                "阶段快照索引校验和不一致",
                field="bundle_checksum",
                details={"path": str(self.snapshot_path)},
            )
        try:
            raw_entries = bundle["entries"]
            entries = [SnapshotEntry.from_dict(item) for item in raw_entries]
            signature = str(bundle["signature"])
            version_counter = int(bundle["version_counter"])
        except (KeyError, TypeError, ValueError) as exc:
            fail(
                "E_SNAPSHOT_CORRUPT",
                "阶段快照索引字段损坏",
                field="entries",
                details={"reason": str(exc)},
            )
        self._validate_entries(entries, version_counter)
        for entry in entries:
            payload_path = self._payload_path(entry.payload_file)
            if not payload_path.is_file():
                fail(
                    "E_SNAPSHOT_CORRUPT",
                    "阶段快照 payload 缺失",
                    field="payload_file",
                    details={"stage": entry.stage, "path": str(payload_path)},
                )
            try:
                payload = read_json(payload_path)
            except (OSError, json.JSONDecodeError) as exc:
                fail(
                    "E_SNAPSHOT_CORRUPT",
                    "阶段快照 payload 损坏",
                    field="payload_file",
                    details={
                        "stage": entry.stage,
                        "path": str(payload_path),
                        "reason": str(exc),
                    },
                )
            if artifact_hash(payload) != entry.content_checksum:
                fail(
                    "E_SNAPSHOT_CORRUPT",
                    "阶段快照内容校验和不一致",
                    field="content_checksum",
                    details={"stage": entry.stage, "path": str(payload_path)},
                )
            if payload.get("completed_utterance_ids") != entry.completed_utterance_ids:
                fail(
                    "E_SNAPSHOT_CORRUPT",
                    "快照完成集合与 payload 不一致",
                    field="completed_utterance_ids",
                    details={"stage": entry.stage},
                )
        return SnapshotState(
            signature=signature,
            version_counter=version_counter,
            entries=entries,
        )

    def _validate_entries(
        self, entries: list[SnapshotEntry], version_counter: int
    ) -> None:
        if version_counter < 0:
            fail(
                "E_SNAPSHOT_CORRUPT",
                "快照版本计数器非法",
                field="version_counter",
                details={"actual": version_counter},
            )
        previous_version = 0
        for index, entry in enumerate(entries):
            if entry.stage_index != index or entry.stage != STAGES[index]:
                fail(
                    "E_SNAPSHOT_CORRUPT",
                    "快照阶段不连续或顺序错误",
                    field="stage",
                    details={
                        "expected_index": index,
                        "actual_index": entry.stage_index,
                        "actual_stage": entry.stage,
                    },
                )
            if entry.version <= previous_version or entry.version > version_counter:
                fail(
                    "E_SNAPSHOT_CORRUPT",
                    "快照版本号非单调或超出计数器",
                    field="version",
                    details={"stage": entry.stage, "version": entry.version},
                )
            previous_version = entry.version
            if not entry.completed_utterance_ids:
                fail(
                    "E_SNAPSHOT_CORRUPT",
                    "已提交快照必须包含完成 utterance 集合",
                    field="completed_utterance_ids",
                    details={"stage": entry.stage},
                )
            if len(set(entry.completed_utterance_ids)) != len(
                entry.completed_utterance_ids
            ):
                fail(
                    "E_SNAPSHOT_CORRUPT",
                    "快照完成集合包含重复 utterance_id",
                    field="completed_utterance_ids",
                    details={"stage": entry.stage},
                )

    def payload_path(self, entry: SnapshotEntry) -> Path:
        return self._payload_path(entry.payload_file)

    def _write_payload(
        self, stage_index: int, payload: dict[str, Any]
    ) -> tuple[str, str]:
        checksum = artifact_hash(payload)
        filename = f"stage-{stage_index}-{checksum}.json"
        target = self._payload_path(filename)
        data = canonical_bytes(payload) + b"\n"
        if not target.is_file():
            self._atomic_write(target, data)
        else:
            try:
                existing = target.read_bytes()
            except OSError as exc:
                fail(
                    "E_SNAPSHOT_CORRUPT",
                    "阶段快照 payload 无法读取",
                    field="payload_file",
                    details={"path": str(target), "reason": str(exc)},
                )
            if existing != data:
                fail(
                    "E_SNAPSHOT_CORRUPT",
                    "同校验和 payload 内容不一致",
                    field="payload_file",
                    details={"path": str(target)},
                )
        return checksum, filename

    def _atomic_write(self, target: Path, data: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        try:
            directory_fd = os.open(str(target.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass

    def _write_bundle(
        self,
        *,
        signature: str,
        version_counter: int,
        entries: list[SnapshotEntry],
    ) -> None:
        bundle = {
            "schema_version": 1,
            "signature": signature,
            "version_counter": version_counter,
            "entries": [entry.to_dict() for entry in entries],
        }
        bundle["bundle_checksum"] = self._bundle_checksum(bundle)
        self._atomic_write(self.snapshot_path, canonical_bytes(bundle) + b"\n")

    def commit_stage(
        self,
        *,
        stage_index: int,
        completed_utterance_ids: Iterable[str],
        payload: dict[str, Any],
        signature: str,
        proposed_version: int | None = None,
        expected_content_checksum: str | None = None,
    ) -> SnapshotEntry:
        if stage_index < 0 or stage_index >= len(STAGES):
            fail(
                "E_SNAPSHOT_ORDER",
                "阶段索引超出五阶段范围",
                field="stage_index",
                details={"actual": stage_index},
            )
        completed = sorted({str(item) for item in completed_utterance_ids})
        if not completed:
            fail(
                "E_SNAPSHOT_EMPTY_STAGE",
                "阶段完成集合不能为空",
                field="completed_utterance_ids",
            )
        with self.commit_lock():
            state = self.load()
            stage = STAGES[stage_index]
            if stage_index > len(state.entries):
                fail(
                    "E_SNAPSHOT_ORDER",
                    "不能跳过未提交阶段",
                    field="stage_index",
                    details={
                        "requested": stage_index,
                        "committed_stages": len(state.entries),
                    },
                )
            if state.signature and state.signature != signature and state.entries:
                fail(
                    "E_STATE_SIGNATURE_MISMATCH",
                    "快照签名与当前输入或配置不一致",
                    field="signature",
                    details={
                        "committed": state.signature,
                        "requested": signature,
                    },
                )
            new_version = (
                int(proposed_version)
                if proposed_version is not None
                else state.version_counter + 1
            )
            if new_version <= state.version_counter:
                fail(
                    "E_SNAPSHOT_ROLLBACK",
                    "快照版本回退被拒绝",
                    field="version",
                    details={
                        "proposed": new_version,
                        "committed_counter": state.version_counter,
                    },
                )
            existing_entry = (
                state.entries[stage_index]
                if stage_index < len(state.entries)
                else None
            )
            if existing_entry is not None and new_version <= existing_entry.version:
                fail(
                    "E_SNAPSHOT_ROLLBACK",
                    "阶段快照版本未前进",
                    field="version",
                    details={
                        "stage": stage,
                        "proposed": new_version,
                        "committed": existing_entry.version,
                    },
                )
            checksum, filename = self._write_payload(stage_index, payload)
            if (
                expected_content_checksum is not None
                and expected_content_checksum != checksum
            ):
                fail(
                    "E_SNAPSHOT_CORRUPT",
                    "提交快照的内容校验和与期望不一致",
                    field="content_checksum",
                    details={
                        "stage": stage,
                        "expected": expected_content_checksum,
                        "actual": checksum,
                    },
                )
            entry = SnapshotEntry(
                stage=stage,
                stage_index=stage_index,
                version=new_version,
                completed_utterance_ids=completed,
                content_checksum=checksum,
                payload_file=filename,
            )
            new_entries = list(state.entries[:stage_index])
            new_entries.append(entry)
            self._write_bundle(
                signature=signature,
                version_counter=new_version,
                entries=new_entries,
            )
            return entry

    def replace_all(
        self,
        *,
        stages: list[tuple[int, Iterable[str], dict[str, Any]]],
        signature: str,
        proposed_versions: list[int] | None = None,
    ) -> list[SnapshotEntry]:
        """Atomically replace the complete snapshot set.

        This is used by incremental commits. No partially updated stage set is
        ever visible: payloads are durable first, then one bundle replacement
        publishes all stage entries in a single ``os.replace``.
        """

        if not stages:
            fail(
                "E_SNAPSHOT_EMPTY_STAGE",
                "整体替换不能为空",
                field="stages",
            )
        indexed = sorted(stages, key=lambda item: item[0])
        if [item[0] for item in indexed] != list(range(len(indexed))):
            fail(
                "E_SNAPSHOT_ORDER",
                "整体替换必须覆盖从 0 开始的连续阶段",
                field="stage_index",
                details={"indices": [item[0] for item in indexed]},
            )
        with self.commit_lock():
            state = self.load()
            base = state.version_counter
            versions = (
                [int(item) for item in proposed_versions]
                if proposed_versions is not None
                else [base + index + 1 for index in range(len(indexed))]
            )
            if len(versions) != len(indexed):
                fail(
                    "E_SNAPSHOT_CORRUPT",
                    "整体替换版本列表长度不匹配",
                    field="proposed_versions",
                )
            previous = base
            for version in versions:
                if version <= previous:
                    fail(
                        "E_SNAPSHOT_ROLLBACK",
                        "整体替换包含回退版本",
                        field="version",
                        details={
                            "proposed": version,
                            "previous": previous,
                        },
                    )
                previous = version
            entries: list[SnapshotEntry] = []
            for item, version in zip(indexed, versions):
                stage_index, completed, payload = item
                if stage_index >= len(STAGES):
                    fail(
                        "E_SNAPSHOT_ORDER",
                        "阶段索引超出五阶段范围",
                        field="stage_index",
                        details={"actual": stage_index},
                    )
                completed_ids = sorted({str(value) for value in completed})
                if not completed_ids:
                    fail(
                        "E_SNAPSHOT_EMPTY_STAGE",
                        "阶段完成集合不能为空",
                        field="completed_utterance_ids",
                    )
                checksum, filename = self._write_payload(stage_index, payload)
                entries.append(
                    SnapshotEntry(
                        stage=STAGES[stage_index],
                        stage_index=stage_index,
                        version=version,
                        completed_utterance_ids=completed_ids,
                        content_checksum=checksum,
                        payload_file=filename,
                    )
                )
            self._write_bundle(
                signature=signature,
                version_counter=versions[-1],
                entries=entries,
            )
            return entries

    def committed_stage_count(self) -> int:
        return len(self.load().entries)

    def latest_payloads(self) -> dict[str, dict[str, Any]]:
        state = self.load()
        return {
            entry.stage: read_json(self._payload_path(entry.payload_file))
            for entry in state.entries
        }

