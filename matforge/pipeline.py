from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

from .hashing import array_digest, combine_digests, digest_obj
from .params import MaterialParams
from .plan import Block, BlockPlan

_U64 = np.uint64
_MASK64 = (1 << 64) - 1


def _splitmix64_scalar(value: int) -> int:
    value = (value + 0x9E3779B97F4A7C15) & _MASK64
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & _MASK64
    value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & _MASK64
    return (value ^ (value >> 31)) & _MASK64


def _hash_lattice(x: np.ndarray, y: np.ndarray, seed: int, octave: int) -> np.ndarray:
    with np.errstate(over="ignore"):
        value = x.astype(np.uint64) * _U64(0xD6E8FEB86659FD93)
        value ^= y.astype(np.uint64) * _U64(0xA5A3564E27F8862F)
        value ^= _U64(seed & _MASK64) * _U64(0x9E3779B97F4A7C15)
        value ^= _U64((octave + 1) * 0xBF58476D1CE4E5B9 & _MASK64)
        value ^= value >> _U64(30)
        value *= _U64(0xBF58476D1CE4E5B9)
        value ^= value >> _U64(27)
        value *= _U64(0x94D049BB133111EB)
        value ^= value >> _U64(31)
    return (value >> _U64(11)).astype(np.float64) * (1.0 / 9007199254740992.0)


def _smooth(value: np.ndarray) -> np.ndarray:
    return value * value * value * (value * (value * 6.0 - 15.0) + 10.0)


def _seed_phase(seed: int, octave: int) -> tuple[float, float]:
    left = _splitmix64_scalar(seed ^ ((octave + 1) * 0x9E3779B97F4A7C15))
    right = _splitmix64_scalar(left ^ 0xD1B54A32D192ED03)
    return (left >> 11) / 9007199254740992.0, (right >> 11) / 9007199254740992.0


def noise_field(
    params: MaterialParams,
    plan: BlockPlan,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
) -> np.ndarray:
    """Evaluate the multi-octave field in global pixel coordinates."""
    xs = np.arange(x0, x1, dtype=np.float64) + 0.5
    ys = np.arange(y0, y1, dtype=np.float64) + 0.5
    grid_x = np.broadcast_to(xs[None, :], (y1 - y0, x1 - x0))
    grid_y = np.broadcast_to(ys[:, None], (y1 - y0, x1 - x0))
    result = np.zeros_like(grid_x, dtype=np.float64)
    noise = params.noise
    weights = noise.octave_weights()
    for octave, weight in enumerate(weights):
        frequency = noise.base_frequency * (noise.lacunarity**octave)
        seed_x, seed_y = _seed_phase(params.seed, octave)
        sx = (grid_x / plan.width) * frequency + noise.phase_x + seed_x
        sy = (grid_y / plan.height) * frequency + noise.phase_y + seed_y
        left = np.floor(sx)
        top = np.floor(sy)
        tx = _smooth(sx - left)
        ty = _smooth(sy - top)
        x0i = left.astype(np.int64)
        y0i = top.astype(np.int64)
        v00 = _hash_lattice(x0i, y0i, params.seed, octave)
        v10 = _hash_lattice(x0i + 1, y0i, params.seed, octave)
        v01 = _hash_lattice(x0i, y0i + 1, params.seed, octave)
        v11 = _hash_lattice(x0i + 1, y0i + 1, params.seed, octave)
        lower = v00 + (v10 - v00) * tx
        upper = v01 + (v11 - v01) * tx
        result += weight * (lower + (upper - lower) * ty)
    return np.ascontiguousarray(result, dtype=np.float64)


