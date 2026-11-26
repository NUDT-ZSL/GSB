"""有向无环计算图编排：校验 -> 变换 -> 归约 -> 提交 -> 恢复裁决。"""

import json
import os

import numpy as np

from .cache import CommitCache, content_key
from .checkpoints import CheckpointStore, RecoveryArbiter
from .clock import LogicalClock
from .consistency import verify_partition
from .ingestion import ingest
from .invalidation import plan_invalidation
from .ledger import assert_within_tolerance, build_ledger
from .memory import MemoryBudget
from .model import Stage


def _canonical(payload):
    return json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False)


def _atomic_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


class DiagnosticPipeline:
    """状态演进与恢复一致性诊断的主入口。"""

    def __init__(self, work_dir, state_dim, config, clock=None,
                 memory_limit_bytes=None, raise_on_tolerance=True):
        self.work_dir = work_dir
        self.state_dim = state_dim
        self.config = config
        self.clock = clock or LogicalClock()
        self.memory = (
            MemoryBudget(memory_limit_bytes)
            if memory_limit_bytes is not None
            else None
        )
        self.raise_on_tolerance = raise_on_tolerance
        os.makedirs(work_dir, exist_ok=True)
        self.state_dir = os.path.join(work_dir, "states")
        self.ledger_dir = os.path.join(work_dir, "ledgers")
        os.makedirs(self.state_dir, exist_ok=True)
        os.makedirs(self.ledger_dir, exist_ok=True)
        self.store = CheckpointStore(os.path.join(work_dir, "checkpoints"),
                                     clock=self.clock)
        self.cache = CommitCache()
        self.arbiter = RecoveryArbiter(self.store)

    def _state_path(self, task_id):
        return os.path.join(self.state_dir, f"{task_id}.npy")

    def _ledger_path(self, task_id):
        return os.path.join(self.ledger_dir, f"{task_id}.json")

    def _write_ledger(self, task_id, ledger):
        _atomic_write(self._ledger_path(task_id), _canonical(ledger))

    def run(self, jsonl_path, fail_after_task=None):
        """执行完整 DAG。fail_after_task 注入中断：处理完该任务后崩溃。"""
        records = ingest(jsonl_path, self.state_dim)
        planned_ids = [r["task"].task_id for r in records]
        decision = self.arbiter.decide(planned_ids)
        committed = set(decision["committed"])
        pending_at_resume = list(decision["to_process"])

        ledgers = {}
        processed = []
        for record in records:
            task = record["task"]
            if task.task_id in committed:
                # 已提交任务不得重算或重复计入。
                continue
            key = content_key(task, self.config)
            cached = self.cache.get(key)
            if cached is not None:
                ledger = cached["ledger"]
            else:
                ledger, payloads = build_ledger(
                    task, record["initial_state"], self.config,
                    budget=self.memory,
                )
                if self.raise_on_tolerance:
                    assert_within_tolerance(ledger)
                np.save(self._state_path(task.task_id), payloads["final_state"])
                self._write_ledger(task.task_id, ledger)
                self.cache.put(
                    key,
                    ledger["final_checksum"],
                    ledger,
                    payloads["reduction"],
                )
            ledgers[task.task_id] = ledger
            processed.append(task.task_id)
            # 阶段边界提交检查点（含已完成集合与内容校验和）。
            self.store.commit(
                owner=f"pipeline:{task.task_id}",
                stage=Stage.SNAPSHOT_COMMIT,
                completed_task_ids=committed | set(processed),
                ledger_checksums={
                    tid: ledgers[tid]["final_checksum"] for tid in processed
                },
                extra={"precision": self.config.precision,
                       "algebra": self.config.algebra},
            )
            if fail_after_task == task.task_id:
                raise _Interrupt(fail_after_task)

        final_decision = self.arbiter.decide(planned_ids)
        final_decision["pending_at_resume"] = pending_at_resume
        return {
            "task_ids": planned_ids,
            "processed": processed,
            "reused_from_checkpoint": sorted(committed),
            "ledgers": ledgers,
            "recovery": final_decision,
            "memory_high_water": (
                self.memory.high_water_bytes if self.memory else None
            ),
        }

    def run_incremental(self, old_jsonl_path, new_jsonl_path, old_config,
                        new_config):
        """任务修订/配置换版后的增量失效闭包执行。"""
        old_records = ingest(old_jsonl_path, self.state_dim)
        new_records = ingest(new_jsonl_path, self.state_dim)
        old_by_id = {r["task"].task_id: r["task"] for r in old_records}
        ordered = [r["task"] for r in new_records]
        committed_keys = {
            entry["key"] for entry in self.cache._entries.values()
        }
        change_set = plan_invalidation(
            ordered, old_by_id, old_config, new_config, committed_keys
        )
        all_ids = [t.task_id for t in ordered]
        verify_partition(change_set, all_ids)

        ledgers = {}
        recomputed, reused = [], []
        for record in new_records:
            task = record["task"]
            key = content_key(task, new_config)
            if task.task_id in change_set.reuse_task_ids and self.cache.has(key):
                reused.append(task.task_id)
                ledgers[task.task_id] = self.cache.get(key)["ledger"]
                continue
            ledger, payloads = build_ledger(task, record["initial_state"],
                                            new_config, budget=self.memory)
            if self.raise_on_tolerance:
                assert_within_tolerance(ledger)
            np.save(self._state_path(task.task_id), payloads["final_state"])
            self._write_ledger(task.task_id, ledger)
            self.cache.put(key, ledger["final_checksum"], ledger,
                           payloads["reduction"])
            ledgers[task.task_id] = ledger
            recomputed.append(task.task_id)
        self.store.commit(
            owner="pipeline:incremental",
            stage=Stage.RECOVERY_ARBITRATION,
            completed_task_ids=all_ids,
            ledger_checksums={
                tid: ledgers[tid]["final_checksum"] for tid in all_ids
            },
            extra={"change_kind": change_set.kind},
        )
        return {
            "change_set": change_set.to_dict(),
            "recomputed": recomputed,
            "reused": reused,
            "ledgers": ledgers,
        }

    def ledger_bytes(self, task_id):
        with open(self._ledger_path(task_id), "rb") as handle:
            return handle.read()


class _Interrupt(Exception):
    """内部中断信号，模拟进程在阶段边界后崩溃。"""

    def __init__(self, task_id):
        super().__init__(f"injected interrupt after {task_id}")
        self.task_id = task_id
