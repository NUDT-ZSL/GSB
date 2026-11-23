"""分层分块算子流水线执行器。

四个阶段（:data:`rope_attn.state.STAGES`）：
    angles    位置旋转角；
    rotate    Q/K 采样与半维配对旋转；
    score     分块打分、在线 softmax 归一化与整段对照；
    writeback 输出写回与摘要落地。

执行器在每个阶段边界把“全部层 × 全部 batch 完成”的结果原子提交为带
单调版本号、已完成键集合与校验和的快照；阶段内进度只写侧车文件，
强制终止后随未提交中间产物一起丢弃。
"""

from __future__ import annotations
import multiprocessing as mp
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .cache import ContentCache
from .config import RunConfig, Task
from .engine import run_worker
from .errors import ResourceLimitError
from .hashing import digest_iterable
from .nodes import case_node_ids, node_id
from .state import STAGES, Snapshot, SnapshotStore


@dataclass
class RunResult:
    status: str
    output_digest: str
    stage_counts: Dict[str, int]
    snapshots: List[dict]
    cache_hits: List[str]
    cache_misses: List[str]
    deviations: List[dict]
    denom_restore: List[dict]
    resource_degradations: List[str] = field(default_factory=list)
    resumed_from: Optional[dict] = None
    exit_report: Optional[dict] = None
    business_codes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "output_digest": self.output_digest,
            "stage_counts": self.stage_counts,
            "snapshots": self.snapshots,
            "cache_hits_count": len(self.cache_hits),
            "cache_misses_count": len(self.cache_misses),
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "deviations": self.deviations,
            "denom_restore": self.denom_restore,
            "resource_degradations": self.resource_degradations,
            "resumed_from": self.resumed_from,
            "exit_report": self.exit_report,
            "business_codes": self.business_codes,
        }