@dataclass(frozen=True)
class NormalizationStats:
    mean: float
    std: float
    minimum: float
    maximum: float
    sample_count: int
    digest: str

    @classmethod
    def from_fields(cls, samples: np.ndarray) -> NormalizationStats:
        flat = np.asarray(samples, dtype=np.float64)
        std = float(flat.std())
        std = max(std, 1e-12)
        stats = {
            "mean": float(flat.mean()),
            "std": std,
            "minimum": float(flat.min()),
            "maximum": float(flat.max()),
            "sample_count": int(flat.size),
        }
        return cls(**stats, digest=digest_obj(stats))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_normalization(params: MaterialParams, plan: BlockPlan, sample_count: int = 4096) -> NormalizationStats:
    """Compute one global normalization source shared by full and block generation."""
    count = min(max(64, sample_count), plan.pixel_count)
    indices = np.arange(count, dtype=np.uint64)
    with np.errstate(over="ignore"):
        hashed = indices * _U64(0x9E3779B97F4A7C15)
        hashed ^= _U64(params.seed & _MASK64)
        hashed ^= hashed >> _U64(29)
        hashed *= _U64(0xBF58476D1CE4E5B9)
        hashed ^= hashed >> _U64(32)
    xs = (hashed % _U64(plan.width)).astype(np.int64)
    ys = ((hashed >> _U64(17)) % _U64(plan.height)).astype(np.int64)
    noise = params.noise
    weights = noise.octave_weights()
    values = np.zeros(count, dtype=np.float64)
    for octave, weight in enumerate(weights):
        frequency = noise.base_frequency * (noise.lacunarity**octave)
        seed_x, seed_y = _seed_phase(params.seed, octave)
        sx = ((xs.astype(np.float64) + 0.5) / plan.width) * frequency + noise.phase_x + seed_x
        sy = ((ys.astype(np.float64) + 0.5) / plan.height) * frequency + noise.phase_y + seed_y
        left = np.floor(sx)
        top = np.floor(sy)
        tx = _smooth(sx - left)
        ty = _smooth(sy - top)
        x0i = left.astype(np.int64)
        y0i = top.astype(np.int64)
        v00 = _hash_lattice(x0i, y0i, params.seed, octave)
        v10 = _hash_lattice(x0i + 1, y0i, params.seed, octave)
        v01 = _hash_lattice(x0i, y0i + 1, params.seed, octave)
        v11 = _hash_lattice(x0i + 1, y0i + 1, params.seed, octave)
        lower = v00 + (v10 - v00) * tx
        upper = v01 + (v11 - v01) * tx
        values += weight * (lower + (upper - lower) * ty)
    return NormalizationStats.from_fields(values)


def normalize_field(field: np.ndarray, stats: NormalizationStats) -> np.ndarray:
    normalized = 0.5 + (field - stats.mean) / (stats.std * 6.0)
    return np.ascontiguousarray(np.clip(normalized, 0.0, 1.0), dtype=np.float64)


def _edge_distance(x_coords: np.ndarray, y_coords: np.ndarray, plan: BlockPlan) -> np.ndarray:
    distances = np.full(x_coords.shape, np.inf, dtype=np.float64)
    for col in range(1, plan.cols):
        boundary = col * plan.tile_width
        distances = np.minimum(distances, np.abs(x_coords - boundary))
    for row in range(1, plan.rows):
        boundary = row * plan.tile_height
        distances = np.minimum(distances, np.abs(y_coords - boundary))
    return distances


def seam_weights(x0: int, y0: int, x1: int, y1: int, plan: BlockPlan, params: MaterialParams) -> np.ndarray:
    xs = np.arange(x0, x1, dtype=np.float64)[None, :]
    ys = np.arange(y0, y1, dtype=np.float64)[:, None]
    distance = _edge_distance(xs, ys, plan)
    weight = 1.0 - params.seam_strength * np.exp(-distance / max(params.seam_width, 1e-9))
    return np.ascontiguousarray(np.clip(weight, 0.0, 1.0), dtype=np.float64)


