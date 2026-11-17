"""Reliable snapshot persistence, verification, and read-only backups."""

from __future__ import annotations

import copy
import json
import os
import shutil
from pathlib import Path
from typing import Any

from .canonical import atomic_write_bytes, atomic_write_json, digest_object
from .config import BatchConfig
from .errors import WorkbenchError


SNAPSHOT_SCHEMA = "infer-workbench.snapshot.v1"


def seal_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    sealed = copy.deepcopy(payload)
    sealed.pop("snapshot_digest", None)
    sealed["snapshot_digest"] = digest_object(sealed)
    return sealed


def snapshot_digest(payload: dict[str, Any]) -> str:
    content = copy.deepcopy(payload)
    content.pop("snapshot_digest", None)
    return digest_object(content)


def save_snapshot(path: str | Path, payload: dict[str, Any]) -> Path:
    sealed = seal_snapshot(payload)
    return atomic_write_json(path, sealed)


def load_snapshot(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        with source.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError as exc:
        raise WorkbenchError("E_SNAPSHOT", f"无法读取快照: {source}", {"path": source.as_posix()}) from exc
    except json.JSONDecodeError as exc:
        raise WorkbenchError(
            "E_SNAPSHOT",
            f"快照 JSON 解析失败: {source}",
            {"path": source.as_posix(), "line": exc.lineno, "column": exc.colno},
        ) from exc
    if not isinstance(payload, dict):
        raise WorkbenchError("E_SNAPSHOT", "快照顶层必须为对象", {"path": source.as_posix()})
    if payload.get("schema") != SNAPSHOT_SCHEMA:
        raise WorkbenchError("E_SNAPSHOT", "快照 schema 不兼容", {"schema": payload.get("schema")})
    recorded = payload.get("snapshot_digest")
    actual = snapshot_digest(payload)
    if not isinstance(recorded, str) or recorded != actual:
        raise WorkbenchError(
            "E_SNAPSHOT",
            "快照摘要不一致，文件已损坏或被修改",
            {"path": source.as_posix(), "recorded": recorded, "actual": actual},
        )
    try:
        config = BatchConfig.from_dict(payload.get("config"))
    except WorkbenchError as exc:
        raise WorkbenchError(
            "E_SNAPSHOT", "快照内配置非法", {"cause": exc.to_dict()}
        ) from exc
    if config.config_version != payload.get("config_version"):
        raise WorkbenchError(
            "E_SNAPSHOT",
            "快照配置版本与配置内容不一致",
            {
                "recorded": payload.get("config_version"),
                "recomputed": config.config_version,
            },
        )
    batches = payload.get("completed_batches")
    plans = payload.get("batch_plan")
    if not isinstance(batches, list) or not isinstance(plans, list):
        raise WorkbenchError("E_SNAPSHOT", "快照缺少批计划或已完成批次")
    for expected_index, batch in enumerate(batches, start=1):
        if not isinstance(batch, dict) or batch.get("batch_index") != expected_index:
            raise WorkbenchError(
                "E_SNAPSHOT",
                "快照已完成批次必须从批次 1 开始连续",
                {"expected_batch_index": expected_index},
            )
        if batch.get("config_version") != config.config_version:
            raise WorkbenchError(
                "E_SNAPSHOT",
                "快照批次配置版本不一致",
                {"batch_index": expected_index},
            )
        if batch.get("status") != "complete":
            raise WorkbenchError(
                "E_SNAPSHOT", "快照中存在未完成批次", {"batch_index": expected_index}
            )
    next_batch_index = payload.get("next_batch_index")
    if next_batch_index != len(batches) + 1:
        raise WorkbenchError(
            "E_SNAPSHOT",
            "快照 next_batch_index 与已完成批次数量不一致",
            {"next_batch_index": next_batch_index, "expected": len(batches) + 1},
        )
    return payload


def preserve_readonly_backup(snapshot_path: str | Path, backup_dir: str | Path) -> Path:
    source = Path(snapshot_path)
    destination_dir = Path(backup_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    payload = load_snapshot(source)
    digest = str(payload["snapshot_digest"])
    destination = destination_dir / f"{source.stem}.{digest[:12]}.readonly.json"
    source_bytes = source.read_bytes()
    if destination.exists():
        if destination.read_bytes() != source_bytes:
            raise WorkbenchError(
                "E_SNAPSHOT",
                "只读备份路径已存在但内容不同",
                {"path": destination.as_posix()},
            )
    else:
        atomic_write_bytes(destination, source_bytes)
    try:
        os.chmod(destination, 0o444)
    except OSError:
        pass
    return destination


def copy_snapshot(src: str | Path, dst: str | Path) -> Path:
    target = Path(dst)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, target)
    return target
