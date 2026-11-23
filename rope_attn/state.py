"""阶段快照状态机：原子提交、单调版本、非阻塞并发控制。

提交规则（对应业务要求 2/7）：
    * 一个阶段对全部层与全部 batch 处理完后才允许提交，部分完成的阶段
      只写进度侧车 ``progress.json``，绝不出现在已提交快照中；
    * 快照先写 ``snapshots/<version>.tmp`` 再 ``os.replace`` 原子落地；
    * 版本号单调递增，旧版本提交返回 E_SNAPSHOT_ROLLBACK；
    * 提交锁为非阻塞 try-lock，冲突立即返回 E_STAGE_BUSY 不等待；
    * 快照携带已完成键集合与内容校验和，恢复时重新校验。
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, List, Optional

from .cache import atomic_write_text
from .errors import SnapshotRollbackError, StageBusyError
from .hashing import canonical_json, digest_iterable

STAGES = ("angles", "rotate", "score", "writeback")
STAGE_INDEX = {name: index for index, name in enumerate(STAGES)}


@dataclass
class Snapshot:
    version: int
    stage: str
    completed_keys: List[str]
    checksum: str
    batch_progress: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict) -> "Snapshot":
        return cls(
            version=payload["version"],
            stage=payload["stage"],
            completed_keys=list(payload["completed_keys"]),
            checksum=payload["checksum"],
            batch_progress=payload.get("batch_progress", {}),
        )

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "stage": self.stage,
            "completed_keys": sorted(self.completed_keys),
            "checksum": self.checksum,
            "batch_progress": self.batch_progress,
        }

    @staticmethod
    def compute_checksum(stage: str, completed_keys: List[str]) -> str:
        return digest_iterable([stage, sorted(completed_keys)])

    def verify(self) -> bool:
        return (
            self.checksum
            == self.compute_checksum(self.stage, self.completed_keys)
        )


class SnapshotStore:
    def __init__(self, state_dir: str) -> None:
        self.root = Path(state_dir)
        self.snapshot_dir = self.root / "snapshots"
        self.root.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.root / ".commit.lock"
        self._progress_path = self.root / "progress.json"
        self._head_path = self.root / "head.json"

    @contextmanager
    def try_commit_lock(self) -> Iterator[None]:
        """非阻塞跨进程锁：拿不到立即 E_STAGE_BUSY。"""
        handle = open(self._lock_path, "a+")
        locked = False
        try:
            if os.name == "nt":
                import msvcrt

                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    locked = True
                except OSError as exc:
                    raise StageBusyError(
                        "另一个阶段快照/增量提交正在生效，后到者立即失败"
                    ) from exc
            else:
                import fcntl

                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                except BlockingIOError as exc:
                    raise StageBusyError(
                        "另一个阶段快照/增量提交正在生效，后到者立即失败"
                    ) from exc
            yield
        finally:
            if locked:
                if os.name == "nt":
                    import msvcrt

                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def latest(self) -> Optional[Snapshot]:
        if not self._head_path.exists():
            return None
        with open(self._head_path, "r", encoding="utf-8") as handle:
            head = json.load(handle)
        snap_path = self.snapshot_dir / head["snapshot"]
        with open(snap_path, "r", encoding="utf-8") as handle:
            snapshot = Snapshot.from_dict(json.load(handle))
        if not snapshot.verify():
            raise SnapshotRollbackError(
                "最近快照校验和不匹配，状态目录可能损坏",
                stage=snapshot.stage,
            )
        return snapshot

    def commit(self, snapshot: Snapshot) -> None:
        with self.try_commit_lock():
            current = self._latest_unlocked()
            if current is not None and snapshot.version <= current.version:
                raise SnapshotRollbackError(
                    f"已提交版本 {current.version} 不得被旧版本 "
                    f"{snapshot.version} 回退覆盖",
                    stage=snapshot.stage,
                )
            if not snapshot.verify():
                raise SnapshotRollbackError(
                    "提交快照校验和自洽失败，拒绝落地", stage=snapshot.stage
                )
            # 阶段名与版本号一一对应：version 从 1 起按 STAGES 顺序。
            expected_stage = STAGES[
                (snapshot.version - 1) % len(STAGES)
            ]
            if snapshot.stage != expected_stage:
                raise SnapshotRollbackError(
                    f"版本 {snapshot.version} 对应阶段应为 {expected_stage}，"
                    f"收到 {snapshot.stage}",
                    stage=snapshot.stage,
                )
            snap_file = self.snapshot_dir / f"{snapshot.version}.json"
            atomic_write_text(snap_file, canonical_json(snapshot.to_dict()))
            atomic_write_text(
                self._head_path,
                canonical_json(
                    {"snapshot": snap_file.name, "version": snapshot.version}
                ),
            )

    def _latest_unlocked(self) -> Optional[Snapshot]:
        if not self._head_path.exists():
            return None
        with open(self._head_path, "r", encoding="utf-8") as handle:
            head = json.load(handle)
        with open(
            self.snapshot_dir / head["snapshot"], "r", encoding="utf-8"
        ) as handle:
            return Snapshot.from_dict(json.load(handle))

    def next_version(self) -> int:
        current = self._latest_unlocked()
        return 1 if current is None else current.version + 1

    def next_stage(self) -> str:
        current = self._latest_unlocked()
        if current is None:
            return STAGES[0]
        return STAGES[(STAGE_INDEX[current.stage] + 1) % len(STAGES)]

    # ---- 进度侧车：阶段内 batch/层 粒度，未提交不进入快照 ----

    def write_progress(self, progress: dict) -> None:
        atomic_write_text(self._progress_path, canonical_json(progress))

    def read_progress(self) -> dict:
        if not self._progress_path.exists():
            return {}
        with open(self._progress_path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def discard_progress(self) -> None:
        """强制终止恢复时丢弃未提交的阶段内中间进度。"""
        if self._progress_path.exists():
            self._progress_path.unlink()

    def all_snapshots(self) -> List[Snapshot]:
        snapshots = []
        for path in sorted(self.snapshot_dir.glob("*.json")):
            with open(path, "r", encoding="utf-8") as handle:
                snapshots.append(Snapshot.from_dict(json.load(handle)))
        return sorted(snapshots, key=lambda item: item.version)
