"""检查点持久化与版本变化重建。

检查点按已完成 file_id 与汇点聚合结论存储；写入走“临时文件 + rename”，
并以同目录文件锁串行化并发写入，锁冲突抛出 E_LOCK。
恢复时只接受结构版本与方言白名单版本都匹配的完整检查点，否则整体重建。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .resources import file_lock, atomic_write_text, LockConflict

CHECKPOINT_FILE = "checkpoint.json"
LOCK_FILE = ".checkpoint.lock"


@dataclass
class CheckpointState:
    completed_files: dict[str, dict]
    rebuilt: bool
    rebuild_reason: str | None


class CheckpointStore:
    def __init__(self, directory: str | Path, config: Config):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.config = config
        self.path = self.directory / CHECKPOINT_FILE
        self.lock_path = self.directory / LOCK_FILE

    def load(self) -> CheckpointState:
        """加载最后一个完整检查点；版本不匹配/损坏时返回空状态并标记重建。"""
        if not self.path.exists():
            return CheckpointState({}, False, None)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return CheckpointState({}, True, "检查点文件损坏，整体重建")

        if data.get("complete") is not True:
            return CheckpointState({}, True, "检查点不完整，整体重建")
        if data.get("struct_version") != self.config.checkpoint_struct_version:
            return CheckpointState({}, True, "检查点结构版本变化，整体重建")
        if data.get("dialect_whitelist_version") != self.config.dialect_whitelist_version:
            return CheckpointState({}, True, "方言白名单版本变化，整体重建")

        completed = data.get("completed_files", {})
        if not isinstance(completed, dict):
            return CheckpointState({}, True, "检查点内容异常，整体重建")
        return CheckpointState(completed, False, None)

    def save(self, completed_files: dict[str, dict]) -> None:
        """在文件锁保护下原子替换检查点；锁冲突向上抛 E_LOCK。"""
        payload = {
            "complete": True,
            "struct_version": self.config.checkpoint_struct_version,
            "dialect_whitelist_version": self.config.dialect_whitelist_version,
            "completed_files": completed_files,
        }
        with file_lock(str(self.lock_path)):
            atomic_write_text(str(self.path), json.dumps(payload, ensure_ascii=False, indent=2))

    def reset(self) -> None:
        with file_lock(str(self.lock_path)):
            if self.path.exists():
                self.path.unlink()


__all__ = ["CheckpointStore", "CheckpointState", "LockConflict"]
