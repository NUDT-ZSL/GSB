"""配置与输入校验链路、资源上限估算。

所有校验失败返回结构化业务码记录（不抛异常），并指明字段与 case_id。
"""

from __future__ import annotations

import numpy as np

from .errors import (
    E_DUP_CASE, E_DTYPE_UNSUPPORTED, E_PARAM_INVALID, E_RESOURCE_LIMIT,
    E_SHAPE_INVALID, error_record,
)
from .rope import VARIANTS

SUPPORTED_DTYPES = (np.dtype(np.float64), np.dtype(np.float32))


def validate_manifest(cases):
    """校验任务清单：重复 case_id、参数域。返回错误记录列表。"""
    errors = []
    seen = set()
    for c in cases:
        cid = c.get("case_id")
        if cid in seen:
            errors.append(error_record(
                E_DUP_CASE, "validate", cid,
                f"重复 case_id: {cid!r}", field="case_id"))
            continue
        seen.add(cid)
        errors.extend(validate_case_params(c))
    return errors


def validate_case_params(case):
    """单 case 参数域校验（不含数组形状）。"""
    errors = []
    cid = case.get("case_id")

    def bad(field, msg):
        errors.append(error_record(E_PARAM_INVALID, "validate", cid, msg,
                                   field=field))

    seq_len = case.get("seq_len")
    if not isinstance(seq_len, int) or seq_len <= 0:
        bad("seq_len", f"seq_len 必须为正整数, got {seq_len!r}")
    head_dim = case.get("head_dim")
    if not isinstance(head_dim, int) or head_dim <= 0:
        bad("head_dim", f"head_dim 必须为正整数, got {head_dim!r}")
    num_heads = case.get("num_heads")
    if not isinstance(num_heads, int) or num_heads <= 0:
        bad("num_heads", f"num_heads 必须为正整数, got {num_heads!r}")
    base_theta = case.get("base_theta")
    if not isinstance(base_theta, (int, float)) or not (1.0 < float(base_theta) <= 1e12):
        bad("base_theta", f"base_theta 越界 (1, 1e12], got {base_theta!r}")
    variant = case.get("pos_encoding_variant")
    if variant not in VARIANTS:
        bad("pos_encoding_variant", f"未知位置编码变体: {variant!r}")
    block_size = case.get("block_size")
    if not isinstance(block_size, int) or block_size <= 0:
        bad("block_size", f"block_size 必须为正整数, got {block_size!r}")
    if not isinstance(case.get("causal"), bool):
        bad("causal", f"causal 必须为布尔值, got {case.get('causal')!r}")
    if not isinstance(case.get("seed"), int):
        bad("seed", f"seed 必须为整数, got {case.get('seed')!r}")
    return errors


def validate_arrays(case, q, k, v):
    """校验 Q/K/V 数组维度与 dtype 是否和 case 声明一致。"""
    errors = []
    cid = case.get("case_id")
    expect = (int(case["num_heads"]), int(case["seq_len"]), int(case["head_dim"]))
    for name, arr in (("q", q), ("k", k), ("v", v)):
        if np.dtype(arr.dtype) not in SUPPORTED_DTYPES:
            errors.append(error_record(
                E_DTYPE_UNSUPPORTED, "validate", cid,
                f"{name} dtype 不支持: {arr.dtype} (支持 float64/float32)",
                field=name))
            continue
        if tuple(arr.shape) != expect:
            errors.append(error_record(
                E_SHAPE_INVALID, "validate", cid,
                f"{name} 形状 {tuple(arr.shape)} 与声明 {expect} 不符",
                field=name))
    return errors


def estimate_block_resident_bytes(case):
    """单分块打分驻留内存估算：num_heads * block_size * seq_len * 8 字节。"""
    return (int(case["num_heads"]) * int(case["block_size"])
            * int(case["seq_len"]) * 8)


def check_resource_limits(case, config):
    """资源上限检查。返回 (errors, degradations)。

    resource_policy=fail    -> 超限返回 E_RESOURCE_LIMIT；
    resource_policy=degrade -> 按策略降级并返回降级记录：
        seq_len 超限      -> resource_truncate（截断序列，由调用方执行）
        分块驻留超限      -> resource_block_clamp（收缩 block_size）
    """
    errors = []
    degradations = []
    cid = case.get("case_id")
    seq_len = int(case["seq_len"])
    if seq_len > config.max_seq_len:
        msg = f"seq_len={seq_len} 超过上限 {config.max_seq_len}"
        if config.resource_policy == "degrade":
            degradations.append({
                "case_id": cid, "stage": "validate",
                "degradation_class": "resource_truncate",
                "upstream_code": E_RESOURCE_LIMIT, "message": msg,
                "detail": {"new_seq_len": config.max_seq_len},
            })
        else:
            errors.append(error_record(
                E_RESOURCE_LIMIT, "validate", cid, msg, field="seq_len"))
    resident = estimate_block_resident_bytes(case)
    if resident > config.max_block_resident_bytes:
        msg = (f"分块驻留估算 {resident}B 超过上限 "
               f"{config.max_block_resident_bytes}B")
        if config.resource_policy == "degrade":
            new_bs = max(1, config.max_block_resident_bytes
                         // (int(case["num_heads"]) * seq_len * 8))
            degradations.append({
                "case_id": cid, "stage": "validate",
                "degradation_class": "resource_block_clamp",
                "upstream_code": E_RESOURCE_LIMIT, "message": msg,
                "detail": {"new_block_size": new_bs},
            })
        else:
            errors.append(error_record(
                E_RESOURCE_LIMIT, "validate", cid, msg, field="block_size",
                detail={"resident_bytes": resident}))
    return errors, degradations
