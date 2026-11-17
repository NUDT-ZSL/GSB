"""阶段快照原子持久化与恢复判定。"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .hashing import fingerprint_array, stable_hash

MANIFEST_NAME = "manifest.json"
STAGES = ("init", "denoise", "upgrade", "decode")


class SnapshotError(RuntimeError):
    """快照损坏或无法恢复。"""


class SnapshotStore:
    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)
        self.stage_dir = os.path.join(self.root, "stages")
        self.frame_dir = os.path.join(self.root, "frames")
        os.makedirs(self.stage_dir, exist_ok=True)
        os.makedirs(self.frame_dir, exist_ok=True)
        self.manifest_path = os.path.join(self.root, MANIFEST_NAME)
        self.manifest = self._load_manifest()

    # ---------- 原子写入 ----------
    def atomic_bytes(self, rel_path: str, payload: bytes) -> str:
        target = os.path.join(self.root, rel_path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=".tmp-", suffix=os.path.basename(rel_path),
            dir=os.path.dirname(target))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, target)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        return target

    def atomic_json(self, rel_path: str, payload: Any) -> str:
        return self.atomic_bytes(
            rel_path, json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                 indent=2).encode("utf-8"))

    def save_array(self, rel_path: str, array: np.ndarray) -> Tuple[str, str]:
        target = self.atomic_bytes(rel_path, np.ascontiguousarray(array).tobytes())
        return target, fingerprint_array(array)

    # ---------- 读取 ----------
    def load_array(self, rel_path: str, shape: Tuple[int, ...],
                   dtype: np.dtype = np.float32) -> np.ndarray:
        path = os.path.join(self.root, rel_path)
        if not os.path.isfile(path):
            raise SnapshotError(f"快照产物缺失: {rel_path}")
        array = np.fromfile(path, dtype=dtype)
        if array.size != int(np.prod(shape)):
            raise SnapshotError(
                f"快照产物尺寸不符: {rel_path} 期望 {shape} 实际 {array.size}")
        return array.reshape(shape)

    def exists(self, rel_path: str) -> bool:
        return os.path.isfile(os.path.join(self.root, rel_path))

    # ---------- 清单 ----------
    def _load_manifest(self) -> Dict[str, Any]:
        if not os.path.isfile(self.manifest_path):
            return {"version": 0, "config_hash": None, "tier_fingerprint": None,
                    "clips": {}, "edit_log": []}
        with open(self.manifest_path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def tier_fingerprint(self, tier_names: List[str]) -> str:
        return stable_hash("tiers", tier_names)

    def is_compatible(self, config_hash: str, tier_names: List[str]) -> bool:
        if self.manifest.get("version", 0) == 0:
            return False
        if self.manifest.get("config_hash") != config_hash:
            return False
        if self.manifest.get("tier_fingerprint") != self.tier_fingerprint(tier_names):
            return False
        return True

    def clip_state(self, clip_id: str) -> Dict[str, Any]:
        return self.manifest.get("clips", {}).get(clip_id, {})

    def begin_run(self, config_hash: str, tier_names: List[str]) -> None:
        if not self.is_compatible(config_hash, tier_names):
            # 配置或档位集合变化：整体失效，清空旧产物与清单。
            self._purge()
            self.manifest = {
                "version": 0,
                "config_hash": config_hash,
                "tier_fingerprint": self.tier_fingerprint(tier_names),
                "clips": {},
                "edit_log": [],
            }
        else:
            self.manifest["config_hash"] = config_hash
            self.manifest["tier_fingerprint"] = self.tier_fingerprint(tier_names)

    def commit_manifest(self, reason: str = "commit") -> int:
        self.manifest["version"] = int(self.manifest.get("version", 0)) + 1
        self.manifest["last_reason"] = reason
        self.atomic_json(MANIFEST_NAME, self.manifest)
        return int(self.manifest["version"])

    def update_clip(self, clip_id: str, state: Dict[str, Any]) -> None:
        clips = self.manifest.setdefault("clips", {})
        clips[clip_id] = state

    def append_edit(self, edit: Dict[str, Any]) -> None:
        self.manifest.setdefault("edit_log", []).append(edit)

    @property
    def version(self) -> int:
        return int(self.manifest.get("version", 0))

    def _purge(self) -> None:
        import shutil
        for directory in (self.stage_dir, self.frame_dir):
            if os.path.isdir(directory):
                shutil.rmtree(directory)
            os.makedirs(directory, exist_ok=True)
        if os.path.isfile(self.manifest_path):
            os.unlink(self.manifest_path)
