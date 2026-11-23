"""算子 DAG 节点命名、缓存键构造与增量失效传播规划。

节点类型（每个 case / layer 是一条独立子 DAG）：
    q        确定性 Q 采样；
    kv       确定性 K/V 采样（随 kv_revision 变化）；
    angles   位置旋转角（随 RoPE 变体/base_theta/pos_scale 变化）；
    rope_q   旋转后 Q；
    rope_k   旋转后 K；
    score_b  第 b 个键分块的打分矩阵；
    stats_b  第 b 个分块的在线 softmax 统计量；
    output   分块路径写回输出；
    ref      不分块整段参考输出与整段分母。

节点 ID 形如 ``case_id/layer/score_b/3``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Sequence

from .attention import block_ranges
from .config import Task
from .errors import InconsistentPlanError

NODE_KINDS = (
    "q",
    "kv_k",
    "kv_v",
    "angles",
    "rope_q",
    "rope_k",
    "score",
    "stats",
    "output",
    "ref",
)


def node_id(case_id: str, layer: int, kind: str, block: int | None = None) -> str:
    if block is None:
        return f"{case_id}/{layer}/{kind}"
    return f"{case_id}/{layer}/{kind}_b/{block}"


def block_count(task: Task) -> int:
    return len(block_ranges(task.seq_len, task.block_size))


def case_node_ids(task: Task, layers: Sequence[int] | None = None) -> List[str]:
    """枚举一个 case（或指定层集合）的全部节点 ID。"""
    layer_indices = range(task.layers) if layers is None else layers
    ids: List[str] = []
    num_blocks = block_count(task)
    for layer in layer_indices:
        ids.append(node_id(task.case_id, layer, "q"))
        ids.append(node_id(task.case_id, layer, "kv_k"))
        ids.append(node_id(task.case_id, layer, "kv_v"))
        ids.append(node_id(task.case_id, layer, "angles"))
        ids.append(node_id(task.case_id, layer, "rope_q"))
        ids.append(node_id(task.case_id, layer, "rope_k"))
        for block in range(num_blocks):
            ids.append(node_id(task.case_id, layer, "score", block))
            ids.append(node_id(task.case_id, layer, "stats", block))
        ids.append(node_id(task.case_id, layer, "output"))
        ids.append(node_id(task.case_id, layer, "ref"))
    return ids


@dataclass
class CacheKey:
    """节点缓存键的显式组成，可序列化为 dict 供查询核对。"""

    node: str
    inputs: Dict[str, str]
    params: Dict[str, object]
    upstream: Dict[str, str]

    def to_dict(self) -> dict:
        return {
            "node": self.node,
            "inputs": self.inputs,
            "params": self.params,
            "upstream": self.upstream,
        }


def rope_params(task: Task) -> Dict[str, object]:
    return {
        "base_theta": task.base_theta,
        "pos_scale": task.pos_scale,
        "pos_encoding_variant": task.pos_encoding_variant,
        "head_dim": task.head_dim,
        "seq_len": task.seq_len,
    }


def attn_params(task: Task) -> Dict[str, object]:
    return {
        "block_size": task.block_size,
        "causal": task.causal,
        "kv_revision": task.kv_revision,
        "dtype": task.dtype,
    }


def build_cache_key(
    task: Task,
    layer: int,
    kind: str,
    block: int | None,
    input_digests: Dict[str, str],
    upstream_digests: Dict[str, str],
) -> CacheKey:
    """构造纳入全部输入摘要、上游哈希与语义参数的缓存键组成。"""
    node = node_id(task.case_id, layer, kind, block)
    params: Dict[str, object] = {"layer": layer, "case_id": task.case_id}
    if kind in ("angles", "rope_q", "rope_k"):
        params.update(rope_params(task))
    if kind in ("score", "stats", "output", "ref", "rope_k",
                "kv_k", "kv_v"):
        params.update(attn_params(task))
    if kind in ("score", "stats"):
        params["block_index"] = block
    return CacheKey(
        node=node,
        inputs=dict(sorted(input_digests.items())),
        params=dict(sorted(params.items())),
        upstream=dict(sorted(upstream_digests.items())),
    )


@dataclass
class InvalidationPlan:
    """一次增量更新的受影响节点划分。"""

    change_type: str
    affected: List[str]
    recompute: List[str]
    reused: List[str]
    reason: str = ""

    def verify(self) -> None:
        """校验重算集合与受影响节点一致，且与复用集合无交集。

        “并集覆盖全部受影响节点”的完整宇宙校验（重算 ∪ 复用 = 该
        case 全部 DAG 节点）在 :mod:`rope_attn.invalidate` 中执行，
        因为 block_size 变更会改变块节点的 ID 空间。
        """
        recompute_set = set(self.recompute)
        reused_set = set(self.reused)
        overlap = recompute_set & reused_set
        if overlap:
            raise InconsistentPlanError(
                f"重算集合与复用集合存在交集：{sorted(overlap)[:5]}",
                details={"overlap": sorted(overlap)},
            )
        if recompute_set != set(self.affected):
            missing = set(self.affected) - recompute_set
            extra = recompute_set - set(self.affected)
            raise InconsistentPlanError(
                "重算集合与受影响节点集合不一致",
                details={"missing": sorted(missing), "extra": sorted(extra)},
            )

    def to_dict(self) -> dict:
        return {
            "change_type": self.change_type,
            "reason": self.reason,
            "affected_count": len(self.affected),
            "recompute": self.recompute,
            "reused": self.reused,
        }


def _suffix_nodes(task: Task, layer: int) -> List[str]:
    num_blocks = block_count(task)
    ids: List[str] = []
    for block in range(num_blocks):
        ids.append(node_id(task.case_id, layer, "score", block))
        ids.append(node_id(task.case_id, layer, "stats", block))
    ids.append(node_id(task.case_id, layer, "output"))
    ids.append(node_id(task.case_id, layer, "ref"))
    return ids


def plan_kv_change(
    old_task: Task, new_task: Task, layer: int
) -> InvalidationPlan:
    """仅 K/V 内容变化（kv_revision 升高，block_size 不变）。

    受影响后缀：kv -> rope_k -> 各块 score/stats -> output/ref。
    q / angles / rope_q 全部复用。
    """
    if old_task.block_size != new_task.block_size:
        raise InconsistentPlanError(
            "plan_kv_change 要求 block_size 保持不变",
            case_id=new_task.case_id,
            layer=layer,
        )
    if new_task.kv_revision <= old_task.kv_revision:
        from .errors import RevisionRollbackError

        raise RevisionRollbackError(
            f"kv_revision 必须严格升高：{old_task.kv_revision} -> "
            f"{new_task.kv_revision}",
            field="kv_revision",
            case_id=new_task.case_id,
            layer=layer,
        )
    affected = [
        node_id(new_task.case_id, layer, "kv_k"),
        node_id(new_task.case_id, layer, "kv_v"),
    ]
    affected.append(node_id(new_task.case_id, layer, "rope_k"))
    affected.extend(_suffix_nodes(new_task, layer))
    reused = [
        node_id(new_task.case_id, layer, "q"),
        node_id(new_task.case_id, layer, "angles"),
        node_id(new_task.case_id, layer, "rope_q"),
    ]
    plan = InvalidationPlan(
        change_type="kv_content",
        affected=affected,
        recompute=affected,
        reused=reused,
        reason=(
            f"kv_revision {old_task.kv_revision}->{new_task.kv_revision}，"
            "仅 K/V 内容变化，分块边界不变"
        ),
    )
    plan.verify()
    return plan


def plan_block_size_change(
    old_task: Task, new_task: Task, layer: int
) -> InvalidationPlan:
    """仅 block_size 变化（kv_revision 不变）。

    分块边界变化导致 score/stats/output/ref 全部失效；上游
    q/kv/angles/rope_q/rope_k 全部复用。
    """
    if old_task.kv_revision != new_task.kv_revision:
        raise InconsistentPlanError(
            "plan_block_size_change 要求 kv_revision 保持不变",
            case_id=new_task.case_id,
            layer=layer,
        )
    if old_task.block_size == new_task.block_size:
        raise InconsistentPlanError(
            "block_size 未发生变化",
            case_id=new_task.case_id,
            layer=layer,
        )
    upstream = [
        node_id(new_task.case_id, layer, "q"),
        node_id(new_task.case_id, layer, "kv_k"),
        node_id(new_task.case_id, layer, "kv_v"),
        node_id(new_task.case_id, layer, "angles"),
        node_id(new_task.case_id, layer, "rope_q"),
        node_id(new_task.case_id, layer, "rope_k"),
    ]
    affected = _suffix_nodes(new_task, layer)
    plan = InvalidationPlan(
        change_type="block_size",
        affected=affected,
        recompute=affected,
        reused=upstream,
        reason=(
            f"block_size {old_task.block_size}->{new_task.block_size}，"
            "分块边界与全部下游节点失效，RoPE 上游全部复用"
        ),
    )
    plan.verify()
    return plan
