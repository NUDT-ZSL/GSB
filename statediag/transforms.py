"""确定性状态生成、分块变换与跨块归约层。"""

import hashlib

import numpy as np

from .errors import EValidation
from .model import BlockTrace


def generate_state(state_seed, dim):
    """固定种子驱动 NumPy 确定性生成 float64 初始状态数组。"""
    rng = np.random.default_rng(int(state_seed))
    return rng.standard_normal(int(dim)).astype(np.float64)


def array_checksum(array):
    """对数组原始字节做 sha256，逐字节稳定，与机器端序无关地固定小端。"""
    arr = np.asarray(array)
    payload = arr.astype(arr.dtype, copy=False).astype("<" + arr.dtype.kind
                                                       + str(arr.dtype.itemsize),
                                                       copy=False).tobytes()
    h = hashlib.sha256()
    h.update(str(arr.shape).encode("utf-8"))
    h.update(b"|")
    h.update(payload)
    return h.hexdigest()


def block_bounds(dim, block_size):
    """确定性分块边界：block_size 必须整除状态维度。"""
    if dim <= 0 or block_size <= 0:
        raise EValidation("维度与块大小必须为正数")
    if dim % block_size != 0:
        raise EValidation(
            "block_size 不能整除状态维度",
            details={"dim": dim, "block_size": block_size},
        )
    return [
        (start, min(start + block_size, dim))
        for start in range(0, dim, block_size)
    ]


def run_monolithic(initial, backend, num_steps, budget=None):
    """整段一次执行路径（参考真值在 float64 下使用）。"""
    cur = backend.initial(initial)
    scope = budget.scope(cur, "monolithic_state") if budget is not None else None
    if scope is None:
        for step in range(num_steps):
            cur = backend.update_block(cur, 0, 0, step)
        return cur
    with scope:
        for step in range(num_steps):
            cur = backend.update_block(cur, 0, 0, step)
        return cur


def run_block_serial(initial, backend, num_steps, block_size, budget=None):
    """分块串行执行路径，返回 (最终状态, 块轨迹列表)。

    每一步按块索引升序更新，块边界邻居取自当前完整状态，因此与整段
    一次执行调用完全相同的逐元素算子，float64 下逐元素位级一致。
    """
    initial = np.asarray(initial)
    dim = initial.shape[0]
    bounds = block_bounds(dim, block_size)
    cur = backend.initial(initial)
    nxt = np.empty_like(cur)
    window = np.empty(block_size, dtype=cur.dtype)
    scopes = (
        [budget.scope(cur, "serial_state"), budget.scope(window, "block_window")]
        if budget is not None
        else []
    )
    if not scopes:
        for step in range(num_steps):
            for block_index, (start, end) in enumerate(bounds):
                left_boundary = cur[start - 1] if start > 0 else 0
                right_boundary = cur[end] if end < dim else 0
                nxt[start:end] = backend.update_block(
                    cur[start:end], left_boundary, right_boundary, step
                )
            cur = nxt.copy()
        traces = _build_traces(cur, bounds, backend, num_steps)
        return cur, traces
    with scopes[0], scopes[1]:
        for step in range(num_steps):
            for block_index, (start, end) in enumerate(bounds):
                left_boundary = cur[start - 1] if start > 0 else 0
                right_boundary = cur[end] if end < dim else 0
                nxt[start:end] = backend.update_block(
                    cur[start:end], left_boundary, right_boundary, step
                )
            cur = nxt.copy()
        traces = _build_traces(cur, bounds, backend, num_steps)
        return cur, traces


def _build_traces(cur, bounds, backend, num_steps):
    traces = []
    for block_index, (start, end) in enumerate(bounds):
        block = cur[start:end]
        raw_sum = backend.kahan_sum(list(block))
        if getattr(backend, "is_fixed", False):
            block_sum = float(raw_sum) / backend.scale
        else:
            block_sum = float(raw_sum)
        traces.append(
            BlockTrace(
                block_index=block_index,
                global_start=start,
                global_end=end,
                reduction_index=block_index,
                steps=num_steps,
                block_sum=float(block_sum),
                checksum=array_checksum(block),
            )
        )
    return traces


def cross_block_reduce(final_state, traces, backend):
    """跨块归约：归约顺序固定为块索引升序，块内/跨块均补偿求和。"""
    ordered = sorted(traces, key=lambda t: t.reduction_index)
    block_sums = [t.block_sum for t in ordered]
    # block_sum 已统一为浮点物理单位；跨块用 float64 补偿求和，归约顺序
    # 固定为块索引升序，保证确定性与跨精度同口径。
    global_sum = _float64_kahan(block_sums)
    dim = final_state.shape[0]
    reduction_trace = [
        {
            "reduction_index": t.reduction_index,
            "block_index": t.block_index,
            "global_start": t.global_start,
            "global_end": t.global_end,
            "block_sum": float(t.block_sum),
        }
        for t in ordered
    ]
    state_values = np.asarray(final_state).ravel()
    if getattr(backend, "is_fixed", False):
        state_values = backend.to_float(state_values)
    sq_sum = _float64_kahan([float(x) * float(x) for x in state_values])
    return {
        "global_sum": float(global_sum),
        "global_mean": float(global_sum) / dim,
        "global_l2": float(np.sqrt(float(sq_sum))),
        "order": [t.reduction_index for t in ordered],
        "trace": reduction_trace,
    }


def _float64_kahan(terms):
    total = 0.0
    comp = 0.0
    for term in terms:
        y = float(term) - comp
        t = total + y
        comp = (t - total) - y
        total = t
    return total
