"""画面差异强度评分与近似重复判定。"""

from __future__ import annotations

import numpy as np

# 评分权重：梯度清晰度为主，整体对比度为辅。
CLARITY_WEIGHT = 0.75
CONTRAST_WEIGHT = 0.25
# 平均梯度达到该值（0-255 灰度级）时清晰度记满分。
CLARITY_FULL_SCALE = 48.0


def score_frame(gray: np.ndarray) -> dict[str, float]:
    """返回画面差异强度评分及其构成项（均为 0-1，越高表示画面变化越明显）。"""

    data = gray.astype(np.float32)
    if data.shape[0] >= 2 and data.shape[1] >= 2:
        gx = np.abs(np.diff(data, axis=1)).mean()
        gy = np.abs(np.diff(data, axis=0)).mean()
        gradient = float((gx + gy) / 2.0)
    else:
        gradient = 0.0
    clarity = min(1.0, gradient / CLARITY_FULL_SCALE)
    contrast = float(min(1.0, data.std() / 128.0))
    score = CLARITY_WEIGHT * clarity + CONTRAST_WEIGHT * contrast
    return {
        "score": round(score, 6),
        "clarity": round(clarity, 6),
        "contrast": round(contrast, 6),
        "gradient": round(gradient, 6),
    }


def frame_difference(a: np.ndarray, b: np.ndarray) -> float:
    """两张同尺寸灰度帧的平均绝对差，归一化到 0-1；0 表示完全相同。"""

    if a.shape != b.shape:
        raise ValueError(f"帧尺寸不一致：{a.shape} vs {b.shape}")
    return float(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean() / 255.0)


def group_duplicates(items: list[dict], threshold: float) -> list[dict]:
    """把近似重复的候选合并成组。

    ``items`` 每项需含 ``gray``（灰度数组）、``score``（评分）以及排序用的
    ``position`` / ``time``。按评分从高到低（同分时时间点更早、清单序号更靠前）
    依次考察：尚未归组的候选若与某个已确定的保留代表之间平均绝对差不超过
    ``threshold``，就并入该代表所在的组；否则自己成为一个新组的代表。

    采用“代表帧”比较而非链式传递，避免 A≈B、B≈C 但 A 与 C 差异明显时被误并入
    同一组。返回按保留代表评分降序排列的组列表。
    """

    order = sorted(
        range(len(items)),
        key=lambda i: (-items[i]["score"], items[i]["time"], items[i]["position"]),
    )
    groups: list[dict] = []
    assigned = [False] * len(items)
    for index in order:
        if assigned[index]:
            continue
        assigned[index] = True
        keeper = index
        merged = []
        keeper_data = items[keeper]["gray"]
        for other in range(len(items)):
            if assigned[other]:
                continue
            value = frame_difference(keeper_data, items[other]["gray"])
            if value <= threshold:
                assigned[other] = True
                merged.append(
                    {
                        "index": other,
                        "position": items[other]["position"],
                        "time": items[other]["time"],
                        "difference": round(value, 6),
                    }
                )
        groups.append(
            {
                "keeper": keeper,
                "merged": sorted(merged, key=lambda m: (m["time"], m["position"])),
            }
        )
    return groups
