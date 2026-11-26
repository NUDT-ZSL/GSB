"""阶段状态机与中断恢复检查点。"""

import hashlib
import json
import os

from .clock import LogicalClock
from .concurrency import CommitGate
from .model import Stage, STAGE_ORDER


def _canonical(payload):
    return json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False)


def _atomic_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def checkpoint_checksum(payload):
    blob = _canonical(payload).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


class CheckpointStore:
    """持久化检查点存储，提交带单调版本号与内容校验和。"""

    def __init__(self, directory, clock=None):
        self.directory = directory
        os.makedirs(directory, exist_ok=True)
        self.clock = clock or LogicalClock()
        self.gate = CommitGate()
        self.scratch_dir = os.path.join(directory, "scratch")
        os.makedirs(self.scratch_dir, exist_ok=True)

    def _path(self, version):
        return os.path.join(self.directory, f"checkpoint_{version:06d}.json")

    def _latest_path(self):
        return os.path.join(self.directory, "latest.json")

    def commit(self, owner, stage, completed_task_ids, ledger_checksums,
               extra=None):
        """在阶段边界原子提交检查点，返回检查点载荷。"""
        with self.gate.lease(owner):
            prev = self.latest()
            version = 1 if prev is None else prev["version"] + 1
            completed = sorted(set(completed_task_ids))
            body = {
                "version": version,
                "stage": stage.value if isinstance(stage, Stage) else stage,
                "committed_at_ms": self.clock.tick(),
                "completed_task_ids": completed,
                "ledger_checksums": dict(sorted(ledger_checksums.items())),
                "extra": extra or {},
            }
            body["content_checksum"] = checkpoint_checksum(body)
            _atomic_write(self._path(version), _canonical(body))
            _atomic_write(self._latest_path(), _canonical(body))
            self.invalidate_scratch()
            return body

    def latest(self):
        path = self._latest_path()
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def list_versions(self):
        out = []
        for name in sorted(os.listdir(self.directory)):
            if name.startswith("checkpoint_") and name.endswith(".json"):
                with open(os.path.join(self.directory, name), encoding="utf-8") as h:
                    out.append(json.load(h))
        return sorted(out, key=lambda p: p["version"])

    def scratch_path(self, name):
        """未提交的中间变换/账本条目的暂存路径（重启后作废）。"""
        return os.path.join(self.scratch_dir, name)

    def invalidate_scratch(self):
        """提交后使所有未提交中间产物作废。"""
        if not os.path.isdir(self.scratch_dir):
            return
        for name in os.listdir(self.scratch_dir):
            os.remove(os.path.join(self.scratch_dir, name))


class RecoveryArbiter:
    """恢复裁决：从最近已提交检查点继续，已提交任务不重算。"""

    def __init__(self, store):
        self.store = store

    def decide(self, planned_task_ids):
        checkpoint = self.store.latest()
        if checkpoint is None:
            return {
                "resume": False,
                "from_version": 0,
                "stage": Stage.INITIAL_VALIDATION.value,
                "committed": [],
                "to_process": list(planned_task_ids),
                "voided_scratch": True,
            }
        committed = set(checkpoint["completed_task_ids"])
        planned = list(planned_task_ids)
        to_process = [tid for tid in planned if tid not in committed]
        # 未提交的中间变换与账本条目标记作废。
        self.store.invalidate_scratch()
        return {
            "resume": True,
            "from_version": checkpoint["version"],
            "stage": checkpoint["stage"],
            "committed": sorted(committed),
            "to_process": to_process,
            "voided_scratch": True,
            "content_checksum": checkpoint["content_checksum"],
        }

    @staticmethod
    def next_stage(stage):
        idx = STAGE_ORDER.index(stage if isinstance(stage, Stage)
                                else Stage(stage))
        if idx == len(STAGE_ORDER) - 1:
            return None
        return STAGE_ORDER[idx + 1]