class Executor:
    def __init__(
        self,
        tasks: Sequence[Task],
        config: RunConfig,
        kill_after_stage: Optional[int] = None,
        interrupt_after_stage: Optional[int] = None,
    ) -> None:
        config.validate()
        self.tasks = list(tasks)
        self.config = config
        self.store = SnapshotStore(config.state_dir)
        self.cache = ContentCache(config.cache_dir)
        # 阶段提交后（从 1 计数）触发外部强杀 / KeyboardInterrupt 的钩子。
        self.kill_after_stage = kill_after_stage
        self.interrupt_after_stage = interrupt_after_stage
        self._interrupted = False
        self.degradations: List[str] = []

    # ---- 资源预算 ----

    def _estimate_block_bytes(self, task: Task) -> int:
        """单 worker 单键分块峰值驻留估算（score + prob + value 块）。"""
        block = min(task.block_size, task.seq_len)
        itemsize = np_dtype_size(task.dtype)
        score_bytes = task.seq_len * block * task.num_heads * 8
        value_bytes = block * task.num_heads * task.head_dim * itemsize
        output_bytes = (
            task.seq_len * task.num_heads * task.head_dim * 8
        )
        return score_bytes + value_bytes + output_bytes

    def _check_budget(self, parallel_nodes: int) -> int:
        worst_block = max(self._estimate_block_bytes(task) for task in self.tasks)
        if worst_block > self.config.max_block_bytes:
            if self.config.resource_policy == "error":
                raise ResourceLimitError(
                    f"分块驻留估算 {worst_block} 字节超过上限 "
                    f"{self.config.max_block_bytes}",
                    field="max_block_bytes",
                    details={"estimated_bytes": worst_block},
                )
            self.degradations.append(
                f"block_memory:{worst_block}->serial_blocks"
            )
        if parallel_nodes > self.config.max_parallel_nodes:
            if self.config.resource_policy == "error":
                raise ResourceLimitError(
                    f"并行重算节点 {parallel_nodes} 超过上限 "
                    f"{self.config.max_parallel_nodes}",
                    field="max_parallel_nodes",
                )
            self.degradations.append(
                f"parallel_nodes:{parallel_nodes}->"
                f"{self.config.max_parallel_nodes}"
            )
            parallel_nodes = self.config.max_parallel_nodes
        cache_entries = self.cache.entry_count()
        if cache_entries > self.config.max_cache_entries:
            if self.config.resource_policy == "error":
                raise ResourceLimitError(
                    f"缓存条目 {cache_entries} 超过上限 "
                    f"{self.config.max_cache_entries}",
                    field="max_cache_entries",
                    details={"entries": cache_entries},
                )
            self.degradations.append(
                f"cache_entries:{cache_entries}->read_only_reuse"
            )
        return min(parallel_nodes, self.config.max_parallel_nodes)

    # ---- 主执行入口 ----

    def run(self) -> RunResult:
        snapshot = self.store.latest()
        resumed_from = None
        start_stage = 0
        if snapshot is not None:
            start_stage = STAGES.index(snapshot.stage) + 1
            resumed_from = {
                "version": snapshot.version,
                "stage": snapshot.stage,
                "completed_keys": len(snapshot.completed_keys),
            }
            if self.config.resume_mode == "strict_stage":
                self.store.discard_progress()

        all_units = [
            (task, layer)
            for task in self.tasks
            for layer in range(task.layers)
        ]
        parallel_nodes = self._check_budget(len(all_units))
        workers = min(self.config.workers, parallel_nodes)

        stage_counts: Dict[str, int] = {stage: 0 for stage in STAGES}
        hits: List[str] = []
        misses: List[str] = []
        deviations: List[dict] = []
        denom_restore: List[dict] = []
        layer_results: List[dict] = []
        committed_snapshots: List[dict] = []

        try:
            for stage_index in range(start_stage, len(STAGES)):
                stage = STAGES[stage_index]
                pending = self._pending_units(stage, all_units, snapshot)
                stage_results = self._dispatch(stage, pending, workers)
                for result in stage_results:
                    hits.extend(result["hits"])
                    misses.extend(result["misses"])
                    deviations.append(
                        {
                            "case_id": result["case_id"],
                            "layer": result["layer"],
                            **result["deviation"],
                        }
                    )
                    denom_restore.append(
                        {
                            "case_id": result["case_id"],
                            "layer": result["layer"],
                            **result["denom_restore"],
                        }
                    )
                    layer_results.append(result)
                completed_keys = self._stage_completed_keys(stage)
                committed = self._commit_stage(stage, completed_keys)
                committed_snapshots.append(committed)
                stage_counts[stage] = len(completed_keys)
                self.store.discard_progress()
                self._after_commit_hook(stage_index + 1)
                if self._interrupted:
                    return self._interrupt_result(
                        committed_snapshots, stage_counts, hits, misses,
                        deviations, denom_restore, resumed_from,
                    )
        except KeyboardInterrupt:
            # KeyboardInterrupt 只在提交边界被承诺可优雅处理。
            return self._interrupt_result(
                committed_snapshots, stage_counts, hits, misses,
                deviations, denom_restore, resumed_from,
            )

        output_digest = self._output_digest_from_cache()
        # 汇总历史快照（含恢复前已提交的），供自检核对版本-阶段一一对应。
        all_snapshot_meta = [
            {"version": snap.version, "stage": snap.stage,
             "keys": len(snap.completed_keys)}
            for snap in self.store.all_snapshots()
        ]
        return RunResult(
            status="completed",
            output_digest=output_digest,
            stage_counts=stage_counts,
            snapshots=all_snapshot_meta,
            cache_hits=sorted(set(hits)),
            cache_misses=sorted(set(misses)),
            deviations=deviations,
            denom_restore=denom_restore,
            resource_degradations=self.degradations,
            resumed_from=resumed_from,
        )

    def _output_digest_from_cache(self) -> str:
        """从缓存中全部 output 节点计算稳定摘要。

        摘要只取决于落盘产物内容，因此中断恢复（部分阶段零重算）与
        不间断完整运行得到逐字节一致的结果。
        """
        entries = self.cache.all_node_entries()
        output_rows = []
        for task in self.tasks:
            for layer in range(task.layers):
                node = node_id(task.case_id, layer, "output")
                entry = entries.get(node)
                if entry is None:
                    continue
                output_rows.append(
                    (task.case_id, layer, entry.blob_hash)
                )
        return digest_iterable(sorted(output_rows))

    # ---- 阶段内调度 ----

    def _pending_units(
        self,
        stage: str,
        all_units: List[tuple],
        snapshot: Optional[Snapshot],
    ) -> List[tuple]:
        if self.config.resume_mode != "batch" or snapshot is None:
            return all_units
        progress = self.store.read_progress()
        done = set(progress.get(stage, []))
        return [
            unit
            for unit in all_units
            if f"{unit[0].case_id}:{unit[1]}" not in done
        ]

    def _dispatch(
        self,
        stage: str,
        units: List[tuple],
        workers: int,
    ) -> List[dict]:
        results: List[dict] = []
        progress = self.store.read_progress()
        done_units = set(progress.get(stage, []))
        payloads = [
            {
                "task": task.to_dict(),
                "config": self.config.__dict__,
                "layer": layer,
                "stage": stage,
            }
            for task, layer in units
        ]
        if workers <= 1 or len(payloads) <= 1:
            for payload in payloads:
                result = run_worker(payload)
                results.append(result)
                done_units.add(
                    f"{payload['task']['case_id']}:{payload['layer']}"
                )
                self.store.write_progress({stage: sorted(done_units)})
        else:
            ctx = mp.get_context("spawn")
            with ctx.Pool(processes=workers) as pool:
                for result in pool.imap_unordered(run_worker, payloads):
                    results.append(result)
                    done_units.add(f"{result['case_id']}:{result['layer']}")
                    self.store.write_progress({stage: sorted(done_units)})
        return results

    def _stage_completed_keys(self, stage: str) -> List[str]:
        keys: List[str] = []
        entries = self.cache.all_node_entries()
        stage_kinds = STAGE_NODE_KINDS[stage]
        for task in self.tasks:
            for node in case_node_ids(task):
                if _node_kind(node) in stage_kinds and node in entries:
                    keys.append(node)
        return sorted(keys)

    def _commit_stage(self, stage: str, completed_keys: List[str]) -> dict:
        version = self.store.next_version()
        snapshot = Snapshot(
            version=version,
            stage=stage,
            completed_keys=completed_keys,
            checksum=Snapshot.compute_checksum(stage, completed_keys),
        )
        self.store.commit(snapshot)
        return {
            "version": version,
            "stage": stage,
            "keys": len(completed_keys),
            "checksum": snapshot.checksum,
        }

    def _after_commit_hook(self, committed_stages: int) -> None:
        if (
            self.kill_after_stage is not None
            and committed_stages >= self.kill_after_stage
        ):
            # 模拟外部强制终止：不做任何 Python 清理，直接结束进程。
            import os
            import signal

            os.kill(os.getpid(), signal.SIGTERM)
            raise SystemExit(99)
        if (
            self.interrupt_after_stage is not None
            and committed_stages >= self.interrupt_after_stage
        ):
            self._interrupted = True

    def _interrupt_result(
        self,
        committed_snapshots: List[dict],
        stage_counts: Dict[str, int],
        hits: List[str],
        misses: List[str],
        deviations: List[dict],
        denom_restore: List[dict],
        resumed_from: Optional[dict],
    ) -> RunResult:
        exit_report = {
            "reason": "keyboard_interrupt",
            "graceful": True,
            "committed_snapshots": committed_snapshots,
            "message": "已在阶段提交边界优雅落盘，重启后基于快照继续",
        }
        return RunResult(
            status="interrupted",
            output_digest="",
            stage_counts=stage_counts,
            snapshots=committed_snapshots,
            cache_hits=sorted(set(hits)),
            cache_misses=sorted(set(misses)),
            deviations=deviations,
            denom_restore=denom_restore,
            resource_degradations=self.degradations,
            resumed_from=resumed_from,
            exit_report=exit_report,
            business_codes=["E_INTERRUPTED_GRACEFUL"],
        )


STAGE_NODE_KINDS = {
    "angles": {"angles"},
    "rotate": {"q", "kv_k", "kv_v", "rope_q", "rope_k"},
    "score": {"score", "stats", "ref"},
    "writeback": {"output"},
}


def _node_kind(node: str) -> str:
    # node 形如 case/layer/score_b/3 或 case/layer/rope_q
    kind_part = node.split("/")[2]
    return kind_part.split("_b")[0]


def np_dtype_size(dtype_name: str) -> int:
    import numpy as np

    return np.dtype(dtype_name).itemsize
