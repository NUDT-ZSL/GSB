"""图生图参考图锚定.

参考图作为初始场锚点参与迭代: 每步更新后按锚定强度向参考图
(归一化到 [0,1] 的浮点场) 回归. 校验锚定强度区间与参考图形状,
并输出各块相对锚点的偏差谱与主导偏差块.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .config import GenerationConfig
from .errors import E_ANCHOR_SHAPE, E_ANCHOR_STRENGTH, GenError
from .pipeline import Region

ANCHOR_MIN = 0.0
ANCHOR_MAX = 1.0


def validate_anchor_strength(strength: float) -> float:
    """锚定强度必须在 [0, 1] 闭区间内."""
    if not (ANCHOR_MIN <= strength <= ANCHOR_MAX):
        raise GenError(
            E_ANCHOR_STRENGTH,
            "anchor strength out of range",
            strength=strength,
            allowed=[ANCHOR_MIN, ANCHOR_MAX],
        )
    return strength


def load_anchor(
    cfg: GenerationConfig, ref_bytes: bytes, shape: Tuple[int, int, int]
) -> np.ndarray:
    """把参考图字节数组解析为浮点锚点场, 并校验与画布尺寸匹配."""
    expected = (cfg.height, cfg.width, cfg.channels)
    if tuple(shape) != expected:
        raise GenError(
            E_ANCHOR_SHAPE,
            "reference image shape mismatch",
            expected=list(expected),
            actual=list(shape),
        )
    raw = np.frombuffer(ref_bytes, dtype=np.uint8)
    if raw.size != expected[0] * expected[1] * expected[2]:
        raise GenError(
            E_ANCHOR_SHAPE,
            "reference image byte length mismatch",
            expected=int(np.prod(expected)),
            actual=int(raw.size),
        )
    return raw.reshape(expected).astype(cfg.dtype) / 255.0


def deviation_spectrum(
    cfg: GenerationConfig,
    block_fields: Sequence[Tuple[int, Region, np.ndarray]],
    anchor: np.ndarray,
) -> Dict[str, object]:
    """各块相对锚点的偏差谱 (平均绝对偏差) 与主导偏差块."""
    entries: List[Dict[str, object]] = []
    for bid, (y0, y1, x0, x1), field in block_fields:
        dev = float(np.mean(np.abs(field - anchor[y0:y1, x0:x1, :])))
        entries.append(
            {"block_id": int(bid), "region": [y0, y1, x0, x1], "mean_abs_dev": dev}
        )
    entries.sort(key=lambda e: e["block_id"])
    dominant: Optional[Dict[str, object]] = None
    if entries:
        dominant = max(entries, key=lambda e: (e["mean_abs_dev"], -e["block_id"]))
    return {"spectrum": entries, "dominant_block": dominant}
