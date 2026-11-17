"""定点加权融合打分、温度缩放与截断。"""

from .fixedpoint import FixedSpec, clip_int, round_div, to_fixed


def fuse_request(norm_result, weights, temperature, score_clip, spec: FixedSpec):
    """对请求内全部候选做定点融合，返回逐候选融合产物。"""
    frac = spec.frac_bits
    one = 1 << frac
    weight_q = []
    weight_residuals = []
    for weight in weights:
        q, residual = to_fixed(weight, frac, spec.rounding)
        weight_q.append(q)
        weight_residuals.append(residual)
    temp_q, temp_residual = to_fixed(temperature, frac, spec.rounding)
    clip_lo, _ = to_fixed(score_clip[0], frac, spec.rounding)
    clip_hi, _ = to_fixed(score_clip[1], frac, spec.rounding)

    per_candidate = []
    for i, row in enumerate(norm_result["codes"]):
        # code 与 weight_q 均为 QF，乘积为 Q2F，除以 ONE 还原为 QF
        contributions = [row[j] * weight_q[j] for j in range(len(row))]
        contribution_qf = []
        contribution_remainders = []
        for product in contributions:
            value, remainder = round_div(product, one, spec.rounding)
            contribution_qf.append(value)
            contribution_remainders.append(remainder)
        weighted_sum = sum(contribution_qf)
        if temp_q == one:
            scaled = weighted_sum
            scale_remainder = 0
        else:
            scaled, scale_remainder = round_div(weighted_sum * one, temp_q, spec.rounding)
        clipped, was_clipped, clip_delta = clip_int(scaled, clip_lo, clip_hi)
        per_candidate.append(
            {
                "index": i,
                "weight_q": list(weight_q),
                "weight_residuals": weight_residuals,
                "temp_q": temp_q,
                "temp_residual": temp_residual,
                "contributions": contribution_qf,
                "contribution_products_q2f": contributions,
                "contribution_remainders": contribution_remainders,
                "weighted_sum": weighted_sum,
                "scaled": scaled,
                "scale_remainder": scale_remainder,
                "score": clipped,
                "clipped": was_clipped,
                "clip_delta": clip_delta,
                "clip_bounds": [clip_lo, clip_hi],
            }
        )
    return per_candidate
