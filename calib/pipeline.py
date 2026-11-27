"""标定流水线：暗电流/暗角校正、辐射传输线性求解、反射率换算、配准与真彩合成。"""
from __future__ import annotations

import numpy as np

REJ_SIGMA = 3.0      # 剔除阈值：残差超过 3 倍 sigma
REJ_ABS = 15.0       # 剔除阈值：绝对残差下限（DN 量级）
MIN_POINTS = 3       # 可信定标所需的最少剩余定标点
VIGNETTE_FLOOR = 0.2  # 暗角场下限，保证除法安全


def build_vignette(params, h, w):
    """由暗角参数构建衰减场 V = 1 - c2*r^2 - c4*r^4（r 归一化到边角为 1）。"""
    c2, c4 = float(params["c2"]), float(params["c4"])
    yy, xx = np.mgrid[0:h, 0:w]
    rx = (xx - (w - 1) / 2.0) / ((w - 1) / 2.0)
    ry = (yy - (h - 1) / 2.0) / ((h - 1) / 2.0)
    r = np.sqrt(rx * rx + ry * ry) / np.sqrt(2.0)
    v = 1.0 - c2 * r ** 2 - c4 * r ** 4
    return np.clip(v, VIGNETTE_FLOOR, 1.0)


def correct_band(dn, dark, vignette):
    """暗电流减法加暗角场除法，返回校正后辐射影像与量级摘要。"""
    dn = np.asarray(dn, dtype=np.float64)
    dark = np.asarray(dark, dtype=np.float64)
    vignette = np.asarray(vignette, dtype=np.float64)
    if np.any(vignette <= 0):
        raise ValueError("暗角场存在非正像素，无法安全除法校正")
    corrected = (dn - dark) / vignette
    summary = {
        "dark_subtracted_mean": float(np.mean(dark)),
        "dark_subtracted_max": float(np.max(dark)),
        "dark_is_zero": bool(np.all(dark == 0)),
        "vignette_divided_min": float(np.min(vignette)),
        "vignette_divided_max": float(np.max(vignette)),
        "vignette_divided_mean": float(np.mean(vignette)),
        "vignette_is_uniform": bool(np.ptp(vignette) < 1e-9),
        "corrected_min": float(np.min(corrected)),
        "corrected_max": float(np.max(corrected)),
    }
    return corrected, summary


def _fit_line(points, corrected):
    n = len(points)
    R = np.array([p["reflectance"] for p in points])
    L = np.array([corrected[p["y"], p["x"]] for p in points])
    A = np.column_stack([R, np.ones(n)])
    coef, *_ = np.linalg.lstsq(A, L, rcond=None)
    a, b = float(coef[0]), float(coef[1])
    return a, b, L - (a * R + b)


def solve_band(corrected, panel, saturated_ids=()):
    """辐射传输线性模型 L = a*R + b 的最小二乘联合求解（a 为增益项，b 为大气路径辐照项）。

    饱和（过曝）定标点先验剔除；再以 RANSAC 点对共识确定内点集，避免粗差
    掩蔽；随后对残差超阈值点做迭代剔除并逐轮记录。仅剩两点时仍精确求解，
    并记录自由度为 0 及不可唯一判定的分量说明。
    """
    saturated = {int(s) for s in saturated_ids}
    pre_excluded = sorted(int(p["id"]) for p in panel if int(p["id"]) in saturated)
    pts = [dict(p) for p in panel if int(p["id"]) not in saturated]
    if len(pts) < 2:
        raise ValueError("定标点不足两个，无法求解线性模型")

    n = len(pts)
    R_all = np.array([p["reflectance"] for p in pts])
    L_all = np.array([corrected[p["y"], p["x"]] for p in pts])
    best_score, best_inl = None, None
    for i in range(n):
        for j in range(i + 1, n):
            dR = R_all[j] - R_all[i]
            if abs(dR) < 1e-9:
                continue
            a = (L_all[j] - L_all[i]) / dR
            b = L_all[i] - a * R_all[i]
            res = np.abs(L_all - (a * R_all + b))
            inl = res <= REJ_ABS
            score = (int(inl.sum()), -float(res[inl].sum()))
            if best_score is None or score > best_score:
                best_score, best_inl = score, inl
    remaining = [pts[k] for k in range(n) if best_inl[k]]
    outliers = [pts[k] for k in range(n) if not best_inl[k]]

    rounds = []
    while outliers:
        a, b, _ = _fit_line(remaining, corrected)
        worst, worst_res = None, -1.0
        for pt in outliers:
            r = abs(corrected[pt["y"], pt["x"]]
                    - (a * pt["reflectance"] + b))
            if r > worst_res:
                worst, worst_res = pt, r
        if worst_res <= REJ_ABS:
            break
        outliers.remove(worst)
        rounds.append({
            "round": len(rounds) + 1,
            "removed_id": int(worst["id"]),
            "removed_xy": [int(worst["x"]), int(worst["y"])],
            "residual": float(worst_res),
            "limit": float(REJ_ABS),
        })

    a, b, res = _fit_line(remaining, corrected)
    dof = len(remaining) - 2
    notes = []
    if pre_excluded:
        notes.append("饱和（过曝）定标点先验剔除: %s" % pre_excluded)
    if dof == 0:
        notes.append(
            "定标点仅剩两点：自由度为 0，增益项 a 与大气路径辐照项 b 由两点精确确定，"
            "无冗余观测，两个分量均不可独立检核，粗差不可唯一判定"
        )
    if rounds:
        notes.append("共迭代剔除 %d 个残差超限定标点" % len(rounds))
    return {
        "a": a, "b": b,
        "kept_ids": [int(p["id"]) for p in remaining],
        "rejected_ids": pre_excluded + [r["removed_id"] for r in rounds],
        "pre_excluded_ids": pre_excluded,
        "rounds": rounds,
        "n_points": len(remaining),
        "dof": int(dof),
        "residuals": {str(int(p["id"])): float(r) for p, r in zip(remaining, res)},
        "notes": notes,
    }


