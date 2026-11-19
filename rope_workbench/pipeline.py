"""四阶段流水线：angle -> rotate -> score -> normalize。

注入触发（全部由输入/参数触发，禁止改源码制造）：
  angle     : pos_scale <= 0                        -> E_INJECT_ANGLE
  rotate    : head_dim 为奇数，半维配对越界          -> E_INJECT_ROTATE
  score     : block_size > seq_len                -> E_INJECT_SCORE
  normalize : causal=False 且 mask='causal' 语义冲突 -> E_INJECT_NORM
  另支持 case['inject'] = {'stage': ..., 'kind': ...} 显式声明注入。

降级策略（下游阶段按明确策略降级，记录携带降级类别与上游业务码）：
  angle 失败     -> identity_angles（恒等位置，角表置零）
  rotate 失败    -> skip_rotation（跳过旋转，用原始 Q/K）
  score 失败     -> clamp_block（block_size 收缩为 seq_len，单块打分）
  normalize 失败 -> reuse_prev_stats（沿用上一查询块统计量，首块均匀权重）

未被任何阶段归因的异常汇入结果报告的 unattributed 列表，运行中止，
不得静默产出看似正常的结果。
"""

from __future__ import annotations

import numpy as np

from . import attention, rope, validate
from .config import DEFAULT_CONFIG
from .errors import (
    E_INJECT_NORM, E_INJECT_ROTATE, E_INJECT_SCORE, E_UNATTRIBUTED,
    STAGE_ANGLE, STAGE_NORMALIZE, STAGE_ROTATE, STAGE_SCORE, STAGES,
    StageError, error_record,
)


class CaseResult:
    """单 case 运行结果：张量产出 + 诊断数据。"""

    def __init__(self, case, config):
        self.case_id = case["case_id"]
        self.case = dict(case)
        self.config = config
        self.angles = None
        self.q_rot = None
        self.k_rot = None
        self.output = None
        self.full_output = None
        self.max_block_dev = None
        self.angle_diag = {}
        self.norm_stats = []
        self.injections = []
        self.degradations = []
        self.errors = []
        self.unattributed = []
        self.executed_stages = []
        self.resources = {}
        self.status = "ok"

    def stage_done(self, stage, degraded=False, detail=None):
        self.executed_stages.append({
            "stage": stage, "degraded": bool(degraded),
            "detail": detail or {},
        })

    def add_injection(self, err, trigger):
        self.injections.append({
            "code": err.code, "stage": err.stage, "case_id": err.case_id,
            "field": err.field, "message": err.message, "trigger": trigger,
        })

    def add_degradation(self, stage, degradation_class, upstream_code, detail=None):
        self.degradations.append({
            "case_id": self.case_id, "stage": stage,
            "degradation_class": degradation_class,
            "upstream_code": upstream_code, "detail": detail or {},
        })


def _declared_inject(case, stage):
    inj = case.get("inject")
    if isinstance(inj, dict) and inj.get("stage") == stage:
        return str(inj.get("kind", "declared"))
    return None


def _apply_resource_degradation(case, degradations):
    eff = dict(case)
    applied = []
    for d in degradations:
        if d["degradation_class"] == "resource_truncate":
            eff["seq_len"] = d["detail"]["new_seq_len"]
            applied.append(d)
        elif d["degradation_class"] == "resource_block_clamp":
            eff["block_size"] = max(1, d["detail"]["new_block_size"])
            applied.append(d)
    return eff, applied


