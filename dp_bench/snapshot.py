"""阶段性快照与恢复。

快照写入采用“同目录临时文件 + os.replace 原子替换”，强制终止后只可能读到
完整替换的快照或上一个完整版本，绝不会读到半文件。

快照文件::

    snapshot_000001.json, snapshot_000002.json, ...
    latest.json -> 最新版本内容（同样原子替换）

每个快照包含：阶段名、配置哈希、模型哈希、已完成句 ID 列表、全局已消耗
步数、版本号。已完成句的完整结果另以 results/<sid>.json 原子落盘，恢复时
据此重建内存结果，保证最终报告与不中断运行逐字段一致。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .errors import BenchError, E_CONFIG_MISMATCH

STAGE_PARSE = "parse"
STAGE_SUMMARY = "summary"


class ConfigMismatchError(BenchError):
    code = E_CONFIG_MISMATCH


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, sort_keys=True,
                      indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class SnapshotStore:
    def __init__(self, directory: str | Path, config_hash: str,
                 model_hash: str):
        self.dir = Path(directory)
        self.results_dir = self.dir / "results"
        self.config_hash = config_hash
        self.model_hash = model_hash
        self.dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)

    # ---- 写入 -----------------------------------------------------------
    def write_snapshot(self, stage: str, completed_ids: list[str],
                       global_consumed: int, version: int) -> Path:
        payload = {
            "stage": stage,
            "config_hash": self.config_hash,
            "model_hash": self.model_hash,
            "completed_sentence_ids": list(completed_ids),
            "global_consumed": int(global_consumed),
            "version": int(version),
        }
        target = self.dir / f"snapshot_{version:06d}.json"
        _atomic_write(target, payload)
        _atomic_write(self.dir / "latest.json", payload)
        return target

    def write_result(self, sentence_id: str, result_dict: dict) -> None:
        _atomic_write(self.results_dir / f"{sentence_id}.json", result_dict)

    # ---- 读取 / 恢复 ----------------------------------------------------
    def latest(self) -> dict | None:
        latest = self.dir / "latest.json"
        if latest.exists():
            with open(latest, "r", encoding="utf-8") as fh:
                return json.load(fh)
        versions = sorted(self.dir.glob("snapshot_*.json"))
        if not versions:
            return None
        with open(versions[-1], "r", encoding="utf-8") as fh:
            return json.load(fh)

    def load_result(self, sentence_id: str) -> dict | None:
        path = self.results_dir / f"{sentence_id}.json"
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def resume_state(self) -> dict | None:
        """返回可恢复状态；哈希不符抛 E_CONFIG_MISMATCH 拒绝恢复。"""
        snap = self.latest()
        if snap is None:
            return None
        if snap.get("config_hash") != self.config_hash or \
                snap.get("model_hash") != self.model_hash:
            raise ConfigMismatchError(
                "快照配置哈希或模型哈希与当前输入不符，拒绝恢复",
                phase="snapshot")
        return snap

    def list_version_files(self) -> list[Path]:
        return sorted(self.dir.glob("snapshot_*.json"))
