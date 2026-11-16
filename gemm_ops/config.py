"""Configuration loading and validation for the GEMM/BMM component."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .errors import GemmError


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SHAPE_SPECS = ROOT / "shape_specs" / "shapes.json"
DEFAULT_STRATEGY_TABLE = ROOT / "strategy_table" / "strategy_table.json"
DEFAULT_COST_MODEL = ROOT / "strategy_table" / "cost_model.json"
DEFAULT_RUNTIME = ROOT / "strategy_table" / "runtime.json"
DEFAULT_INJECTIONS = ROOT / "injection_config" / "injections.json"
DEFAULT_REPORT = ROOT / "reports" / "gemm_bmm_report.json"


def load_json(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        with source.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError as exc:
        raise GemmError(
            "E_CONFIG_INVALID",
            f"configuration file not found: {source}",
            {"path": str(source)},
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise GemmError(
            "E_CONFIG_INVALID",
            f"configuration file cannot be read: {source}",
            {"path": str(source), "error": str(exc)},
        ) from exc
    if not isinstance(value, dict):
        raise GemmError(
            "E_CONFIG_INVALID",
            f"configuration root must be a JSON object: {source}",
            {"path": str(source)},
        )
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def require_positive_int(name: str, value: Any, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GemmError(
            "E_CONFIG_INVALID",
            f"{name} must be an integer",
            {"field": name, "value": value},
        )
    minimum = 0 if allow_zero else 1
    if value < minimum:
        raise GemmError(
            "E_CONFIG_INVALID",
            f"{name} must be >= {minimum}",
            {"field": name, "value": value},
        )
    return value


def require_positive_number(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GemmError(
            "E_CONFIG_INVALID",
            f"{name} must be numeric",
            {"field": name, "value": value},
        )
    result = float(value)
    if result <= 0.0:
        raise GemmError(
            "E_CONFIG_INVALID",
            f"{name} must be > 0",
            {"field": name, "value": value},
        )
    return result


@dataclass(frozen=True)
class Tile:
    BM: int
    BN: int
    BK: int

    def as_dict(self) -> dict[str, int]:
        return {"BM": self.BM, "BN": self.BN, "BK": self.BK}


@dataclass(frozen=True)
class Strategy:
    strategy_id: str
    tile: Tile
    loop_order: str
    batch_schedule: str
    tags: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "tile": self.tile.as_dict(),
            "loop_order": self.loop_order,
            "batch_schedule": self.batch_schedule,
            "tags": list(self.tags),
        }


@dataclass(frozen=True)
class CostModel:
    flops_per_second: float
    memory_bytes_per_second: float
    tile_overhead_seconds: float
    task_overhead_seconds: float
    parallel_sync_seconds: float
    cache_miss_penalty_scale: float
    cache_pressure_exponent: float
    nominal_parallel_workers: int
    flatten_chunk_size: int
    batch_parallel_min_batch: int
    flatten_min_batch: int
    formula: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "flops_per_second": self.flops_per_second,
            "memory_bytes_per_second": self.memory_bytes_per_second,
            "tile_overhead_seconds": self.tile_overhead_seconds,
            "task_overhead_seconds": self.task_overhead_seconds,
            "parallel_sync_seconds": self.parallel_sync_seconds,
            "cache_miss_penalty_scale": self.cache_miss_penalty_scale,
            "cache_pressure_exponent": self.cache_pressure_exponent,
            "nominal_parallel_workers": self.nominal_parallel_workers,
            "flatten_chunk_size": self.flatten_chunk_size,
            "batch_parallel_min_batch": self.batch_parallel_min_batch,
            "flatten_min_batch": self.flatten_min_batch,
            "formula": dict(self.formula),
        }


@dataclass(frozen=True)
class RuntimeConfig:
    max_workers: int
    max_tile_elements: int
    max_tile_dimension: int
    absolute_tolerance: float
    relative_tolerance: float
    relative_error_epsilon: float
    benchmark_all_candidates: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_workers": self.max_workers,
            "max_tile_elements": self.max_tile_elements,
            "max_tile_dimension": self.max_tile_dimension,
            "absolute_tolerance": self.absolute_tolerance,
            "relative_tolerance": self.relative_tolerance,
            "relative_error_epsilon": self.relative_error_epsilon,
            "benchmark_all_candidates": self.benchmark_all_candidates,
        }


def parse_tile(value: Mapping[str, Any]) -> Tile:
    if not isinstance(value, Mapping) or set(value) != {"BM", "BN", "BK"}:
        raise GemmError(
            "E_TILE_INVALID",
            "tile must contain exactly BM, BN and BK",
            {"tile": value},
        )
    parsed: dict[str, int] = {}
    for key in ("BM", "BN", "BK"):
        item = value[key]
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            raise GemmError(
                "E_TILE_INVALID",
                f"tile dimension {key} must be a positive integer",
                {"tile": value, "field": key},
            )
        parsed[key] = item
    return Tile(**parsed)


def load_strategy_table(path: str | Path = DEFAULT_STRATEGY_TABLE) -> tuple[dict[str, Any], list[Strategy]]:
    raw = load_json(path)
    entries = raw.get("strategies")
    if not isinstance(entries, list) or not entries:
        raise GemmError(
            "E_CONFIG_INVALID",
            "strategy table must contain a non-empty strategies list",
            {"path": str(path)},
        )
    strategies: list[Strategy] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise GemmError("E_CONFIG_INVALID", "strategy entry must be an object")
        strategy_id = entry.get("strategy_id")
        if not isinstance(strategy_id, str) or not strategy_id:
            raise GemmError("E_CONFIG_INVALID", "strategy_id must be a non-empty string")
        if strategy_id in seen:
            raise GemmError(
                "E_CONFIG_INVALID",
                "strategy_id values must be unique",
                {"strategy_id": strategy_id},
            )
        seen.add(strategy_id)
        loop_order = entry.get("loop_order")
        if loop_order not in {"ijk", "ikj", "jik"}:
            raise GemmError(
                "E_CONFIG_INVALID",
                "loop_order must be one of ijk, ikj, jik",
                {"strategy_id": strategy_id, "loop_order": loop_order},
            )
        batch_schedule = entry.get("batch_schedule")
        if batch_schedule not in {"serial", "batch_parallel", "flattened"}:
            raise GemmError(
                "E_CONFIG_INVALID",
                "batch_schedule must be serial, batch_parallel or flattened",
                {"strategy_id": strategy_id, "batch_schedule": batch_schedule},
            )
        tags = entry.get("tags", [])
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise GemmError(
                "E_CONFIG_INVALID",
                "strategy tags must be a list of strings",
                {"strategy_id": strategy_id},
            )
        strategies.append(
            Strategy(
                strategy_id=strategy_id,
                tile=parse_tile(entry.get("tile", {})),
                loop_order=loop_order,
                batch_schedule=batch_schedule,
                tags=tuple(tags),
            )
        )
    return raw, strategies


def load_cost_model(path: str | Path = DEFAULT_COST_MODEL) -> tuple[dict[str, Any], CostModel]:
    raw = load_json(path)
    formula = raw.get("formula")
    if not isinstance(formula, Mapping):
        raise GemmError("E_CONFIG_INVALID", "cost model must contain a formula object")
    model = CostModel(
        flops_per_second=require_positive_number(
            "flops_per_second", raw.get("flops_per_second")
        ),
        memory_bytes_per_second=require_positive_number(
            "memory_bytes_per_second", raw.get("memory_bytes_per_second")
        ),
        tile_overhead_seconds=require_positive_number(
            "tile_overhead_seconds", raw.get("tile_overhead_seconds")
        ),
        task_overhead_seconds=require_positive_number(
            "task_overhead_seconds", raw.get("task_overhead_seconds")
        ),
        parallel_sync_seconds=require_positive_number(
            "parallel_sync_seconds", raw.get("parallel_sync_seconds")
        ),
        cache_miss_penalty_scale=require_positive_number(
            "cache_miss_penalty_scale", raw.get("cache_miss_penalty_scale")
        ),
        cache_pressure_exponent=require_positive_number(
            "cache_pressure_exponent", raw.get("cache_pressure_exponent")
        ),
        nominal_parallel_workers=require_positive_int(
            "nominal_parallel_workers", raw.get("nominal_parallel_workers")
        ),
        flatten_chunk_size=require_positive_int(
            "flatten_chunk_size", raw.get("flatten_chunk_size")
        ),
        batch_parallel_min_batch=require_positive_int(
            "batch_parallel_min_batch", raw.get("batch_parallel_min_batch")
        ),
        flatten_min_batch=require_positive_int(
            "flatten_min_batch", raw.get("flatten_min_batch")
        ),
        formula=dict(formula),
    )
    return raw, model


def load_runtime(path: str | Path = DEFAULT_RUNTIME) -> tuple[dict[str, Any], RuntimeConfig]:
    raw = load_json(path)
    config = RuntimeConfig(
        max_workers=require_positive_int("max_workers", raw.get("max_workers")),
        max_tile_elements=require_positive_int(
            "max_tile_elements", raw.get("max_tile_elements")
        ),
        max_tile_dimension=require_positive_int(
            "max_tile_dimension", raw.get("max_tile_dimension")
        ),
        absolute_tolerance=require_positive_number(
            "absolute_tolerance", raw.get("absolute_tolerance")
        ),
        relative_tolerance=require_positive_number(
            "relative_tolerance", raw.get("relative_tolerance")
        ),
        relative_error_epsilon=require_positive_number(
            "relative_error_epsilon", raw.get("relative_error_epsilon")
        ),
        benchmark_all_candidates=bool(raw.get("benchmark_all_candidates", True)),
    )
    return raw, config