def _normal_from_height(height: np.ndarray, strength: float) -> np.ndarray:
    if height.shape[0] < 2 or height.shape[1] < 2:
        base = np.zeros((*height.shape, 3), dtype=np.float64)
        base[..., 2] = 1.0
        return base
    dy, dx = np.gradient(height, edge_order=1)
    nx = -dx * strength
    ny = -dy * strength
    nz = np.ones_like(height)
    length = np.sqrt(nx * nx + ny * ny + nz * nz)
    return np.stack((nx / length, ny / length, nz / length), axis=-1)


def map_channels(
    height: np.ndarray,
    params: MaterialParams,
    plan: BlockPlan,
    x0: int,
    y0: int,
    seam: np.ndarray,
    normal: np.ndarray | None = None,
) -> np.ndarray:
    base = np.asarray(params.base_color, dtype=np.float64) / 255.0
    if normal is None:
        normal = _normal_from_height(height, params.normal_strength)
    modulation = 0.72 + 0.58 * height
    light = np.asarray((-0.35, -0.45, 0.82), dtype=np.float64)
    lambert = np.clip(normal[..., 0] * light[0] + normal[..., 1] * light[1] + normal[..., 2] * light[2], 0.0, 1.0)
    shading = 0.78 + 0.34 * lambert
    color = base[None, None, :] * modulation[..., None] * shading[..., None]
    color = 0.5 + (color - 0.5) * params.saturation
    roughness = np.clip(params.roughness + params.roughness_contrast * (height - 0.5), 0.0, 1.0)
    color = np.clip(color * seam[..., None], 0.0, 1.0)
    roughness = np.clip(roughness * seam, 0.0, 1.0)
    channels = np.empty((*height.shape, 7), dtype=np.uint8)
    channels[..., 0:3] = np.rint(color * 255.0).astype(np.uint8)
    channels[..., 3] = np.rint(roughness * 255.0).astype(np.uint8)
    channels[..., 4:7] = np.rint((normal * 0.5 + 0.5) * 255.0).astype(np.uint8)
    return np.ascontiguousarray(channels)


@dataclass
class StageRecord:
    name: str
    duration_ms: float
    digest: str
    detail: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FullTexture:
    channels: np.ndarray
    height: np.ndarray
    seam: np.ndarray
    stats: NormalizationStats
    digest: str
    stages: list[StageRecord]
    duration_ms: float
    plan_digest: str
    params_digest: str

    def summary(self) -> dict[str, Any]:
        return {
            "shape": list(self.channels.shape),
            "digest": self.digest,
            "duration_ms": self.duration_ms,
            "plan_digest": self.plan_digest,
            "params_digest": self.params_digest,
            "normalization": self.stats.to_dict(),
            "stages": [stage.to_dict() for stage in self.stages],
        }


@dataclass
class BlockTexture:
    block: Block
    channels: np.ndarray
    seam: np.ndarray
    raw_noise: np.ndarray
    normalization_digest: str
    upstream_hash: str
    content_digest: str
    channel_digest: str
    seam_digest: str
    duration_ms: float
    stage_timings_ms: dict[str, float]
    estimates: dict[str, Any]
    cache_key: str
    semantic_key: str
    node_path: str

    def summary(self) -> dict[str, Any]:
        return {
            "block": self.block.index,
            "pixel_range": self.block.pixel_range,
            "shape": list(self.channels.shape),
            "channel_digest": self.channel_digest,
            "seam_digest": self.seam_digest,
            "content_digest": self.content_digest,
            "upstream_hash": self.upstream_hash,
            "normalization_digest": self.normalization_digest,
            "cache_key": self.cache_key,
            "semantic_key": self.semantic_key,
            "node_path": self.node_path,
            "duration_ms": self.duration_ms,
            "stage_timings_ms": self.stage_timings_ms,
            "estimates": self.estimates,
        }


