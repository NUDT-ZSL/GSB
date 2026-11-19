"""跨变体一致性裁决。

声明的变体映射关系（角度域，p=位置, i=维对, d=head_dim, s=pos_scale）：
  rope_classic -> position_interpolation: angle_pi(p,i) = angle_classic(p,i) / s
  rope_classic -> ntk_aware:
      angle_ntk(p,i) = angle_classic(p,i) * s^(-2i/(d-2))   (d>2)
  rope_classic -> blockwise_freq:
      b = p // B, angle_bw(p,i) = (p - b*B)/p * theta^(-2b/d) * angle_classic(p,i)
      （p=0 时映射角为 0）

裁决输出偏差谱（逐维对最大绝对偏差、逐位置最大绝对偏差）与映射说明；
当配置声明等价（expect_equivalent）但实测偏差超容差时返回
E_VARIANT_MISMATCH，并指出首个偏差位置与变体名。
"""

from __future__ import annotations

import numpy as np

from .errors import E_RESOURCE_LIMIT, E_VARIANT_MISMATCH, error_record


def mapping_description(variant_a, variant_b, pos_scale, block_size, head_dim):
    """返回 (映射说明文本, 角度映射函数 mapped = f(angles_a))。"""
    key = (variant_a, variant_b)
    if key == ("rope_classic", "position_interpolation"):
        desc = f"angle_pi(p,i) = angle_classic(p,i) / s, s={pos_scale}"

        def f(angles):
            return angles / float(pos_scale)
    elif key == ("rope_classic", "ntk_aware"):
        d = head_dim
        desc = (f"angle_ntk(p,i) = angle_classic(p,i) * s^(-2i/(d-2)), "
                f"s={pos_scale}, d={d}")
        half = d // 2
        idx = np.arange(half, dtype=np.float64)
        factor = float(pos_scale) ** (-2.0 * idx / (d - 2.0)) if d > 2 \
            else np.ones(half)

        def f(angles):
            return angles * factor[None, :]
    elif key == ("rope_classic", "blockwise_freq"):
        desc = (f"angle_bw(p,i) = local(p)/p * theta^(-2*floor(p/B)/d) "
                f"* angle_classic(p,i), B={block_size}")

        def f(angles):
            return angles  # 占位，实际映射在 adjudicate 中按位置构造
    else:
        desc = f"未声明映射: {variant_a} -> {variant_b}（仅做直接偏差对比）"

        def f(angles):
            return angles
    return desc, f


def _blockwise_mapped_angles(angles_a, case_b):
    """classic 角表 -> blockwise_freq 声明映射角表（按位置逐行构造）。"""
    theta = float(case_b["base_theta"])
    d = int(case_b["head_dim"])
    bs = max(1, int(case_b["block_size"]))
    seq_len = angles_a.shape[0]
    pos = np.arange(seq_len, dtype=np.float64)
    blk = np.floor(pos / bs)
    local = pos - blk * bs
    ratio = np.where(pos > 0, local / np.maximum(pos, 1.0), 0.0)
    decay = theta ** (-2.0 * blk / d)
    return angles_a * (ratio * decay)[:, None]


def adjudicate(result_a, result_b, config):
    """对同一 case 的两种变体运行结果做一致性裁决，返回裁决报告 dict。"""
    case_a, case_b = result_a.case, result_b.case
    va, vb = case_a["pos_encoding_variant"], case_b["pos_encoding_variant"]
    s = float(case_b["pos_scale"])
    d = int(case_b["head_dim"])
    bs = int(case_b["block_size"])
    desc, map_fn = mapping_description(va, vb, s, bs, d)

    report = {
        "case_id": case_a["case_id"],
        "variant_a": va, "variant_b": vb,
        "mapping": desc,
        "expect_equivalent": bool(case_b.get("expect_equivalent", False)),
        "tolerance": config.variant_tol,
        "errors": [],
    }

    angles_a, angles_b = result_a.angles, result_b.angles
    if angles_a is None or angles_b is None:
        report["errors"].append(error_record(
            E_VARIANT_MISMATCH, "adjudicate", case_a["case_id"],
            "存在缺失角表（上游已降级），无法裁决", field="angles"))
        return report

    n_cmp = angles_a.size * 2 + (result_a.output.size
                                 if result_a.output is not None else 0)
    report["comparisons"] = int(n_cmp)
    if n_cmp > config.max_comparisons:
        report["errors"].append(error_record(
            E_RESOURCE_LIMIT, "adjudicate", case_a["case_id"],
            f"比较次数 {n_cmp} 超过上限 {config.max_comparisons}",
            field="max_comparisons"))
        return report

    if (va, vb) == ("rope_classic", "blockwise_freq"):
        mapped = _blockwise_mapped_angles(angles_a, case_b)
    else:
        mapped = map_fn(angles_a)

    diff = np.abs(mapped - angles_b)          # (seq_len, half)
    spectrum_dim = diff.max(axis=0)           # 逐维对最大偏差
    spectrum_pos = diff.max(axis=1)           # 逐位置最大偏差
    report["angle_dev_spectrum"] = {
        "per_dim": spectrum_dim.tolist(),
        "per_pos_max": float(spectrum_pos.max()),
        "max_abs": float(diff.max()),
    }

    if result_a.output is not None and result_b.output is not None \
            and result_a.output.shape == result_b.output.shape:
        out_diff = np.abs(result_a.output - result_b.output)
        report["output_dev_spectrum"] = {
            "per_head": out_diff.max(axis=(1, 2)).tolist(),
            "max_abs": float(out_diff.max()),
        }
    else:
        report["output_dev_spectrum"] = None

    if report["expect_equivalent"]:
        max_dev = report["angle_dev_spectrum"]["max_abs"]
        if max_dev > config.variant_tol:
            idx = np.unravel_index(int(np.argmax(diff)), diff.shape)
            report["errors"].append(error_record(
                E_VARIANT_MISMATCH, "adjudicate", case_a["case_id"],
                f"声明等价但角度偏差 {max_dev:.3e} 超容差 "
                f"{config.variant_tol:.3e}",
                field="angles",
                detail={"first_dev_pos": int(idx[0]),
                        "first_dev_dim": int(idx[1]),
                        "variant_a": va, "variant_b": vb}))
    return report
