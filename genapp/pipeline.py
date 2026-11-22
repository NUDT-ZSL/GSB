"""确定性条件生成流水线 (五阶段串联).

阶段:
  1. 图像侧种子像素块生成 (seed -> 噪声场)
  2. 文本条件池化 (见 textcond.condition_vector)
  3. 条件融合为初始场 (显式声明的融合口径 FusionParams)
  4. 逐步迭代更新 (Jacobi 式, 仅依赖上一步场的 4 邻域)
  5. 结果聚合为图像字节 (全局归一化 -> uint8)

逐字节可复现的关键设计:
- 融合系数是全局像素坐标的确定性函数, 分块与全局共用同一来源
  (derive_fusion_params) 与同一归一化 (坐标 / 画布尺寸);
- 迭代更新为 Jacobi 式: 每个像素的新值只依赖上一步场的邻域,
  与像素计算次序无关, 因此分块依赖锥计算与全局一次计算逐位相同;
- 聚合统计量对同一张组装后的场做同一次 numpy 归约, 归约顺序固定.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np

from .config import GenerationConfig

Region = Tuple[int, int, int, int]  # (y0, y1, x0, x1), 半开区间


@dataclass(frozen=True)
class FusionParams:
    """显式声明的条件融合口径, 由条件向量确定性派生."""

    mix_base: float
    mix_amp: float
    freq_y: float
    freq_x: float
    phase: float
    pat_freq_y: float
    pat_freq_x: float
    pat_phases: Tuple[float, ...]
    reaction_target: Tuple[float, ...]

    def to_dict(self) -> dict:
        return {
            "mix_base": round(self.mix_base, 12),
            "mix_amp": round(self.mix_amp, 12),
            "freq_y": round(self.freq_y, 12),
            "freq_x": round(self.freq_x, 12),
            "phase": round(self.phase, 12),
            "pat_freq_y": round(self.pat_freq_y, 12),
            "pat_freq_x": round(self.pat_freq_x, 12),
            "pat_phases": [round(p, 12) for p in self.pat_phases],
            "reaction_target": [round(t, 12) for t in self.reaction_target],
        }


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def derive_fusion_params(cond_vec: np.ndarray, cfg: GenerationConfig) -> FusionParams:
    """从条件向量派生融合口径. 全局与分块路径必须共用此函数."""
    c = [float(v) for v in cond_vec]
    channels = cfg.channels
    return FusionParams(
        mix_base=0.5,
        mix_amp=0.35 * _sigmoid(c[3 % len(c)]),
        freq_y=1.0 + 2.0 * _sigmoid(c[4 % len(c)]),
        freq_x=1.0 + 2.0 * _sigmoid(c[5 % len(c)]),
        phase=math.pi * math.tanh(c[6 % len(c)]),
        pat_freq_y=1.0 + 3.0 * _sigmoid(c[7 % len(c)]),
        pat_freq_x=1.0 + 3.0 * _sigmoid(c[8 % len(c)]),
        pat_phases=tuple(
            math.pi * math.tanh(c[(9 + ch) % len(c)]) for ch in range(channels)
        ),
        reaction_target=tuple(
            0.5 + 0.3 * math.tanh(c[(12 + ch) % len(c)]) for ch in range(channels)
        ),
    )


def seed_field(cfg: GenerationConfig) -> np.ndarray:
    """阶段 1: 固定种子生成初始像素块 (整幅噪声场)."""
    rng = np.random.default_rng(cfg.seed)
    field = rng.random(
        (cfg.height, cfg.width, cfg.channels), dtype=np.float64
    )
    return field.astype(cfg.dtype)


def initial_field(
    cfg: GenerationConfig,
    fusion: FusionParams,
    region: Optional[Region] = None,
    noise: Optional[np.ndarray] = None,
) -> np.ndarray:
    """阶段 3: 条件融合为初始场.

    region 为全局坐标下的半开区间; 缺省为整幅画布.
    融合系数只依赖全局归一化坐标, 与切块方式无关.
    """
    if region is None:
        region = (0, cfg.height, 0, cfg.width)
    y0, y1, x0, x1 = region
    if noise is None:
        noise = seed_field(cfg)
    block_noise = noise[y0:y1, x0:x1, :]
    dtype = cfg.dtype
    ys = (np.arange(y0, y1, dtype=np.float64) + 0.5) / cfg.height
    xs = (np.arange(x0, x1, dtype=np.float64) + 0.5) / cfg.width
    yy, xx = np.meshgrid(ys, xs, indexing="ij")
    alpha = fusion.mix_base + fusion.mix_amp * np.sin(
        2.0 * math.pi * (fusion.freq_y * yy + fusion.freq_x * xx) + fusion.phase
    )
    alpha = alpha.astype(dtype)[:, :, None]
    pattern = np.empty((y1 - y0, x1 - x0, cfg.channels), dtype=dtype)
    for ch in range(cfg.channels):
        pattern[:, :, ch] = 0.5 + 0.5 * np.sin(
            2.0 * math.pi * (fusion.pat_freq_y * yy + fusion.pat_freq_x * xx)
            + fusion.pat_phases[ch]
        )
    return (1.0 - alpha) * block_noise + alpha * pattern


def _update_core(
    cfg: GenerationConfig,
    fusion: FusionParams,
    field: np.ndarray,
    anchor: Optional[np.ndarray],
    anchor_strength: float,
) -> np.ndarray:
    """单步 Jacobi 迭代: field 与 anchor (若有) 形状一致."""
    padded = np.pad(field, ((1, 1), (1, 1), (0, 0)), mode="edge")
    lap = (
        padded[:-2, 1:-1, :]
        + padded[2:, 1:-1, :]
        + padded[1:-1, :-2, :]
        + padded[1:-1, 2:, :]
        - 4.0 * field
    )
    target = np.asarray(fusion.reaction_target, dtype=cfg.dtype)[None, None, :]
    reaction = cfg.reaction * (target - field)
    nxt = field + cfg.dt * (lap + reaction)
    if anchor is not None and anchor_strength > 0.0:
        nxt = nxt + anchor_strength * (anchor - nxt)
    return nxt


def iterate_global(
    cfg: GenerationConfig,
    fusion: FusionParams,
    field0: np.ndarray,
    anchor: Optional[np.ndarray] = None,
    anchor_strength: float = 0.0,
) -> Tuple[np.ndarray, List[np.ndarray]]:
    """阶段 4 (全局口径): 返回最终场与各步中间场."""
    field = field0
    intermediates: List[np.ndarray] = []
    for _ in range(cfg.steps):
        field = _update_core(cfg, fusion, field, anchor, anchor_strength)
        intermediates.append(field)
    return field, intermediates


def cone_region(cfg: GenerationConfig, block: Region, margin: int) -> Region:
    """block 向外扩 margin 像素并裁剪到画布."""
    y0, y1, x0, x1 = block
    return (
        max(0, y0 - margin),
        min(cfg.height, y1 + margin),
        max(0, x0 - margin),
        min(cfg.width, x1 + margin),
    )


def iterate_block(
    cfg: GenerationConfig,
    fusion: FusionParams,
    block: Region,
    noise: Optional[np.ndarray] = None,
    anchor: Optional[np.ndarray] = None,
    anchor_strength: float = 0.0,
) -> np.ndarray:
    """阶段 4 (分块口径): 依赖锥计算, 结果与全局口径逐位相同.

    第 t 步只需 block 外扩 (steps - t) 像素的区域; 区域在画布边界处
    裁剪, 边界邻域采用与全局一致的边缘复制.
    """
    if noise is None:
        noise = seed_field(cfg)
    region = cone_region(cfg, block, cfg.steps)
    field = initial_field(cfg, fusion, region, noise=noise)
    for t in range(cfg.steps):
        next_region = cone_region(cfg, block, cfg.steps - t - 1)
        anchor_slice = None
        if anchor is not None:
            ry0, ry1, rx0, rx1 = region
            anchor_slice = anchor[ry0:ry1, rx0:rx1, :]
        field = _update_core(cfg, fusion, field, anchor_slice, anchor_strength)
        oy0 = next_region[0] - region[0]
        ox0 = next_region[2] - region[2]
        dy = next_region[1] - next_region[0]
        dx = next_region[3] - next_region[2]
        field = field[oy0 : oy0 + dy, ox0 : ox0 + dx, :]
        region = next_region
    return field


def aggregate(field: np.ndarray) -> bytes:
    """阶段 5: 全局归一化聚合为图像字节 (uint8, 行优先, 通道交错)."""
    fmin = float(field.min())
    fmax = float(field.max())
    span = fmax - fmin
    if span <= 0.0:
        span = 1.0
    img = np.clip((field - fmin) / span * 255.0, 0.0, 255.0)
    return img.astype(np.uint8).tobytes()


def run_global(
    cfg: GenerationConfig,
    cond_vec: np.ndarray,
    anchor: Optional[np.ndarray] = None,
    anchor_strength: float = 0.0,
) -> Dict[str, object]:
    """全局一次生成: 返回条件向量以外的全部阶段产物."""
    fusion = derive_fusion_params(cond_vec, cfg)
    field0 = initial_field(cfg, fusion)
    final, intermediates = iterate_global(
        cfg, fusion, field0, anchor=anchor, anchor_strength=anchor_strength
    )
    return {
        "fusion": fusion,
        "field0": field0,
        "intermediates": intermediates,
        "final": final,
        "image": aggregate(final),
    }
