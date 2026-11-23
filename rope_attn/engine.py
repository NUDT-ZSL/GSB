"""DAG 节点的实际计算逻辑（worker 可调用的顶层函数）。

每个节点先按 :mod:`rope_attn.nodes` 的规则构造缓存键，再经内容寻址缓存
获取或计算，因此恢复执行时已提交阶段的节点全部命中缓存、不会重算。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np

from . import attention as attn
from . import rope as rope_mod
from . import sampling
from .cache import ContentCache
from .config import RunConfig, Task
from .errors import NumericUnstableError, WorkerFailure
from .hashing import digest_array, digest_obj
from .nodes import (
    block_count,
    build_cache_key,
    node_id,
)


@dataclass
class EngineContext:
    task: Task
    config: RunConfig
    cache: ContentCache
    hits: List[str] = field(default_factory=list)
    misses: List[str] = field(default_factory=list)

    def record(self, node: str, cache_hit: bool) -> None:
        (self.hits if cache_hit else self.misses).append(node)


def _get_or_compute(
    ctx: EngineContext,
    layer: int,
    kind: str,
    block: Optional[int],
    input_digests: Dict[str, str],
    upstream_digests: Dict[str, str],
    compute,
) -> np.ndarray:
    cache_key = build_cache_key(
        ctx.task, layer, kind, block, input_digests, upstream_digests
    )
    array, _entry, cache_hit = ctx.cache.get_or_compute(cache_key, compute)
    ctx.record(cache_key.node, cache_hit)
    return array


def compute_q(ctx: EngineContext, layer: int) -> np.ndarray:
    task = ctx.task
    return _get_or_compute(
        ctx,
        layer,
        "q",
        None,
        {"seed": digest_obj(task.seed), "shape": digest_obj(
            [task.seq_len, task.num_heads, task.head_dim]
        )},
        {},
        lambda: sampling.generate_q(task, layer),
    )


def compute_kv(ctx: EngineContext, layer: int):
    task = ctx.task

    def make_key() -> np.ndarray:
        return sampling.generate_kv(task, layer, task.kv_revision)[0]

    def make_value() -> np.ndarray:
        return sampling.generate_kv(task, layer, task.kv_revision)[1]

    revision_digest = digest_obj(
        [task.seed, layer, task.kv_revision, task.dtype]
    )
    key = _get_or_compute(
        ctx, layer, "kv_k", None,
        {"kv_revision": revision_digest, "role": digest_obj("K")}, {}, make_key
    )
    value = _get_or_compute(
        ctx, layer, "kv_v", None,
        {"kv_revision": revision_digest, "role": digest_obj("V")}, {}, make_value
    )
    return key, value


def compute_angles(ctx: EngineContext, layer: int) -> np.ndarray:
    task = ctx.task
    spec = rope_mod.RopeSpec.from_task(task)
    return _get_or_compute(
        ctx,
        layer,
        "angles",
        None,
        {"rope_spec": digest_obj(spec.__dict__)},
        {},
        lambda: rope_mod.compute_angles(spec),
    )


def compute_rope(
    ctx: EngineContext,
    layer: int,
    x: np.ndarray,
    angles: np.ndarray,
    kind: str,
) -> np.ndarray:
    return _get_or_compute(
        ctx,
        layer,
        kind,
        None,
        {"input": digest_array(x), "angles": digest_array(angles)},
        {},
        lambda: rope_mod.apply_rotation(x, angles),
    )


def compute_layer(
    ctx: EngineContext, layer: int
) -> Dict[str, Any]:
    """完整单层 DAG：返回输出、偏差、分块统计与缓存命中明细。"""
    task = ctx.task
    config = ctx.config
    query = compute_q(ctx, layer)
    key, value = compute_kv(ctx, layer)
    angles = compute_angles(ctx, layer)
    rope_q = compute_rope(ctx, layer, query, angles, "rope_q")
    rope_k = compute_rope(ctx, layer, key, angles, "rope_k")

    num_blocks = block_count(task)
    ranges = attn.block_ranges(task.seq_len, task.block_size)
    block_stats: List[attn.BlockStats] = []
    per_block_scores: List[np.ndarray] = []

    for block_index, (k_start, k_end) in enumerate(ranges):
        q_start, q_end = 0, task.seq_len

        def make_scores(
            q=rope_q, k=rope_k, ks=k_start, ke=k_end, qs=q_start, qe=q_end
        ) -> np.ndarray:
            scores = attn.scaled_dot_product(q[qs:qe], k[ks:ke])
            if task.causal:
                scores = attn.apply_causal_mask(scores, qs, ks)
            return scores

        score_digests = {
            "rope_q": digest_array(rope_q),
            "rope_k_slice": digest_array(rope_k[k_start:k_end]),
        }
        scores = _get_or_compute(
            ctx, layer, "score", block_index, score_digests,
            {"rope_k_node": node_id(task.case_id, layer, "rope_k")},
            make_scores,
        )

        def make_stats(
            scores=scores, bi=block_index, qr=(q_start, q_end),
            kr=(k_start, k_end)
        ) -> np.ndarray:
            stats = attn.compute_block_stats(
                task, rope_q, rope_k, bi, qr, kr, config
            )
            # 落盘为结构化数组：前两行存 block_max / block_denom。
            return np.stack([stats.block_max, stats.block_denom], axis=0)

        stats_array = _get_or_compute(
            ctx, layer, "stats", block_index,
            {"scores": digest_array(scores)},
            {"score_node": node_id(task.case_id, layer, "score", block_index)},
            make_stats,
        )
        stats = attn.BlockStats(
            block_index=block_index,
            q_start=q_start,
            q_end=q_end,
            k_start=k_start,
            k_end=k_end,
            block_max=stats_array[0],
            block_denom=stats_array[1],
        )
        block_stats.append(stats)
        per_block_scores.append(scores)

    # 分块在线 softmax 主循环（float64 累加）。
    state = attn.init_online_state(
        (task.seq_len, task.num_heads), task.head_dim
    )
    for stats, scores in zip(block_stats, per_block_scores):
        state = attn.update_online_state(state, stats, value, scores)
    chunked_output = attn.finalize_output(state, task.dtype)

    def make_output() -> np.ndarray:
        return chunked_output

    output_upstream = {
        f"stats_{stats.block_index}": digest_array(
            np.stack([stats.block_max, stats.block_denom], axis=0)
        )
        for stats in block_stats
    }
    output_upstream["value"] = digest_array(value)
    output = _get_or_compute(
        ctx, layer, "output", None,
        {"online_state": digest_array(state.output)},
        output_upstream, make_output,
    )

    # 整段参考路径与分母还原核对。
    ref_output, ref_max, ref_denom = attn.full_reference(
        task, rope_q, rope_k, value
    )

    def make_ref() -> np.ndarray:
        return np.concatenate(
            [ref_output.astype(np.float64),
             ref_max[..., None], ref_denom[..., None]],
            axis=-1,
        )

    ref_packed = _get_or_compute(
        ctx, layer, "ref", None,
        {"rope_q": digest_array(rope_q), "rope_k": digest_array(rope_k),
         "value": digest_array(value)},
        {}, make_ref,
    )
    # 运行时再核对一次缓存落盘的参考输出与本次计算一致。
    cached_ref_output = ref_packed[..., : task.head_dim]
    if not np.allclose(
        cached_ref_output, ref_output.astype(np.float64),
        rtol=1e-12, atol=1e-12,
    ):
        raise NumericUnstableError(
            "参考路径缓存产物与本次重算结果不一致",
            case_id=task.case_id,
            stage="score",
            layer=layer,
        )

    restored_max, restored_denom = attn.merged_full_denom(block_stats)
    restore_error = attn.denominator_restore_error(restored_denom, ref_denom)
    worst_index = np.unravel_index(
        np.argmax(restore_error), restore_error.shape
    )
    worst_block = _locate_worst_block(block_stats, restored_denom, ref_denom)
    if np.max(restore_error) > config.tolerance:
        raise NumericUnstableError(
            "分块统计合并还原整段 softmax 分母偏差超容差",
            case_id=task.case_id,
            stage="score",
            layer=layer,
            block=worst_block,
            details={
                "max_relative_error": float(np.max(restore_error)),
                "tolerance": config.tolerance,
                "index": [int(axis) for axis in worst_index],
                "block": worst_block,
                "case_id": task.case_id,
                "layer": layer,
            },
        )

    deviation = attn.elementwise_deviation(output, ref_output)
    return {
        "case_id": task.case_id,
        "layer": layer,
        "output_digest": digest_array(output),
        "ref_digest": digest_array(ref_output),
        "deviation": deviation,
        "denom_restore": {
            "max_relative_error": float(np.max(restore_error)),
            "index": [int(axis) for axis in worst_index],
            "block": worst_block,
        },
        "num_blocks": num_blocks,
        "hits": list(ctx.hits),
        "misses": list(ctx.misses),
    }


def _locate_worst_block(
    block_stats: List[attn.BlockStats],
    restored_denom: np.ndarray,
    reference_denom: np.ndarray,
) -> int:
    """逐块做增量合并，定位累计相对偏差首次超一半最终误差的块序号。"""
    final_error = float(
        np.max(attn.denominator_restore_error(restored_denom, reference_denom))
    )
    running_max = block_stats[0].block_max.copy()
    running_denom = np.zeros_like(running_max)
    for stats in block_stats:
        new_max = np.maximum(running_max, stats.block_max)
        scale_old = np.exp(
            np.where(np.isfinite(running_max), running_max - new_max, 0.0)
        )
        scale_block = np.exp(stats.block_max - new_max)
        running_denom = (
            running_denom * scale_old + stats.block_denom * scale_block
        )
        running_max = new_max
        error = attn.denominator_restore_error(running_denom, reference_denom)
        if float(np.max(error)) >= 0.5 * final_error:
            return stats.block_index
    return block_stats[-1].block_index


def run_worker(payload: Dict[str, Any]) -> Dict[str, Any]:
    """进程池入口：捕获异常并以业务码 + 阶段/层/块归因回传。"""
    task = Task(**payload["task"])
    config = RunConfig(**payload["config"])
    layer = payload["layer"]
    try:
        cache = ContentCache(config.cache_dir)
        ctx = EngineContext(task=task, config=config, cache=cache)
        return compute_layer(ctx, layer)
    except NumericUnstableError:
        raise
    except Exception as exc:  # noqa: BLE001 - 边界处统一包装回传
        if hasattr(exc, "code"):
            raise
        wrapped = WorkerFailure(
            f"worker 计算失败：{exc!r}",
            case_id=task.case_id,
            stage=payload.get("stage", "score"),
            layer=layer,
            block=payload.get("block"),
            details={"original_code": "E_INTERNAL", "error_type": type(exc).__name__},
        )
        raise wrapped from exc