def reflectance_map(corrected, a, b):
    """由增益与大气项把校正后辐射值换算为地表反射率。"""
    return (corrected - b) / a


def estimate_shift(ref, img):
    """相位相关法估计 img 相对 ref 的整体平移 (dy, dx) 及峰值强度。"""
    F = np.fft.fft2(ref)
    G = np.fft.fft2(img)
    cross = F * np.conj(G)
    cross /= np.abs(cross) + 1e-12
    corr = np.fft.ifft2(cross)
    mag = np.abs(corr)
    py, px = np.unravel_index(int(np.argmax(mag)), mag.shape)
    h, w = ref.shape
    dy = py - h if py > h // 2 else py
    dx = px - w if px > w // 2 else px
    strength = float(mag[py, px] / (mag.mean() + 1e-12))
    return int(-dy), int(-dx), strength


def shift_with_fill(img, dy, dx, fill=np.nan):
    """把影像平移 (dy, dx) 重采样到统一网格，空出区域填 fill。"""
    h, w = img.shape
    out = np.full((h, w), fill, dtype=np.float64)
    ys = slice(max(0, dy), min(h, h + dy))
    xs = slice(max(0, dx), min(w, w + dx))
    ysrc = slice(max(0, -dy), min(h, h - dy))
    xsrc = slice(max(0, -dx), min(w, w - dx))
    out[ys, xs] = img[ysrc, xsrc]
    return out


def alignment_residual(ref, aligned, valid_mask=None):
    """对齐残差：重叠有效区内 1 - 归一化互相关系数（对波段间亮度差异不变）。

    valid_mask 可排除定标板斑点等非地物像素。
    """
    mask = np.isfinite(aligned) & np.isfinite(ref)
    if valid_mask is not None:
        mask &= valid_mask
    if np.sum(mask) < 16:
        return float("nan")
    a = ref[mask] - np.mean(ref[mask])
    b = aligned[mask] - np.mean(aligned[mask])
    denom = np.sqrt(np.sum(a * a) * np.sum(b * b)) + 1e-12
    return float(1.0 - np.sum(a * b) / denom)


def region_report(aligned, regions, bands):
    """对每块已知地物区域计算反射率均值与解析真值的偏差。"""
    report = {}
    for name, reg in regions.items():
        y0, y1, x0, x1 = reg["rect"]
        per_band = {}
        for b in bands:
            tile = aligned[b][y0:y1, x0:x1]
            mean = float(np.nanmean(tile))
            truth = float(reg["reflectance"][b])
            per_band[b] = {
                "mean": mean,
                "truth": truth,
                "bias": mean - truth,
                "abs_bias": abs(mean - truth),
                "valid_pixels": int(np.isfinite(tile).sum()),
            }
        report[name] = per_band
    return report


def composite_true_color(aligned, untrusted, true_color_map, bands, ref_band):
    """按固定波段映射合成真彩反射率影像。

    不可信波段的通道用参考波段（或首个可信波段）替换并记录；反射率截断到
    [0,1]，截断前的原始数值保留在 pre_clip 供偏差统计。
    """
    channels = {}
    for ch, band in true_color_map.items():
        used = band
        substituted = False
        reason = ""
        if band in untrusted:
            fallback = [ref_band] + [b for b in bands if b != ref_band]
            used = next(b for b in fallback if b not in untrusted)
            substituted = True
            reason = "; ".join(r["reason"] for r in untrusted[band])
        channels[ch] = {"mapped": band, "used": used,
                        "substituted": substituted, "reason": reason}
    pre = np.stack([np.nan_to_num(aligned[channels[ch]["used"]], nan=0.0)
                    for ch in ("R", "G", "B")], axis=-1)
    clipped = np.clip(pre, 0.0, 1.0)
    record = {
        "channels": channels,
        "clip": {
            "lower": 0.0, "upper": 1.0,
            "pre_min": float(pre.min()),
            "pre_max": float(pre.max()),
            "clipped_low_pixels": int((pre < 0.0).sum()),
            "clipped_high_pixels": int((pre > 1.0).sum()),
        },
    }
    return clipped, pre, record
