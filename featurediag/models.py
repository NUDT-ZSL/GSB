"""Input contracts and strict validation for tasks, batches, and run config."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .canonical import read_json, read_jsonl, sha256_json
from .errors import BusinessError


QUANT_FORMULA_ALIASES = {
    "affine_zero_point",
    "q=round(x/scale)+zero_point",
    "q=round_half_away_from_zero(x/scale)+zero_point",
    "q=round(x/quant_scale)+quant_zero_point",
}
DEQUANT_FORMULA_ALIASES = {
    "affine_zero_point",
    "x_hat=(q-zero_point)*scale",
    "x_hat=(q-quant_zero_point)*quant_scale",
    "xhat=(q-zp)*scale",
}
ROUNDING_ALIASES = {
    "half_away_from_zero": "half_away_from_zero",
    "round_half_away_from_zero": "half_away_from_zero",
    "nearest_away_from_zero": "half_away_from_zero",
    "half_to_even": "half_to_even",
    "round_half_to_even": "half_to_even",
    "bankers": "half_to_even",
}
REDUCTION_ALIASES = {
    "canonical_block_order_exact",
    "ordered_block_concat_exact",
    "exact_block_order",
    "block_order",
}


def _formula_token(value: str) -> str:
    return "".join(value.lower().split())


def _require_int(raw: dict[str, Any], key: str, *, context: str) -> int:
    if key not in raw:
        raise BusinessError(
            "E_PARAM_INVALID", f"missing required field {key}", context={"source": context}
        )
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise BusinessError(
            "E_PARAM_INVALID",
            f"field {key} must be an integer",
            context={"source": context, "value": value},
        )
    return value


def _require_str(raw: dict[str, Any], key: str, *, context: str) -> str:
    if key not in raw:
        raise BusinessError(
            "E_PARAM_INVALID", f"missing required field {key}", context={"source": context}
        )
    value = raw[key]
    if not isinstance(value, str) or not value:
        raise BusinessError(
            "E_PARAM_INVALID",
            f"field {key} must be a non-empty string",
            context={"source": context, "value": value},
        )
    return value


def parse_block_plan(value: Any, feature_dim: int) -> tuple[tuple[int, int], ...]:
    """Parse either boundary form [0,3,6,8] or explicit ranges [[0,3],[3,6]]. """

    if isinstance(value, dict):
        value = value.get("feature_blocks", value.get("blocks"))
    if not isinstance(value, list) or not value:
        raise BusinessError(
            "E_PARAM_INVALID",
            "block_plan must be a non-empty boundary list or an explicit block list",
            context={"block_plan": value, "feature_dim": feature_dim},
        )
    blocks: list[tuple[int, int]]
    if all(isinstance(item, int) and not isinstance(item, bool) for item in value):
        boundaries = [int(item) for item in value]
        if boundaries[0] != 0 or boundaries[-1] != feature_dim:
            raise BusinessError(
                "E_PARAM_INVALID",
                "block_plan boundaries must start at 0 and end at feature_dim",
                context={"block_plan": value, "feature_dim": feature_dim},
            )
        if any(right <= left for left, right in zip(boundaries, boundaries[1:])):
            raise BusinessError(
                "E_PARAM_INVALID",
                "block_plan boundaries must be strictly increasing",
                context={"block_plan": value},
            )
        blocks = list(zip(boundaries, boundaries[1:]))
    else:
        blocks = []
        for item in value:
            if isinstance(item, dict):
                start, stop = item.get("start"), item.get("stop")
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                start, stop = item
            else:
                raise BusinessError(
                    "E_PARAM_INVALID",
                    "each block must be [start, stop) or {start, stop}",
                    context={"block_plan": value, "item": item},
                )
            if (
                isinstance(start, bool)
                or isinstance(stop, bool)
                or not isinstance(start, int)
                or not isinstance(stop, int)
                or start < 0
                or stop <= start
            ):
                raise BusinessError(
                    "E_PARAM_INVALID",
                    "block boundaries must be ordered non-negative integers",
                    context={"block_plan": value, "item": item},
                )
            blocks.append((start, stop))
        expected = 0
        for start, stop in blocks:
            if start != expected:
                raise BusinessError(
                    "E_PARAM_INVALID",
                    "explicit block_plan must partition [0, feature_dim) without gaps",
                    context={"block_plan": value, "expected_start": expected},
                )
            expected = stop
        if expected != feature_dim:
            raise BusinessError(
                "E_PARAM_INVALID",
                "explicit block_plan must end at feature_dim",
                context={"block_plan": value, "feature_dim": feature_dim},
            )
    return tuple(blocks)


@dataclass(frozen=True)
class Tolerance:
    abs_max: float
    rel_max: float
    relative_floor: float = 1e-12

    @classmethod
    def parse(cls, value: Any, fallback: "Tolerance | None" = None) -> "Tolerance":
        if value is None:
            return fallback or cls(0.5, 0.1, 1e-12)
        if not isinstance(value, dict):
            raise BusinessError("E_PARAM_INVALID", "tolerance must be an object")
        try:
            abs_max = float(value["abs_max"])
            rel_max = float(value["rel_max"])
            relative_floor = float(value.get("relative_floor", 1e-12))
        except (KeyError, TypeError, ValueError) as exc:
            raise BusinessError(
                "E_PARAM_INVALID", "tolerance requires numeric abs_max and rel_max"
            ) from exc
        if not all(math.isfinite(item) and item >= 0 for item in (abs_max, rel_max, relative_floor)):
            raise BusinessError(
                "E_PARAM_INVALID",
                "tolerance values must be finite and non-negative",
                context=value,
            )
        return cls(abs_max, rel_max, relative_floor)

    def to_dict(self) -> dict[str, float]:
        return {
            "abs_max": self.abs_max,
            "rel_max": self.rel_max,
            "relative_floor": self.relative_floor,
        }


@dataclass(frozen=True)
class Task:
    task_id: str
    feature_dim: int
    quant_scale: float
    quant_zero_point: int
    block_plan: tuple[tuple[int, int], ...]
    revision: int
    logical_clock_ms: int
    feature_ids: tuple[str, ...]
    quant_formula: str
    dequant_formula: str
    rounding_mode: str
    normalization_method: str
    normalization_ddof: int
    normalization_epsilon: float
    reduction_rule: str
    tolerance: Tolerance
    feature_seed: int | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    def identity(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "feature_dim": self.feature_dim,
            "quant_scale": self.quant_scale,
            "quant_zero_point": self.quant_zero_point,
            "block_plan": self.block_plan,
            "revision": self.revision,
            "logical_clock_ms": self.logical_clock_ms,
            "feature_ids": self.feature_ids,
            "quant_formula": self.quant_formula,
            "dequant_formula": self.dequant_formula,
            "rounding_mode": self.rounding_mode,
            "normalization": {
                "method": self.normalization_method,
                "ddof": self.normalization_ddof,
                "epsilon": self.normalization_epsilon,
            },
            "reduction_rule": self.reduction_rule,
            "tolerance": self.tolerance.to_dict(),
            "feature_seed": self.feature_seed,
        }

    def quant_config_digest(self) -> str:
        return sha256_json(
            {
                "scale": self.quant_scale,
                "zero_point": self.quant_zero_point,
                "quant_formula": self.quant_formula,
                "dequant_formula": self.dequant_formula,
                "rounding_mode": self.rounding_mode,
            }
        )

    def block_config_digest(self) -> str:
        return sha256_json({"block_plan": self.block_plan, "reduction_rule": self.reduction_rule})

    def normalization_digest(self) -> str:
        return sha256_json(
            {
                "method": self.normalization_method,
                "ddof": self.normalization_ddof,
                "epsilon": self.normalization_epsilon,
            }
        )


@dataclass(frozen=True)
class Batch:
    batch_id: str
    task_id: str
    sample_count: int
    feature_seed: int
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    def identity(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "task_id": self.task_id,
            "sample_count": self.sample_count,
            "feature_seed": self.feature_seed,
        }


@dataclass(frozen=True)
class RunConfig:
    master_seed: int
    tolerance: Tolerance
    reduction_rule: str
    schema: str = "featurediag.config.v1"

    @classmethod
    def load(cls, path: Path) -> "RunConfig":
        try:
            raw = read_json(path)
        except (OSError, json.JSONDecodeError) as exc:
            raise BusinessError(
                "E_FORMAT_CORRUPT", f"cannot read config {path}: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise BusinessError("E_FORMAT_CORRUPT", "config must be a JSON object")
        master_seed = raw.get("master_seed", 0)
        if isinstance(master_seed, bool) or not isinstance(master_seed, int):
            raise BusinessError("E_PARAM_INVALID", "config.master_seed must be an integer")
        tolerance = Tolerance.parse(raw.get("tolerance"))
        reduction_rule = str(raw.get("reduction_rule", "canonical_block_order_exact"))
        return cls(master_seed, tolerance, reduction_rule)


def parse_task(raw: dict[str, Any], *, source: str, default_tolerance: Tolerance) -> Task:
    if not isinstance(raw, dict):
        raise BusinessError("E_PARAM_INVALID", "task line must be a JSON object", context={"source": source})
    task_id = _require_str(raw, "task_id", context=source)
    feature_dim = _require_int(raw, "feature_dim", context=source)
    if feature_dim <= 0:
        raise BusinessError("E_PARAM_INVALID", "feature_dim must be positive", task_id=task_id)
    try:
        quant_scale = float(raw["quant_scale"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BusinessError(
            "E_PARAM_INVALID", "quant_scale must be numeric", task_id=task_id
        ) from exc
    if not math.isfinite(quant_scale) or quant_scale <= 0:
        raise BusinessError(
            "E_PARAM_INVALID",
            "quant_scale must be finite and positive",
            task_id=task_id,
            context={"quant_scale": raw.get("quant_scale")},
        )
    quant_zero_point = _require_int(raw, "quant_zero_point", context=source)
    revision = _require_int(raw, "revision", context=source)
    logical_clock_ms = _require_int(raw, "logical_clock_ms", context=source)
    if revision < 0 or logical_clock_ms < 0:
        raise BusinessError(
            "E_PARAM_INVALID", "revision and logical_clock_ms must be non-negative", task_id=task_id
        )
    block_plan = parse_block_plan(raw.get("block_plan"), feature_dim)
    feature_ids_raw = raw.get("feature_ids")
    if feature_ids_raw is None:
        feature_ids = tuple(f"{task_id}:f{index}" for index in range(feature_dim))
    else:
        if (
            not isinstance(feature_ids_raw, list)
            or len(feature_ids_raw) != feature_dim
            or any(not isinstance(item, str) or not item for item in feature_ids_raw)
        ):
            raise BusinessError(
                "E_PARAM_INVALID",
                "feature_ids must be a non-empty string list of length feature_dim",
                task_id=task_id,
            )
        if len(set(feature_ids_raw)) != len(feature_ids_raw):
            raise BusinessError("E_PARAM_INVALID", "feature_ids must be unique", task_id=task_id)
        feature_ids = tuple(feature_ids_raw)

    quant_formula_raw = raw.get("quant_formula", raw.get("quantization_formula"))
    dequant_formula_raw = raw.get("dequant_formula", raw.get("dequantization_formula"))
    rounding_raw = raw.get("rounding_mode", raw.get("rounding"))
    if not isinstance(quant_formula_raw, str) or not isinstance(dequant_formula_raw, str):
        raise BusinessError(
            "E_PARAM_INVALID",
            "quant_formula and dequant_formula must be explicitly declared strings",
            task_id=task_id,
        )
    if _formula_token(quant_formula_raw) not in {_formula_token(x) for x in QUANT_FORMULA_ALIASES}:
        raise BusinessError(
            "E_PARAM_INVALID",
            "unsupported explicit quant_formula",
            task_id=task_id,
            context={"quant_formula": quant_formula_raw},
        )
    if _formula_token(dequant_formula_raw) not in {_formula_token(x) for x in DEQUANT_FORMULA_ALIASES}:
        raise BusinessError(
            "E_PARAM_INVALID",
            "unsupported explicit dequant_formula",
            task_id=task_id,
            context={"dequant_formula": dequant_formula_raw},
        )
    if not isinstance(rounding_raw, str) or rounding_raw not in ROUNDING_ALIASES:
        raise BusinessError(
            "E_PARAM_INVALID",
            "rounding_mode must be explicitly declared and supported",
            task_id=task_id,
            context={"rounding_mode": rounding_raw},
        )
    rounding_mode = ROUNDING_ALIASES[rounding_raw]

    normalization_raw = raw.get("normalization", "zscore")
    if isinstance(normalization_raw, str):
        normalization = {"method": normalization_raw}
    elif isinstance(normalization_raw, dict):
        normalization = normalization_raw
    else:
        raise BusinessError(
            "E_PARAM_INVALID", "normalization must be a method or object", task_id=task_id
        )
    normalization_method = str(normalization.get("method", "zscore")).lower()
    if normalization_method != "zscore":
        raise BusinessError(
            "E_PARAM_INVALID",
            "only explicitly declared zscore normalization is supported",
            task_id=task_id,
        )
    normalization_ddof = normalization.get("ddof", 0)
    if isinstance(normalization_ddof, bool) or normalization_ddof not in (0, 1):
        raise BusinessError(
            "E_PARAM_INVALID", "normalization.ddof must be 0 or 1", task_id=task_id
        )
    try:
        normalization_epsilon = float(normalization.get("epsilon", 1e-12))
    except (TypeError, ValueError) as exc:
        raise BusinessError(
            "E_PARAM_INVALID", "normalization.epsilon must be numeric", task_id=task_id
        ) from exc
    if not math.isfinite(normalization_epsilon) or normalization_epsilon <= 0:
        raise BusinessError(
            "E_PARAM_INVALID", "normalization.epsilon must be finite and positive", task_id=task_id
        )

    reduction_rule = raw.get("reduction_rule")
    if reduction_rule is None:
        reduction_rule = "canonical_block_order_exact"
    if not isinstance(reduction_rule, str) or reduction_rule not in REDUCTION_ALIASES:
        raise BusinessError(
            "E_PARAM_INVALID",
            "reduction_rule must be explicitly supported",
            task_id=task_id,
            context={"reduction_rule": reduction_rule},
        )
    tolerance = Tolerance.parse(raw.get("tolerance"), default_tolerance)
    feature_seed = raw.get("feature_seed")
    if feature_seed is not None and (
        isinstance(feature_seed, bool) or not isinstance(feature_seed, int) or feature_seed < 0
    ):
        raise BusinessError(
            "E_PARAM_INVALID", "feature_seed must be a non-negative integer", task_id=task_id
        )

    return Task(
        task_id=task_id,
        feature_dim=feature_dim,
        quant_scale=quant_scale,
        quant_zero_point=quant_zero_point,
        block_plan=block_plan,
        revision=revision,
        logical_clock_ms=logical_clock_ms,
        feature_ids=feature_ids,
        quant_formula=quant_formula_raw,
        dequant_formula=dequant_formula_raw,
        rounding_mode=rounding_mode,
        normalization_method=normalization_method,
        normalization_ddof=int(normalization_ddof),
        normalization_epsilon=normalization_epsilon,
        reduction_rule=reduction_rule,
        tolerance=tolerance,
        feature_seed=feature_seed,
        raw=raw,
    )


def load_tasks(
    path: Path,
    *,
    default_tolerance: Tolerance | None = None,
    validate_clock_monotonic: bool = True,
) -> dict[str, Task]:
    fallback = default_tolerance or Tolerance(0.5, 0.1, 1e-12)
    try:
        rows = read_jsonl(path)
    except (OSError, ValueError) as exc:
        raise BusinessError("E_FORMAT_CORRUPT", f"cannot read task manifest {path}: {exc}") from exc
    tasks: dict[str, Task] = {}
    seen_sources: dict[str, str] = {}
    previous_clock: int | None = None
    for line_number, raw in enumerate(rows, 1):
        try:
            task = parse_task(raw, source=f"{path}:{line_number}", default_tolerance=fallback)
        except BusinessError:
            raise
        if task.task_id in tasks:
            raise BusinessError(
                "E_DUP_TASK",
                "duplicate task_id in task manifest",
                task_id=task.task_id,
                context={
                    "source": f"{path}:{line_number}",
                    "first_source": seen_sources.get(task.task_id),
                },
            )
        if validate_clock_monotonic and previous_clock is not None and task.logical_clock_ms < previous_clock:
            raise BusinessError(
                "E_CLOCK_ROLLBACK",
                "logical clock moved backwards in task manifest",
                task_id=task.task_id,
                context={
                    "previous_clock_ms": previous_clock,
                    "logical_clock_ms": task.logical_clock_ms,
                    "source": f"{path}:{line_number}",
                },
            )
        previous_clock = task.logical_clock_ms
        seen_sources[task.task_id] = f"{path}:{line_number}"
        tasks[task.task_id] = task
    if not tasks:
        raise BusinessError("E_PARAM_INVALID", "task manifest is empty")
    return tasks


def load_batches(path: Path, tasks: dict[str, Task]) -> list[Batch]:
    try:
        rows = read_jsonl(path)
    except (OSError, ValueError) as exc:
        raise BusinessError("E_BAD_BATCH", f"cannot read batch stream {path}: {exc}") from exc
    batches: list[Batch] = []
    seen: dict[str, int] = {}
    for line_number, raw in enumerate(rows, 1):
        if not isinstance(raw, dict):
            raise BusinessError(
                "E_BAD_BATCH",
                "batch line must be an object",
                context={"source": f"{path}:{line_number}"},
            )
        batch_id_raw = raw.get("batch_id")
        task_id_raw = raw.get("task_id")
        if batch_id_raw is None or task_id_raw is None:
            raise BusinessError(
                "E_BAD_BATCH",
                "batch requires both batch_id and task_id",
                batch_id=str(batch_id_raw) if batch_id_raw is not None else None,
                task_id=str(task_id_raw) if task_id_raw is not None else None,
                context={"source": f"{path}:{line_number}"},
            )
        if not isinstance(batch_id_raw, str) or not batch_id_raw:
            raise BusinessError(
                "E_BAD_BATCH",
                "batch_id must be a non-empty string",
                batch_id=str(batch_id_raw),
                context={"source": f"{path}:{line_number}"},
            )
        batch_id = batch_id_raw
        task_id = str(task_id_raw)
        if batch_id in seen:
            raise BusinessError(
                "E_BAD_BATCH",
                "duplicate batch_id",
                batch_id=batch_id,
                context={
                    "source": f"{path}:{line_number}",
                    "first_line": seen[batch_id],
                },
            )
        if task_id not in tasks:
            raise BusinessError(
                "E_BAD_BATCH",
                "batch references unknown task_id",
                batch_id=batch_id,
                task_id=task_id,
                context={"source": f"{path}:{line_number}"},
            )
        sample_count = raw.get("sample_count")
        if (
            isinstance(sample_count, bool)
            or not isinstance(sample_count, int)
            or sample_count <= 0
        ):
            raise BusinessError(
                "E_BAD_BATCH",
                "sample_count must be a positive integer",
                batch_id=batch_id,
                task_id=task_id,
            )
        feature_seed = raw.get("feature_seed")
        if (
            isinstance(feature_seed, bool)
            or not isinstance(feature_seed, int)
            or feature_seed < 0
        ):
            raise BusinessError(
                "E_BAD_BATCH",
                "feature_seed must be a non-negative integer",
                batch_id=batch_id,
                task_id=task_id,
            )
        seen[batch_id] = line_number
        batches.append(Batch(batch_id, task_id, sample_count, feature_seed, raw))
    if not batches:
        raise BusinessError("E_BAD_BATCH", "batch stream is empty")
    return sorted(batches, key=lambda batch: batch.batch_id)


def validate_task_against_registry(
    tasks: dict[str, Task], registry_path: Path, *, update: bool = False
) -> dict[str, Any]:
    historical: dict[str, Any] = {}
    if registry_path.exists():
        try:
            historical = read_json(registry_path)
        except (OSError, json.JSONDecodeError) as exc:
            raise BusinessError(
                "E_FORMAT_CORRUPT", f"cannot read task registry {registry_path}: {exc}"
            ) from exc
    report: dict[str, Any] = {"checked": [], "updated": []}
    new_registry = dict(historical)
    for task_id, task in tasks.items():
        old = historical.get(task_id)
        if old is not None:
            old_revision = int(old.get("revision", -1))
            old_clock = int(old.get("logical_clock_ms", -1))
            if task.revision < old_revision:
                raise BusinessError(
                    "E_REVISION_ROLLBACK",
                    "task revision moved backwards",
                    task_id=task_id,
                    context={
                        "previous_revision": old_revision,
                        "revision": task.revision,
                    },
                )
            if task.logical_clock_ms < old_clock:
                raise BusinessError(
                    "E_CLOCK_ROLLBACK",
                    "task logical clock moved backwards",
                    task_id=task_id,
                    context={
                        "previous_logical_clock_ms": old_clock,
                        "logical_clock_ms": task.logical_clock_ms,
                    },
                )
            report["checked"].append(task_id)
        new_registry[task_id] = {
            "revision": task.revision,
            "logical_clock_ms": task.logical_clock_ms,
            "feature_dim": task.feature_dim,
            "feature_ids": list(task.feature_ids),
        }
        report["updated"].append(task_id)
    if update:
        from .canonical import atomic_write_json

        atomic_write_json(registry_path, new_registry)
    report["registry_path"] = str(registry_path)
    return report
