"""跨模块一致性自检。

校验四项：
    1. 阶段快照版本号与阶段名一一对应、校验和自洽；
    2. 缓存键哈希与产物内容哈希互相可核对（重算 blob 内容哈希）；
    3. 各分块统计量可合并还原整段 softmax 分母（容差内）；
    4. 增量计划的重算/复用集合无交集且并集覆盖全部受影响节点。
"""

from __future__ import annotations

from typing import Optional, Sequence

import numpy as np

from . import attention as attn
from .cache import ContentCache
from .config import Task
from .nodes import InvalidationPlan
from .state import STAGES, SnapshotStore


def run_self_check(
    tasks: Sequence[Task],
    cache_dir: str,
    state_dir: str,
    tolerance: float = 1.0e-12,
    plan: Optional[InvalidationPlan] = None,
) -> dict:
    report = {
        "snapshot_version_stage": _check_snapshots(state_dir),
        "cache_key_blob": _check_cache(cache_dir),
        "denominator_restore": _check_denominator(tasks, cache_dir, tolerance),
        "invalidation_partition": _check_plan(plan),
    }
    report["ok"] = all(
        item["ok"] for item in report.values() if isinstance(item, dict)
    )
    return report


def _check_snapshots(state_dir: str) -> dict:
    store = SnapshotStore(state_dir)
    snapshots = store.all_snapshots()
    failures = []
    for index, snapshot in enumerate(snapshots, start=1):
        expected_stage = STAGES[(snapshot.version - 1) % len(STAGES)]
        if snapshot.version != index:
            failures.append(
                f"版本号不连续：期望 {index}，实际 {snapshot.version}"
            )
        if snapshot.stage != expected_stage:
            failures.append(
                f"版本 {snapshot.version} 阶段应为 {expected_stage}，"
                f"实际 {snapshot.stage}"
            )
        if not snapshot.verify():
            failures.append(f"版本 {snapshot.version} 校验和不自洽")
    return {"ok": not failures, "failures": failures,
            "snapshot_count": len(snapshots)}


def _check_cache(cache_dir: str) -> dict:
    cache = ContentCache(cache_dir)
    failures = []
    checked = 0
    for entry in cache.entries():
        result = cache.verify_entry(entry)
        checked += 1
        if not result["content_ok"]:
            failures.append(f"节点 {entry.node} 产物哈希与 manifest 不符")
        # 反查 key_hash：manifest 键必须等于键组成的规范化哈希。
        if cache.key_hash_of(
            _rebuild_cache_key(entry.key_payload)
        ) != entry.key_hash:
            failures.append(f"节点 {entry.node} 缓存键重算不一致")
    return {"ok": not failures, "failures": failures, "entries_checked": checked}


def _rebuild_cache_key(payload: dict):
    from .nodes import CacheKey

    return CacheKey(
        node=payload["node"],
        inputs=payload["inputs"],
        params=payload["params"],
        upstream=payload["upstream"],
    )


def _check_denominator(
    tasks: Sequence[Task], cache_dir: str, tolerance: float
) -> dict:
    cache = ContentCache(cache_dir)
    generations: dict = {}
    for entry in cache.entries():
        generations.setdefault(entry.node, []).append(entry)
    failures = []
    checked = 0
    for task in tasks:
        for layer in range(task.layers):
            ref_node = node_ref(task.case_id, layer)
            if ref_node not in generations:
                continue
            ref_entry = generations[ref_node][-1]
            ref_packed = cache.get_array(ref_entry)
            ref_denom = ref_packed[..., -1]
            ref_revision = int(
                ref_entry.key_payload["params"].get("kv_revision", 1)
            )
            block_stats = []
            prefix = f"{task.case_id}/{layer}/stats_b/"
            stats_nodes = sorted(
                (node for node in generations if node.startswith(prefix)),
                key=lambda node: int(node.rsplit("/", 1)[1]),
            )
            # 同节点 ID 可能保留多代（KV 修订 / block_size 变更），
            # 按条目携带的 block_size 分组并保留每块的全部代。
            block_groups = {}
            for stats_node in stats_nodes:
                for entry in generations[stats_node]:
                    if int(
                        entry.key_payload["params"].get("kv_revision", 1)
                    ) != ref_revision:
                        continue
                    block_size = int(
                        entry.key_payload["params"]["block_size"]
                    )
                    block_groups.setdefault(block_size, {})[
                        int(stats_node.rsplit("/", 1)[1])
                    ] = entry
            chosen_nodes = []
            chosen_block_size = task.block_size
            for block_size, node_map in block_groups.items():
                ranges = attn.block_ranges(task.seq_len, block_size)
                if len(node_map) >= len(ranges):
                    chosen_nodes = [
                        node_map[index].node
                        for index in range(len(ranges))
                    ]
                    chosen_block_size = block_size
                    break
            if not chosen_nodes:
                continue
            ranges = attn.block_ranges(task.seq_len, chosen_block_size)
            for block_index, (k_start, k_end) in enumerate(ranges):
                stats_node = chosen_nodes[block_index]
                stats_array = cache.get_array(
                    block_groups[chosen_block_size][block_index]
                )
                block_stats.append(
                    attn.BlockStats(
                        block_index=block_index,
                        q_start=0,
                        q_end=task.seq_len,
                        k_start=k_start,
                        k_end=k_end,
                        block_max=stats_array[0],
                        block_denom=stats_array[1],
                    )
                )
            if not block_stats:
                continue
            # 边界必须恰好覆盖整段，否则该层不具备可还原前提。
            if block_stats[-1].k_end != task.seq_len:
                failures.append(
                    f"{task.case_id} 层 {layer} 分块边界未覆盖整段序列"
                )
                continue
            _restored_max, restored = attn.merged_full_denom(block_stats)
            error = attn.denominator_restore_error(restored, ref_denom)
            checked += 1
            if float(np.max(error)) > tolerance:
                failures.append(
                    f"{task.case_id} 层 {layer} 分母还原偏差 "
                    f"{float(np.max(error)):.3e} 超过 {tolerance:.0e}"
                )
    return {"ok": not failures, "failures": failures, "layers_checked": checked}


def _check_plan(plan: Optional[InvalidationPlan]) -> dict:
    if plan is None:
        return {"ok": True, "failures": [], "skipped": True}
    try:
        plan.verify()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "failures": [str(exc)]}
    return {
        "ok": True,
        "failures": [],
        "recompute_count": len(plan.recompute),
        "reused_count": len(plan.reused),
    }


def node_ref(case_id: str, layer: int) -> str:
    return f"{case_id}/{layer}/ref"
