"""阶段快照：临时文件 + 原子替换持久化，支持恢复与哈希校验。"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .errors import ConfigMismatch
from .hashing import fingerprint

STAGES = ["normalize", "intent", "slots", "rewrite"]


def snapshot_path(snapshot_dir: Path, stage: str) -> Path:
    return Path(snapshot_dir) / f"{stage}.json"


def write_snapshot(snapshot_dir: Path, stage: str, config_hash: str, injection_hash: str,
                   cursor: int, results: dict, failures: list) -> None:
    payload = {
        "stage": stage,
        "config_hash": config_hash,
        "injection_hash": injection_hash,
        "cursor": cursor,
        "results": results,
        "failures": failures,
        "result_fingerprint": fingerprint(results),
    }
    snapshot_dir = Path(snapshot_dir)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    tmp = snapshot_dir / f".{stage}.json.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(tmp, snapshot_path(snapshot_dir, stage))  # 原子替换


def read_snapshot(snapshot_dir: Path, stage: str, config_hash: str,
                  injection_hash: str) -> dict | None:
    path = snapshot_path(snapshot_dir, stage)
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (payload.get("stage") != stage
            or payload.get("config_hash") != config_hash
            or payload.get("injection_hash") != injection_hash):
        raise ConfigMismatch(
            f"snapshot {path} hash mismatch: stage={payload.get('stage')!r} "
            f"config={payload.get('config_hash')!r} injection={payload.get('injection_hash')!r}")
    return payload


def clear_snapshots(snapshot_dir: Path) -> None:
    snapshot_dir = Path(snapshot_dir)
    if not snapshot_dir.exists():
        return
    for stage in STAGES:
        path = snapshot_path(snapshot_dir, stage)
        if path.exists():
            path.unlink()