def run_case(case, arrays=None, config=None):
    """运行单 case 四阶段流水线，返回 CaseResult。"""
    config = config or DEFAULT_CONFIG
    result = CaseResult(case, config)

    param_errors = validate.validate_case_params(case)
    param_errors = [e for e in param_errors
                    if e["field"] not in ("pos_scale", "block_size")]
    result.errors.extend(param_errors)
    res_errors, res_degrades = validate.check_resource_limits(case, config)
    result.errors.extend(res_errors)
    if param_errors or res_errors:
        result.status = "rejected"
        return result

    eff_case = dict(case)
    if res_degrades:
        eff_case, applied = _apply_resource_degradation(case, res_degrades)
        result.degradations.extend(applied)

    if arrays is None:
        from .samples import case_arrays
        q, k, v = case_arrays(eff_case)
    else:
        q, k, v = arrays
    arr_errors = validate.validate_arrays(eff_case, q, k, v)
    result.errors.extend(arr_errors)
    if arr_errors:
        result.status = "rejected"
        return result
    q = np.asarray(q, dtype=np.float64)
    k = np.asarray(k, dtype=np.float64)
    v = np.asarray(v, dtype=np.float64)

    seq_len = int(eff_case["seq_len"])
    head_dim = int(eff_case["head_dim"])
    block_size = int(eff_case["block_size"])
    causal = bool(eff_case["causal"])
    result.resources = {
        "seq_len": seq_len,
        "block_resident_bytes": validate.estimate_block_resident_bytes(eff_case),
        "angle_table_bytes": seq_len * (head_dim // 2) * 8,
    }

    # 阶段 1: angle
    try:
        kind = _declared_inject(eff_case, STAGE_ANGLE)
        if kind:
            raise StageError("E_INJECT_ANGLE", STAGE_ANGLE, eff_case["case_id"],
                             f"declared inject: {kind}", field="inject")
        angles, result.angle_diag = rope.angle_table(eff_case, config)
        result.stage_done(STAGE_ANGLE)
    except StageError as e:
        result.add_injection(e, trigger="param" if e.field != "inject" else "declared")
        angles = np.zeros((seq_len, head_dim // 2), dtype=np.float64)
        result.angle_diag = {"subnormal_guard_triggered": False,
                             "rounding_mode": config.rounding_mode,
                             "max_abs_angle": 0.0, "angle_rel_err": 0.0,
                             "half_dim": head_dim // 2,
                             "pairing": "interleaved(2i,2i+1)"}
        result.add_degradation(STAGE_ANGLE, "identity_angles", e.code)
        result.stage_done(STAGE_ANGLE, degraded=True)
    except Exception as e:
        result.unattributed.append(error_record(
            E_UNATTRIBUTED, STAGE_ANGLE, eff_case["case_id"], repr(e)))
        result.status = "aborted"
        return result

    # 阶段 2: rotate
    try:
        kind = _declared_inject(eff_case, STAGE_ROTATE)
        if kind:
            raise StageError(E_INJECT_ROTATE, STAGE_ROTATE, eff_case["case_id"],
                             f"declared inject: {kind}", field="inject")
        if head_dim % 2 != 0:
            raise StageError(E_INJECT_ROTATE, STAGE_ROTATE, eff_case["case_id"],
                             f"head_dim={head_dim} odd, half-dim pairing overflow",
                             field="head_dim")
        q_rot = rope.apply_rotation(q, angles, eff_case["case_id"], head_dim)
        k_rot = rope.apply_rotation(k, angles, eff_case["case_id"], head_dim)
        result.stage_done(STAGE_ROTATE)
    except StageError as e:
        result.add_injection(e, trigger="param" if e.field != "inject" else "declared")
        q_rot, k_rot = q, k
        result.add_degradation(STAGE_ROTATE, "skip_rotation", e.code)
        result.stage_done(STAGE_ROTATE, degraded=True)
    except Exception as e:
        result.unattributed.append(error_record(
            E_UNATTRIBUTED, STAGE_ROTATE, eff_case["case_id"], repr(e)))
        result.status = "aborted"
        return result

    # 阶段 3: score
    try:
        kind = _declared_inject(eff_case, STAGE_SCORE)
        if kind:
            raise StageError(E_INJECT_SCORE, STAGE_SCORE, eff_case["case_id"],
                             f"declared inject: {kind}", field="inject")
        if block_size > seq_len:
            raise StageError(E_INJECT_SCORE, STAGE_SCORE, eff_case["case_id"],
                             f"block_size={block_size} > seq_len={seq_len}",
                             field="block_size")
        blocks = attention.score_blocks(q_rot, k_rot, block_size, causal)
        result.stage_done(STAGE_SCORE)
    except StageError as e:
        result.add_injection(e, trigger="param" if e.field != "inject" else "declared")
        blocks = attention.score_blocks(q_rot, k_rot, seq_len, causal)
        result.add_degradation(STAGE_SCORE, "clamp_block", e.code,
                               detail={"new_block_size": seq_len})
        result.stage_done(STAGE_SCORE, degraded=True)
    except Exception as e:
        result.unattributed.append(error_record(
            E_UNATTRIBUTED, STAGE_SCORE, eff_case["case_id"], repr(e)))
        result.status = "aborted"
        return result

    # 阶段 4: normalize
    try:
        kind = _declared_inject(eff_case, STAGE_NORMALIZE)
        mask_conflict = (not causal) and eff_case.get("mask") == "causal"
        if kind or mask_conflict:
            msg = (f"declared inject: {kind}" if kind else
                   "causal=False conflicts with mask='causal'")
            raise StageError(E_INJECT_NORM, STAGE_NORMALIZE, eff_case["case_id"],
                             msg, field="mask" if mask_conflict else "inject")
        out, stats = attention.normalize_blocks(blocks, v, seq_len)
        result.stage_done(STAGE_NORMALIZE)
    except StageError as e:
        result.add_injection(e, trigger="param" if e.field != "inject" else "declared")
        out, stats = attention.normalize_blocks(blocks, v, seq_len,
                                                degrade_prev_stats=True)
        result.add_degradation(STAGE_NORMALIZE, "reuse_prev_stats", e.code)
        result.stage_done(STAGE_NORMALIZE, degraded=True)
    except Exception as e:
        result.unattributed.append(error_record(
            E_UNATTRIBUTED, STAGE_NORMALIZE, eff_case["case_id"], repr(e)))
        result.status = "aborted"
        return result

    result.angles = angles
    result.q_rot = q_rot
    result.k_rot = k_rot
    result.output = out
    result.norm_stats = stats
    result.full_output = attention.full_attention(q_rot, k_rot, v, causal)
    result.max_block_dev = float(np.max(np.abs(out - result.full_output)))
    if result.injections or result.degradations:
        result.status = "degraded"
    return result
