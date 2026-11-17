"""定点特征归一化（minmax / zscore），全程整数运算。

原始浮点首先按 guard 位宽转成定点整数，此后统计量、归一化、量化全部在
整数域完成。每个转换点记录舍入残差，量化点记录量化残差。
"""

from .fixedpoint import FixedSpec, quantize_levels, round_div, to_fixed

GUARD_EXTRA_BITS = 4


def _request_matrix(request, feature_names):
    rows = []
    for cand in request["candidates"]:
        rows.append(build_feature_vector(cand, feature_names))
    return rows


def build_feature_vector(candidate, feature_names):
    vector = []
    for name in feature_names:
        if name in candidate["scores"]:
            vector.append(candidate["scores"][name])
        elif name in candidate["dense_map"]:
            vector.append(candidate["dense_map"][name])
        else:
            vector.append(candidate["sparse"][name])
    return vector


def normalize_request(request, feature_names, methods, spec: FixedSpec):
    """返回归一化产物字典，含每个候选的量化码、残差与请求级统计量。"""
    matrix = _request_matrix(request, feature_names)
    n = len(matrix)
    dim = len(feature_names)
    guard_bits = spec.frac_bits + GUARD_EXTRA_BITS
    guard_one = 1 << guard_bits
    one = 1 << spec.frac_bits

    fixed_matrix = []
    convert_residuals = []
    for row in matrix:
        fixed_row, res_row = [], []
        for value in row:
            q, residual = to_fixed(value, guard_bits, spec.rounding)
            fixed_row.append(q)
            res_row.append(residual)
        fixed_matrix.append(fixed_row)
        convert_residuals.append(res_row)

    stats = []
    codes = [[0] * dim for _ in range(n)]
    quant_residuals = [[0] * dim for _ in range(n)]
    for j in range(dim):
        col = [fixed_matrix[i][j] for i in range(n)]
        col_min, col_max = min(col), max(col)
        stat = {"feature": feature_names[j], "method": methods[j], "min": col_min, "max": col_max}
        if methods[j] == "minmax":
            span = col_max - col_min
            stat["span"] = span
            for i in range(n):
                if span == 0:
                    code, qres = one, 0
                else:
                    scaled, rrem = round_div(
                        (col[i] - col_min) * guard_one, span, spec.rounding
                    )
                    base, _srem = round_div(scaled, 1 << GUARD_EXTRA_BITS, spec.rounding)
                    qcode, qres = quantize_levels(base, spec.levels, spec.frac_bits, 0, one)
                    code = qcode
                    quant_residuals[i][j] = qres
                codes[i][j] = code
        else:
            total = sum(col)
            mean_q, mean_rem = round_div(total, n, spec.rounding)
            var_num = sum((v - mean_q) * (v - mean_q) for v in col)
            var_q, _ = round_div(var_num, n, spec.rounding)
            std_q = _isqrt(var_q)
            stat["mean"] = mean_q
            stat["std"] = std_q
            stat["mean_remainder"] = mean_rem
            if spec.levels:
                lo, hi = col_min, col_max
            else:
                lo = hi = None
            for i in range(n):
                if std_q <= 0:
                    base = 0
                else:
                    z, _ = round_div((col[i] - mean_q) * guard_one, std_q, spec.rounding)
                    base, _ = round_div(z, 1 << GUARD_EXTRA_BITS, spec.rounding)
                qcode, qres = quantize_levels(base, spec.levels, spec.frac_bits, lo, hi)
                codes[i][j] = qcode
                quant_residuals[i][j] = qres
        stats.append(stat)

    return {
        "spec": spec.as_dict(),
        "guard_bits": guard_bits,
        "stats": stats,
        "codes": codes,
        "convert_residuals": convert_residuals,
        "quant_residuals": quant_residuals,
        "raw_matrix": matrix,
    }


def _isqrt(value):
    if value < 0:
        raise ValueError("sqrt of negative integer")
    if value == 0:
        return 0
    x = value
    y = (x + 1) // 2
    while y < x:
        x = y
        y = (x + value // x) // 2
    return x
