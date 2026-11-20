"""调度计划、预算账本、执行摘要的版本化快照。

每个快照类别一个文件，载荷携带单调递增版本号与内容校验和：

* 保存时先写临时文件再原子 ``os.replace``，任何时刻读到的都是完整版本；
* 检测到版本号或校验和变化时整体替换内存态，绝不做增量修补；
* 已完成的更高版本不得被旧版本覆盖，回退尝试抛
  :class:`ESnapshotRollback`；
* 文件损坏、JSON 非法或校验和不符抛 :class:`ESnapshotCorrupt`，
  错误上下文指明快照类别与版本号。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .determinism import canonical_json, stable_hash
from .errors import ESnapshotCorrupt, ESnapshotRollback

PLAN_KIND = "plan"
LEDGER_KIND = "ledger"
SUMMARY_KIND = "summary"
SNAPSHOT_KINDS = (PLAN_KIND, LEDGER_KIND, SUMMARY_KIND)


def to_jsonable(value: Any) -> Any:
    """把 dataclass / tuple / numpy 标量递归转换为 JSON 原生类型。"""
    if hasattr(value, "__dataclass_fields__"):
        return {
            key: to_jsonable(getattr(value, key))
            for key in sorted(value.__dataclass_fields__)
        }
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, (int, str, bool)) or value is None:
        return value
    if isinstance(value, float):
        return value
    # numpy 标量等兜底转换。
    return to_jsonable(value.item() if hasattr(value, "item") else repr(value))


@dataclass
class SnapshotEnvelope:
    kind: str
    version: int
    checksum: str
    payload: dict


class SnapshotStore:
    """三类快照的本地文件存储，目录内整体替换、历史版本另存。"""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.history_dir = self.directory / "history"
        self.history_dir.mkdir(parents=True, exist_ok=True)
        self._memory: dict[str, SnapshotEnvelope] = {}

    def _path(self, kind: str) -> Path:
        return self.directory / f"{kind}.snapshot.json"

    def save(
        self,
        kind: str,
        payload: Any,
        expected_version: int | None = None,
        candidate_version: int | None = None,
    ) -> SnapshotEnvelope:
        """整体写入新版本。

        ``expected_version`` 给出内存中当前应有的基础版本（乐观并发校验）；
        ``candidate_version`` 显式声明待写入版本，若不高于当前版本则按
        回退尝试处理。默认候选版本为当前版本 + 1，保证单调递增。
        """
        if kind not in SNAPSHOT_KINDS:
            raise ValueError(f"未知快照类别: {kind}")
        clean_payload = to_jsonable(payload)
        current = self._memory.get(kind)
        next_version = candidate_version if candidate_version is not None else (
            (current.version + 1) if current else 1
        )
        if expected_version is not None and current is not None and expected_version != current.version:
            raise ESnapshotRollback(
                f"快照 {kind} 版本冲突：期望基于 {expected_version}，当前为 {current.version}",
                kind=kind,
                expected_version=expected_version,
                current_version=current.version,
            )
        if current is not None and next_version <= current.version:
            raise ESnapshotRollback(
                f"快照 {kind} 拒绝回退：{next_version} <= {current.version}",
                kind=kind,
                candidate_version=next_version,
                current_version=current.version,
            )
        checksum = stable_hash(clean_payload)
        envelope = SnapshotEnvelope(
            kind=kind,
            version=next_version,
            checksum=checksum,
            payload=clean_payload,
        )
        target = self._path(kind)
        tmp = target.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                {
                    "kind": kind,
                    "version": next_version,
                    "checksum": checksum,
                    "payload": clean_payload,
                },
                handle,
                sort_keys=True,
                ensure_ascii=False,
                indent=2,
            )
        os.replace(tmp, target)
        if current is not None:
            history_path = self.history_dir / f"{kind}-v{current.version}.json"
            with history_path.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(
                    {
                        "kind": current.kind,
                        "version": current.version,
                        "checksum": current.checksum,
                        "payload": current.payload,
                    },
                    handle,
                    sort_keys=True,
                    ensure_ascii=False,
                )
        # 整体替换内存态，不保留任何旧版本字段的增量残留。
        self._memory[kind] = envelope
        return envelope

    def load(self, kind: str) -> SnapshotEnvelope | None:
        """从磁盘加载并校验；识别版本变化后整体替换内存态。"""
        path = self._path(kind)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            version = int(raw["version"])
            disk_checksum = str(raw["checksum"])
            payload = raw["payload"]
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            raise ESnapshotCorrupt(
                f"快照 {kind} 结构损坏，无法解析",
                kind=kind,
                version="unknown",
            ) from exc
        actual_checksum = stable_hash(payload)
        if actual_checksum != disk_checksum:
            raise ESnapshotCorrupt(
                f"快照 {kind} 版本 {version} 校验和不符",
                kind=kind,
                version=version,
                expected_checksum=disk_checksum,
                actual_checksum=actual_checksum,
            )
        envelope = SnapshotEnvelope(kind, version, disk_checksum, payload)
        previous = self._memory.get(kind)
        if previous is None or previous.version != version or previous.checksum != disk_checksum:
            # 版本号或校验和变化 → 整体替换。
            self._memory[kind] = envelope
        return envelope

    def current(self, kind: str) -> SnapshotEnvelope | None:
        return self._memory.get(kind)

    def versions(self) -> dict[str, int]:
        return {kind: env.version for kind, env in sorted(self._memory.items())}

    def assert_no_rollback(self, kind: str, candidate_version: int) -> None:
        current = self._memory.get(kind)
        if current is not None and candidate_version <= current.version:
            raise ESnapshotRollback(
                f"快照 {kind} 拒绝回退：{candidate_version} <= {current.version}",
                kind=kind,
                candidate_version=candidate_version,
                current_version=current.version,
            )

    def canonical_payload(self, kind: str) -> str:
        """供测试与自检使用的规范化载荷字符串。"""
        envelope = self._memory[kind]
        return canonical_json(envelope.payload)
