"""双路径生成配置的加载、校验与档位解析。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

import numpy as np

from .hashing import stable_hash


class ConfigError(ValueError):
    """配置不合法，业务码 E_CONFIG_INVALID。"""


REQUIRED_FIELDS = (
    "vocab", "embed_dim", "seed", "denoise_steps", "guidance_scale",
    "frames", "tiers", "img2img_replace_steps", "noise_tables",
    "guidance_tables", "concurrency", "first_frame_budget_ms",
    "denoise_step_budget", "memory_cap_mb",
)


def parse_tier(name: str) -> Tuple[int, int]:
    try:
        width, height = str(name).lower().split("x")
        width, height = int(width), int(height)
    except (ValueError, AttributeError) as exc:
        raise ConfigError(f"档位格式非法: {name!r}，应为 WxH") from exc
    if width <= 0 or height <= 0:
        raise ConfigError(f"档位尺寸必须为正整数: {name!r}")
    return width, height


@dataclass(frozen=True)
class Tier:
    name: str
    width: int
    height: int

    @property
    def pixels(self) -> int:
        return self.width * self.height

    @property
    def latent_shape(self) -> Tuple[int, int, int]:
        return (4, self.height // 8, self.width // 8)


@dataclass(frozen=True)
class Config:
    raw: Dict[str, Any]
    vocab: Tuple[str, ...]
    embed_dim: int
    seed: int
    denoise_steps: int
    guidance_scale: float
    frames: int
    tiers: Tuple[Tier, ...]
    img2img_replace_steps: int
    noise_tables: Dict[str, np.ndarray]
    guidance_tables: Dict[str, np.ndarray]
    concurrency: int
    first_frame_budget_ms: float
    denoise_step_budget: int
    memory_cap_mb: float
    latent_channels: int
    preview_cost_per_step_ms: float
    fault_inject: Dict[str, Any]
    source_path: str

    @property
    def tier_names(self) -> Tuple[str, ...]:
        return tuple(t.name for t in self.tiers)

    def tier(self, name: str) -> Tier:
        for tier in self.tiers:
            if tier.name == name:
                return tier
        raise KeyError(name)

    def config_hash(self) -> str:
        """配置内容哈希（噪声/引导表内容也纳入），用于快照命中判定。"""
        payload = {
            key: value for key, value in self.raw.items()
            if key not in ("noise_tables", "guidance_tables")
        }
        tables = {
            tier.name: {
                "noise": self.noise_tables[tier.name].tolist(),
                "guidance": self.guidance_tables[tier.name].tolist(),
            }
            for tier in self.tiers
        }
        return stable_hash(payload, tables)


def _load_table(base_dir: str, rel_path: str, tier_name: str,
                denoise_steps: int, kind: str) -> np.ndarray:
    full_path = rel_path if os.path.isabs(rel_path) else os.path.join(base_dir, rel_path)
    if not os.path.isfile(full_path):
        raise ConfigError(f"档位 {tier_name} 的{kind}文件不存在: {rel_path}")
    try:
        table = np.load(full_path).astype(np.float32)
    except Exception as exc:  # pragma: no cover - 由具体用例覆盖
        raise ConfigError(f"档位 {tier_name} 的{kind}无法读取: {exc}") from exc
    if table.ndim != 1 or table.shape[0] != denoise_steps:
        raise ConfigError(
            f"档位 {tier_name} 的{kind}长度 {table.shape[0] if table.ndim else 0} "
            f"不等于去噪步数 {denoise_steps}"
        )
    return table


def load_config(path: str) -> Config:
    if not os.path.isfile(path):
        raise ConfigError(f"配置文件不存在: {path}")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置 JSON 解析失败: {exc}") from exc

    missing = [name for name in REQUIRED_FIELDS if name not in raw]
    if missing:
        raise ConfigError(f"配置缺少必填字段: {', '.join(missing)}")

    fault = raw.get("fault_inject", {}) or {}
    if fault.get("drop_field"):
        raise ConfigError(f"fault_inject.drop_field: 缺失字段 {fault['drop_field']}")

    if not isinstance(raw["vocab"], list) or not raw["vocab"]:
        raise ConfigError("受限词表 vocab 必须是非空字符串数组")
    vocab = tuple(str(token) for token in raw["vocab"])
    if len(set(vocab)) != len(vocab):
        raise ConfigError("受限词表存在重复词元")

    embed_dim = raw["embed_dim"]
    if not isinstance(embed_dim, int) or embed_dim <= 0:
        raise ConfigError("embed_dim 必须为正整数")

    seed = raw["seed"]
    if not isinstance(seed, int) or not (0 <= seed < 2 ** 32):
        raise ConfigError("seed 必须是 [0, 2^32) 内整数")

    denoise_steps = raw["denoise_steps"]
    if not isinstance(denoise_steps, int) or denoise_steps <= 0:
        raise ConfigError("denoise_steps 必须为正整数")

    guidance_scale = raw["guidance_scale"]
    if not isinstance(guidance_scale, (int, float)) or isinstance(guidance_scale, bool) \
            or not (0.0 <= float(guidance_scale) <= 30.0):
        raise ConfigError("guidance_scale 必须是 [0, 30] 内数值")

    frames = raw["frames"]
    if not isinstance(frames, int) or frames <= 0:
        raise ConfigError("frames 必须为正整数")

    tier_names = raw["tiers"]
    if not isinstance(tier_names, list) or not tier_names:
        raise ConfigError("tiers 必须是非空档位数组")
    tiers: List[Tier] = []
    prev = None
    for name in tier_names:
        width, height = parse_tier(str(name))
        if width % 8 or height % 8:
            raise ConfigError(f"档位 {name} 尺寸必须是 8 的倍数（潜变量下采样 8 倍）")
        if prev is not None and not (width >= prev[0] and height >= prev[1]
                                     and width * height > prev[0] * prev[1]):
            raise ConfigError(f"档位集合必须单调递增: {prev} -> {(width, height)}")
        prev = (width, height)
        tiers.append(Tier(str(name), width, height))

    replace_steps = raw["img2img_replace_steps"]
    if not isinstance(replace_steps, int) or not (0 <= replace_steps <= denoise_steps):
        raise ConfigError("img2img_replace_steps 必须在 [0, denoise_steps] 内")

    concurrency = raw["concurrency"]
    if not isinstance(concurrency, int) or concurrency < 1:
        raise ConfigError("concurrency 必须为 >=1 的整数")

    budget_ms = raw["first_frame_budget_ms"]
    if not isinstance(budget_ms, (int, float)) or budget_ms < 0:
        raise ConfigError("first_frame_budget_ms 必须为 >=0 的数值")

    step_budget = raw["denoise_step_budget"]
    if not isinstance(step_budget, int) or step_budget < 0:
        raise ConfigError("denoise_step_budget 必须为 >=0 的整数")
    if fault.get("zero_step_budget"):
        step_budget = 0

    memory_cap = raw["memory_cap_mb"]
    if not isinstance(memory_cap, (int, float)) or memory_cap <= 0:
        raise ConfigError("memory_cap_mb 必须为正数")

    latent_channels = int(raw.get("latent_channels", 4))
    preview_cost = float(raw.get("preview_cost_per_step_ms", 1.0))
    if preview_cost <= 0:
        raise ConfigError("preview_cost_per_step_ms 必须为正数")

    base_dir = os.path.dirname(os.path.abspath(path))
    noise_tables: Dict[str, np.ndarray] = {}
    guidance_tables: Dict[str, np.ndarray] = {}
    noise_map = raw["noise_tables"]
    guidance_map = raw["guidance_tables"]
    for tier in tiers:
        if tier.name not in noise_map or tier.name not in guidance_map:
            raise ConfigError(f"档位 {tier.name} 缺少噪声表或引导表路径")
        noise_tables[tier.name] = _load_table(
            base_dir, noise_map[tier.name], tier.name, denoise_steps, "噪声表")
        guidance_tables[tier.name] = _load_table(
            base_dir, guidance_map[tier.name], tier.name, denoise_steps, "引导表")

    return Config(
        raw=raw, vocab=vocab, embed_dim=embed_dim, seed=seed,
        denoise_steps=denoise_steps, guidance_scale=float(guidance_scale),
        frames=frames, tiers=tuple(tiers),
        img2img_replace_steps=replace_steps,
        noise_tables=noise_tables, guidance_tables=guidance_tables,
        concurrency=concurrency, first_frame_budget_ms=float(budget_ms),
        denoise_step_budget=step_budget, memory_cap_mb=float(memory_cap),
        latent_channels=latent_channels,
        preview_cost_per_step_ms=preview_cost,
        fault_inject=fault, source_path=os.path.abspath(path),
    )