def estimate_block_cost(params: MaterialParams, plan: BlockPlan, block_index: int) -> dict[str, Any]:
    block = plan.block(block_index)
    pixels = block.pixel_count
    halo_pixels = (block.width + 2) * (block.height + 2)
    seam_neighborhood = 2 * (block.width + block.height) * max(1, math.ceil(params.seam_width * 3))
    estimated_ms = 0.025 + halo_pixels * 0.000015 * params.noise.octaves + seam_neighborhood * 0.000004
    return {
        "formula_version": "cost-v1",
        "pixel_count": pixels,
        "halo_pixel_count": halo_pixels,
        "octave_count": params.noise.octaves,
        "seam_neighborhood_pixels": seam_neighborhood,
        "estimated_ms": round(estimated_ms, 6),
        "coefficients": {"pixel_ms": 0.000015, "seam_pixel_ms": 0.000004, "base_ms": 0.025},
    }


def _block_cache_key(
    params: MaterialParams,
    plan: BlockPlan,
    block_index: int,
    normalization_digest: str,
    upstream_hash: str,
) -> str:
    return digest_obj(
        {
            "schema": "matforge.cache-key.v1",
            "params_digest": params.digest(),
            "seed": params.seed,
            "plan_digest": plan.digest(),
            "block_grid": {
                "width": plan.width,
                "height": plan.height,
                "tile_width": plan.tile_width,
                "tile_height": plan.tile_height,
                "rows": plan.rows,
                "cols": plan.cols,
                "block": block_index,
            },
            "octave_config_digest": params.noise.digest(),
            "normalization_digest": normalization_digest,
            "upstream_hash": upstream_hash,
        }
    )


def semantic_cache_key(
    params: MaterialParams,
    plan: BlockPlan,
    block_index: int,
    normalization_digest: str,
) -> str:
    """A pre-generation lookup key. The final key additionally binds upstream bytes."""
    block = plan.block(block_index)
    raster_params = params.to_dict()
    for key in ("revision", "name", "metadata"):
        raster_params.pop(key, None)
    return digest_obj(
        {
            "schema": "matforge.semantic-key.v1",
            "raster_params": raster_params,
            "seed": params.seed,
            "plan_digest": plan.digest(),
            "block_grid": {
                "width": plan.width,
                "height": plan.height,
                "tile_width": plan.tile_width,
                "tile_height": plan.tile_height,
                "rows": plan.rows,
                "cols": plan.cols,
                "block": block_index,
            },
            "octave_config_digest": params.noise.digest(),
            "normalization_digest": normalization_digest,
            "upstream_descriptor": {
                "noise": params.noise.digest(),
                "seed": params.seed,
                "plan": plan.digest(),
                "normalization": normalization_digest,
                "pixel_range": block.pixel_range,
            },
        }
    )


def generate_block(
    params: MaterialParams,
    plan: BlockPlan,
    block_index: int,
    stats: NormalizationStats | None = None,
) -> BlockTexture:
    started = time.perf_counter()
    params.validate()
    block = plan.block(block_index)
    stats = stats or compute_normalization(params, plan)
    stage: dict[str, float] = {}
    mark = time.perf_counter()
    ex0, ey0 = max(0, block.x0 - 1), max(0, block.y0 - 1)
    ex1, ey1 = min(plan.width, block.x1 + 1), min(plan.height, block.y1 + 1)
    raw = noise_field(params, plan, ex0, ey0, ex1, ey1)
    stage["noise_ms"] = (time.perf_counter() - mark) * 1000.0
    mark = time.perf_counter()
    height = normalize_field(raw, stats)
    normal = _normal_from_height(height, params.normal_strength)
    crop = (slice(block.y0 - ey0, block.y1 - ey0), slice(block.x0 - ex0, block.x1 - ex0))
    height = np.ascontiguousarray(height[crop])
    normal = np.ascontiguousarray(normal[crop])
    stage["normalize_ms"] = (time.perf_counter() - mark) * 1000.0
    mark = time.perf_counter()
    seam = seam_weights(block.x0, block.y0, block.x1, block.y1, plan, params)
    channels = map_channels(height, params, plan, block.x0, block.y0, seam, normal)
    stage["map_seam_ms"] = (time.perf_counter() - mark) * 1000.0
    upstream_hash = combine_digests(array_digest(raw), stats.digest, params.noise.digest())
    cache_key = _block_cache_key(params, plan, block_index, stats.digest, upstream_hash)
    semantic_key = semantic_cache_key(params, plan, block_index, stats.digest)
    duration_ms = (time.perf_counter() - started) * 1000.0
    return BlockTexture(
        block=block,
        channels=channels,
        seam=seam,
        raw_noise=raw,
        normalization_digest=stats.digest,
        upstream_hash=upstream_hash,
        content_digest=combine_digests(array_digest(channels), array_digest(seam)),
        channel_digest=array_digest(channels),
        seam_digest=array_digest(seam),
        duration_ms=duration_ms,
        stage_timings_ms=stage,
        estimates=estimate_block_cost(params, plan, block_index),
        cache_key=cache_key,
        semantic_key=semantic_key,
        node_path=f"material/{params.material_id}/pipeline/channel_map/block/{block_index:04d}",
    )


