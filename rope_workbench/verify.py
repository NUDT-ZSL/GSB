"""独立验证器：复核流水线产出。

复核项（全部独立重算，不复用流水线中间量）：
  1. 旋转角表可重算（纯 Python 循环逐元素重算对比）
  2. 分块输出与整段输出在声明容差内一致
  3. 注入记录的阶段与实际执行阶段轨迹吻合
检测到偏差返回 E_VERIFY_FAIL 记录，指出首个偏差位置与 case_id。
"""

from __future__ import annotations

import numpy as np

from .errors import E_VERIFY_FAIL, error_record


def _recompute_angles_py(case):
    """纯 Python 独立重算角表（与 rope.angle_table 不同代码路径）。"""
    variant = case["pos_encoding_variant"]
    s = int(case["seq_len"])
    d = int(case["head_dim"])
    theta = float(case["base_theta"])
    scale = float(case["pos_scale"])
    bs = max(1, int(case["block_size"]))
    half = d // 2
    if variant == "ntk_aware" and d > 2:
        theta_eff = theta * scale ** (d / (d - 2.0))
    else:
        theta_eff = theta
    inv = [theta_eff ** (-2.0 * i / d) for i in range(half)]
    if variant == "position_interpolation":
        inv = [x / scale for x in inv]
    table = np.zeros((s, half), dtype=np.float64)
    for p in range(s):
        for i in range(half):
            if variant == "blockwise_freq":
                b = p // bs
                local = p - b * bs
                table[p, i] = local * inv[i] * theta ** (-2.0 * b / d)
            else:
                table[p, i] = p * inv[i]
    return table


def verify_case(case, result, config):
    """复核单 case 运行结果，返回问题记录列表（空列表 = 通过）。"""
    issues = []
    cid = case["case_id"]

    angle_degraded = any(d["stage"] == "angle" for d in result.degradations)
    if result.angles is not None and not angle_degraded:
        ref = _recompute_angles_py(result.case)
        if ref.shape != result.angles.shape:
            issues.append(error_record(
                E_VERIFY_FAIL, "verify", cid,
                f"角表形状 {result.angles.shape} 与重算 {ref.shape} 不符",
                field="angles"))
        else:
            diff = np.abs(result.angles - ref)
            tol = max(config.angle_tol, 1e-12) * max(1.0, float(np.abs(ref).max()))
            if float(diff.max()) > tol:
                idx = np.unravel_index(int(np.argmax(diff)), diff.shape)
                issues.append(error_record(
                    E_VERIFY_FAIL, "verify", cid,
                    f"角表重算偏差 {float(diff.max()):.3e} 超容差 {tol:.3e}",
                    field="angles",
                    detail={"first_dev_pos": int(idx[0]),
                            "first_dev_dim": int(idx[1])}))

    if result.output is not None and result.full_output is not None:
        dev = float(np.max(np.abs(result.output - result.full_output)))
        norm_degraded = any(d["stage"] == "normalize"
                            for d in result.degradations)
        if not norm_degraded and dev > config.block_tol:
            d = np.abs(result.output - result.full_output)
            idx = np.unravel_index(int(np.argmax(d)), d.shape)
            issues.append(error_record(
                E_VERIFY_FAIL, "verify", cid,
                f"分块/整段输出偏差 {dev:.3e} 超容差 {config.block_tol:.3e}",
                field="output",
                detail={"first_dev_index": [int(x) for x in idx]}))

    executed = {s["stage"] for s in result.executed_stages}
    degraded = {s["stage"] for s in result.executed_stages if s["degraded"]}
    for inj in result.injections:
        if inj["stage"] not in executed:
            issues.append(error_record(
                E_VERIFY_FAIL, "verify", cid,
                f"注入记录阶段 {inj['stage']!r} 不在实际执行轨迹 {sorted(executed)}",
                field="injections", detail={"injection": inj}))
        elif inj["stage"] not in degraded:
            issues.append(error_record(
                E_VERIFY_FAIL, "verify", cid,
                f"注入阶段 {inj['stage']!r} 未标记降级，轨迹不吻合",
                field="injections", detail={"injection": inj}))
    return issues
