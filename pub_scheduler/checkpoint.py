"""Versioned, checksum-protected scheduler checkpoints."""

from __future__ import annotations

import copy
import hmac
import json
import os
import tempfile
from typing import Any

from .engine import SchedulerEngine
from .model import (
    SCHEMA_VERSION,
    ValidationError,
    digest_value,
    validate_workload,
    workload_digest,
)

CHECKPOINT_FORMAT = "publication-scheduler-checkpoint"
CHECKPOINT_VERSION = "1.0"


class CheckpointError(ValueError):
    """Raised when checkpoint version, digest, or state is invalid."""


def build_checkpoint(engine: SchedulerEngine) -> dict[str, Any]:
    content = {
        "checkpoint_version": CHECKPOINT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "workload_hash": engine.workload_hash,
        "workload": copy.deepcopy(engine.workload),
        "state": engine.export_state(),
    }
    return {
        "format": CHECKPOINT_FORMAT,
        "checkpoint_version": CHECKPOINT_VERSION,
        "content_sha256": digest_value(content),
        "content": content,
    }


def save_checkpoint(engine: SchedulerEngine, path: str) -> dict[str, Any]:
    envelope = build_checkpoint(engine)
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    file_descriptor, temporary_path = tempfile.mkstemp(
        prefix=".checkpoint-", suffix=".json", dir=directory, text=True
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            json.dump(envelope, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except Exception:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)
        raise
    return envelope


def parse_checkpoint(payload: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(payload, dict):
        raise CheckpointError("检查点根节点必须是 JSON 对象")
    if payload.get("format") != CHECKPOINT_FORMAT:
        raise CheckpointError("检查点 format 标识不匹配")
    if payload.get("checkpoint_version") != CHECKPOINT_VERSION:
        raise CheckpointError(
            f"不支持检查点版本 {payload.get('checkpoint_version')!r}"
        )
    content = payload.get("content")
    if not isinstance(content, dict):
        raise CheckpointError("检查点 content 必须是 JSON 对象")
    if content.get("checkpoint_version") != CHECKPOINT_VERSION:
        raise CheckpointError("content.checkpoint_version 不匹配")
    expected_digest = payload.get("content_sha256")
    if not isinstance(expected_digest, str):
        raise CheckpointError("检查点缺少 content_sha256")
    actual_digest = digest_value(content)
    if not hmac.compare_digest(expected_digest, actual_digest):
        raise CheckpointError("检查点内容摘要校验失败")
    if content.get("schema_version") != SCHEMA_VERSION:
        raise CheckpointError("检查点 schema_version 不匹配")
    workload = content.get("workload")
    if not isinstance(workload, dict):
        raise CheckpointError("检查点缺少 workload")
    try:
        validated_workload = validate_workload(workload)
    except ValidationError as error:
        raise CheckpointError(f"检查点作业工程无效: {error}") from error
    actual_workload_hash = workload_digest(validated_workload)
    if content.get("workload_hash") != actual_workload_hash:
        raise CheckpointError("检查点 workload_hash 校验失败")
    state = content.get("state")
    if not isinstance(state, dict):
        raise CheckpointError("检查点缺少 state")
    return content, {
        "format": CHECKPOINT_FORMAT,
        "checkpoint_version": CHECKPOINT_VERSION,
        "content_sha256": actual_digest,
        "workload_hash": actual_workload_hash,
        "state_time": state.get("current_time"),
        "event_index": state.get("event_index"),
    }


def load_checkpoint(path: str) -> SchedulerEngine:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise CheckpointError(f"无法读取检查点: {error}") from error
    content, metadata = parse_checkpoint(payload)
    try:
        engine = SchedulerEngine.from_checkpoint_content(
            content["workload"], content["state"]
        )
    except (ValueError, KeyError, TypeError, RuntimeError) as error:
        raise CheckpointError(f"检查点状态无法恢复: {error}") from error
    if engine.workload_hash != content["workload_hash"]:
        raise CheckpointError("恢复后作业工程摘要不一致")
    if not (0 <= engine.event_index <= len(engine.events)):
        raise CheckpointError("检查点 event_index 越界")
    metadata["path"] = os.path.abspath(path)
    engine.checkpoint_metadata = metadata
    return engine