def generate_full(params: MaterialParams, plan: BlockPlan) -> FullTexture:
    started = time.perf_counter()
    params.validate()
    mark = time.perf_counter()
    stats = compute_normalization(params, plan)
    stages = [StageRecord("validate+normalization", (time.perf_counter() - mark) * 1000.0, stats.digest, stats.to_dict())]
    mark = time.perf_counter()
    raw = noise_field(params, plan, 0, 0, plan.width, plan.height)
    stages.append(
        StageRecord(
            "multi_octave_noise",
            (time.perf_counter() - mark) * 1000.0,
            array_digest(raw),
            {"shape": list(raw.shape), "octaves": params.noise.octaves, "weights": list(params.noise.octave_weights())},
        )
    )
    mark = time.perf_counter()
    height = normalize_field(raw, stats)
    normal = _normal_from_height(height, params.normal_strength)
    stages.append(
        StageRecord(
            "material_channel_map",
            (time.perf_counter() - mark) * 1000.0,
            "pending",
            {"normal_source": "global_halo_consistent_gradient"},
        )
    )
    mark = time.perf_counter()
    seam = seam_weights(0, 0, plan.width, plan.height, plan, params)
    channels = map_channels(height, params, plan, 0, 0, seam, normal)
    stages.append(
        StageRecord(
            "block_grid_slice",
            (time.perf_counter() - mark) * 1000.0,
            plan.digest(),
            {"rows": plan.rows, "cols": plan.cols, "block_count": plan.block_count},
        )
    )
    mark = time.perf_counter()
    digest = array_digest(channels)
    stages.append(
        StageRecord(
            "seam_normalize+stitch",
            (time.perf_counter() - mark) * 1000.0,
            digest,
            {"seam_width": params.seam_width, "seam_strength": params.seam_strength},
        )
    )
    stages[2].digest = combine_digests(array_digest(height), array_digest(normal), digest)
    return FullTexture(
        channels=channels,
        height=height,
        seam=seam,
        stats=stats,
        digest=digest,
        stages=stages,
        duration_ms=(time.perf_counter() - started) * 1000.0,
        plan_digest=plan.digest(),
        params_digest=params.digest(),
    )


def stitch_blocks(plan: BlockPlan, channels_by_block: dict[int, np.ndarray]) -> np.ndarray:
    missing = sorted(set(range(plan.block_count)) - set(channels_by_block))
    if missing:
        from .errors import fail

        fail("E_BLOCK_MISSING", "拼接前仍有切片未就绪", missing=missing)
    stitched = np.empty((plan.height, plan.width, 7), dtype=np.uint8)
    for block in plan.blocks():
        stitched[block.y0 : block.y1, block.x0 : block.x1] = channels_by_block[block.index]
    return stitched
