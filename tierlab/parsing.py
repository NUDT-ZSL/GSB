from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from .errors import BusinessError, ErrorCode
from .models import (
    COLD,
    HOT,
    AdaptiveConfig,
    BackoffConfig,
    Manifest,
    SamplingConfig,
    StorageObject,
    TierConfig,
    first_present,
    require_bool,
    require_decimal,
    require_int,
    require_nonnegative_int,
    require_object,
    require_positive_int,
)


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BusinessError(
                ErrorCode.DUPLICATE_JSON_KEY,
                "JSON object contains a duplicate key",
                {"key": key},
            )
        result[key] = value
    return result


def read_json(path: str | Path, label: str) -> Any:
    source = Path(path)
    try:
        with source.open("r", encoding="utf-8") as handle:
            return json.load(
                handle,
                parse_float=Decimal,
                object_pairs_hook=_object_pairs,
            )
    except BusinessError:
        raise
    except FileNotFoundError as exc:
        raise BusinessError(
            ErrorCode.INVALID_JSON,
            f"{label} file does not exist",
            {"path": str(source)},
        ) from exc
    except json.JSONDecodeError as exc:
        raise BusinessError(
            ErrorCode.INVALID_JSON,
            f"{label} is not valid JSON",
            {"path": str(source), "line": exc.lineno, "column": exc.colno},
        ) from exc
    except OSError as exc:
        raise BusinessError(
            ErrorCode.INVALID_JSON,
            f"{label} cannot be read",
            {"path": str(source), "error": str(exc)},
        ) from exc


def _layer(value: Any, where: str) -> str:
    if not isinstance(value, str) or value not in (HOT, COLD):
        raise BusinessError(
            ErrorCode.INVALID_LAYER,
            f"{where} must be 'hot' or 'cold'",
            {"value": value},
        )
    return value


def parse_manifest(path: str | Path) -> Manifest:
    raw = read_json(path, "manifest")
    root = require_object(raw, "manifest")
    bucket = root.get("bucket", root.get("bucket_id", root.get("bucket_name")))
    if not isinstance(bucket, str) or not bucket.strip():
        raise BusinessError(
            ErrorCode.INVALID_BUCKET,
            "manifest.bucket must be a non-empty string",
        )
    raw_objects = root.get("objects", root.get("items"))
    if not isinstance(raw_objects, list):
        raise BusinessError(
            ErrorCode.INVALID_TYPE,
            "manifest.objects must be an array",
        )
    if not raw_objects:
        raise BusinessError(
            ErrorCode.INVALID_FIELD,
            "manifest.objects must not be empty",
        )

    seen: set[str] = set()
    objects: list[StorageObject] = []
    for index, raw_item in enumerate(raw_objects):
        where = f"manifest.objects[{index}]"
        item = require_object(raw_item, where)
        key = first_present(item, ("key", "object_key", "object_id"), where)
        if not isinstance(key, str) or not key:
            raise BusinessError(
                ErrorCode.INVALID_OBJECT_KEY,
                f"{where}.key must be a non-empty string",
            )
        if key in seen:
            raise BusinessError(
                ErrorCode.DUPLICATE_OBJECT_KEY,
                "object keys must be unique",
                {"key": key},
            )
        seen.add(key)
        size = require_positive_int(
            first_present(
                item,
                ("size_bytes", "byte_size", "bytes", "size"),
                where,
            ),
            f"{where}.size_bytes",
            ErrorCode.INVALID_SIZE,
        )
        last_access = require_nonnegative_int(
            first_present(
                item,
                (
                    "last_access_clock",
                    "last_access_logical_clock",
                    "last_access_logic_clock",
                    "last_access_clock_value",
                    "last_access",
                ),
                where,
            ),
            f"{where}.last_access_clock",
            ErrorCode.INVALID_ACCESS_CLOCK,
        )
        access_count = require_nonnegative_int(
            first_present(item, ("access_count", "accesses"), where),
            f"{where}.access_count",
            ErrorCode.INVALID_ACCESS_COUNT,
        )
        heat_weight = require_decimal(
            first_present(
                item,
                ("heat_weight", "weight"),
                where,
            ),
            f"{where}.heat_weight",
            ErrorCode.INVALID_HEAT_WEIGHT,
        )
        if heat_weight < 0:
            raise BusinessError(
                ErrorCode.INVALID_HEAT_WEIGHT,
                f"{where}.heat_weight must be >= 0",
                {"value": str(heat_weight)},
            )
        declared_layer = _layer(
            first_present(
                item,
                ("declared_tier", "declared_layer", "tier", "layer"),
                where,
            ),
            f"{where}.declared_tier",
        )
        migratable = require_bool(
            first_present(
                item,
                (
                    "migratable",
                    "allow_migration",
                    "allow_migrate",
                    "migration_allowed",
                ),
                where,
            ),
            f"{where}.migratable",
        )
        objects.append(
            StorageObject(
                bucket=bucket,
                key=key,
                size_bytes=size,
                last_access_clock=last_access,
                access_count=access_count,
                heat_weight=heat_weight,
                declared_layer=declared_layer,
                migratable=migratable,
                current_layer=declared_layer,
            )
        )
    return Manifest(bucket=bucket, objects=tuple(objects))


def parse_config(path: str | Path) -> TierConfig:
    raw = read_json(path, "config")
    return config_from_dict(raw)


def config_from_dict(raw: Any) -> TierConfig:
    root = require_object(raw, "config")

    hot_capacity = require_positive_int(
        first_present(
            root,
            (
                "hot_capacity_bytes",
                "hot_capacity",
                "hot_capacity_limit",
                "hot_capacity_limit_bytes",
                "hot_limit_bytes",
            ),
            "config",
        ),
        "config.hot_capacity_bytes",
        ErrorCode.INVALID_CAPACITY,
    )
    cold_capacity = require_positive_int(
        first_present(
            root,
            (
                "cold_capacity_bytes",
                "cold_capacity",
                "cold_capacity_limit",
                "cold_capacity_limit_bytes",
                "cold_limit_bytes",
            ),
            "config",
        ),
        "config.cold_capacity_bytes",
        ErrorCode.INVALID_CAPACITY,
    )
    round_budget = require_positive_int(
        first_present(
            root,
            (
                "round_byte_budget",
                "per_round_byte_budget",
                "migration_byte_budget",
                "byte_budget",
                "round_budget",
                "budget_bytes",
            ),
            "config",
        ),
        "config.round_byte_budget",
        ErrorCode.INVALID_BUDGET,
    )
    concurrency = require_positive_int(
        first_present(
            root,
            (
                "migration_concurrency",
                "concurrency",
                "max_concurrency",
                "concurrency_limit",
                "max_parallel_migrations",
            ),
            "config",
        ),
        "config.migration_concurrency",
        ErrorCode.INVALID_CONCURRENCY,
    )
    threshold = require_decimal(
        first_present(root, ("hot_threshold", "heat_threshold"), "config"),
        "config.hot_threshold",
        ErrorCode.INVALID_THRESHOLD,
    )
    if threshold < 0:
        raise BusinessError(
            ErrorCode.INVALID_THRESHOLD,
            "config.hot_threshold must be >= 0",
        )
    tolerance = require_nonnegative_int(
        root.get(
            "clock_tolerance",
            root.get(
                "logic_clock_tolerance",
                root.get(
                    "logical_clock_tolerance",
                    root.get("rollback_tolerance", 0),
                ),
            ),
        ),
        "config.clock_tolerance",
        ErrorCode.INVALID_TOLERANCE,
    )
    mode = root.get("mode", "auto")
    if mode not in ("auto", "exact", "sampled"):
        raise BusinessError(
            ErrorCode.INVALID_MODE,
            "config.mode must be auto, exact, or sampled",
            {"value": mode},
        )

    backoff_value = root.get("backoff", root.get("retry_backoff"))
    if backoff_value is None:
        backoff_raw: dict[str, Any] = {}
        for target, names in (
            ("base_delay_clocks", ("backoff_base_clocks", "retry_base_clocks")),
            ("max_delay_clocks", ("backoff_max_clocks", "retry_max_clocks")),
            ("multiplier", ("backoff_multiplier", "retry_multiplier")),
            (
                "max_consecutive_failures",
                ("max_consecutive_failures", "failure_limit"),
            ),
        ):
            for name in names:
                if name in root:
                    backoff_raw[target] = root[name]
                    break
    else:
        backoff_raw = require_object(backoff_value, "config.backoff")
    base_delay = require_positive_int(
        first_present(
            backoff_raw,
            ("base_delay_clocks", "base_clocks", "base_delay", "base"),
            "config.backoff",
        ),
        "config.backoff.base_delay_clocks",
        ErrorCode.INVALID_BACKOFF,
    )
    max_delay = require_positive_int(
        first_present(
            backoff_raw,
            ("max_delay_clocks", "max_clocks", "max_delay", "max"),
            "config.backoff",
        ),
        "config.backoff.max_delay_clocks",
        ErrorCode.INVALID_BACKOFF,
    )
    multiplier = require_positive_int(
        first_present(
            backoff_raw,
            ("multiplier", "backoff_multiplier", "factor"),
            "config.backoff",
        ),
        "config.backoff.multiplier",
        ErrorCode.INVALID_BACKOFF,
    )
    max_failures = require_positive_int(
        first_present(
            backoff_raw,
            (
                "max_consecutive_failures",
                "max_failures",
                "max_retries",
                "failure_limit",
            ),
            "config.backoff",
        ),
        "config.backoff.max_consecutive_failures",
        ErrorCode.INVALID_BACKOFF,
    )
    if max_delay < base_delay:
        raise BusinessError(
            ErrorCode.INVALID_BACKOFF,
            "config.backoff.max_delay_clocks must be >= base_delay_clocks",
        )

    sampling_value = root.get("sampling", root.get("sample"))
    if sampling_value is None:
        sampling_raw = {
            "ratio": root.get("sampling_ratio", root.get("sample_ratio", "0.1")),
            "confidence_z": root.get("confidence_z", "1.96"),
            "min_sample_per_stratum": root.get(
                "min_sample_per_stratum",
                2,
            ),
            "random_seed": root.get("random_seed", 0),
        }
    else:
        sampling_raw = require_object(sampling_value, "config.sampling")
    ratio = require_decimal(
        sampling_raw.get(
            "ratio",
            sampling_raw.get(
                "sample_ratio",
                sampling_raw.get("fraction", "0.1"),
            ),
        ),
        "config.sampling.ratio",
        ErrorCode.INVALID_SAMPLING,
    )
    if ratio <= 0 or ratio > 1:
        raise BusinessError(
            ErrorCode.INVALID_SAMPLING,
            "config.sampling.ratio must be in (0, 1]",
            {"value": str(ratio)},
        )
    confidence_z = require_decimal(
        sampling_raw.get(
            "confidence_z",
            sampling_raw.get("z_score", sampling_raw.get("z", "1.96")),
        ),
        "config.sampling.confidence_z",
        ErrorCode.INVALID_SAMPLING,
    )
    if confidence_z <= 0:
        raise BusinessError(
            ErrorCode.INVALID_SAMPLING,
            "config.sampling.confidence_z must be > 0",
        )
    min_sample = require_positive_int(
        sampling_raw.get(
            "min_sample_per_stratum",
            sampling_raw.get("min_sample", 2),
        ),
        "config.sampling.min_sample_per_stratum",
        ErrorCode.INVALID_SAMPLING,
    )
    random_seed = require_int(
        sampling_raw.get("random_seed", sampling_raw.get("seed", 0)),
        "config.sampling.random_seed",
    )

    adaptive_value = root.get("adaptive", root.get("mode_adaptation"))
    if adaptive_value is None:
        adaptive_raw = {}
    else:
        adaptive_raw = require_object(adaptive_value, "config.adaptive")
    exact_max_objects = require_positive_int(
        adaptive_raw.get(
            "exact_max_objects",
            adaptive_raw.get(
                "max_exact_objects",
                adaptive_raw.get(
                    "exact_object_threshold",
                    root.get("exact_max_objects", 1000),
                ),
            ),
        ),
        "config.adaptive.exact_max_objects",
        ErrorCode.INVALID_ADAPTIVE,
    )
    exact_max_candidates = require_positive_int(
        adaptive_raw.get(
            "exact_max_candidates",
            adaptive_raw.get(
                "max_exact_candidates",
                adaptive_raw.get(
                    "exact_candidate_threshold",
                    root.get("exact_max_candidates", 500),
                ),
            ),
        ),
        "config.adaptive.exact_max_candidates",
        ErrorCode.INVALID_ADAPTIVE,
    )
    max_error_ratio = require_decimal(
        adaptive_raw.get(
            "max_estimated_error_ratio",
            adaptive_raw.get(
                "max_error_ratio",
                root.get("max_estimated_error_ratio", Decimal("0.2")),
            ),
        ),
        "config.adaptive.max_estimated_error_ratio",
        ErrorCode.INVALID_ADAPTIVE,
    )
    if max_error_ratio < 0:
        raise BusinessError(
            ErrorCode.INVALID_ADAPTIVE,
            "config.adaptive.max_estimated_error_ratio must be >= 0",
        )

    def cost(name: str, default: str) -> Decimal:
        value = require_decimal(
            adaptive_raw.get(name, root.get(name, Decimal(default))),
            f"config.adaptive.{name}",
            ErrorCode.INVALID_ADAPTIVE,
        )
        if value < 0:
            raise BusinessError(
                ErrorCode.INVALID_ADAPTIVE,
                f"config.adaptive.{name} must be >= 0",
            )
        return value

    return TierConfig(
        hot_capacity_bytes=hot_capacity,
        cold_capacity_bytes=cold_capacity,
        round_byte_budget=round_budget,
        migration_concurrency=concurrency,
        hot_threshold=threshold,
        clock_tolerance=tolerance,
        backoff=BackoffConfig(
            base_delay_clocks=base_delay,
            max_delay_clocks=max_delay,
            multiplier=multiplier,
            max_consecutive_failures=max_failures,
        ),
        sampling=SamplingConfig(
            ratio=ratio,
            confidence_z=confidence_z,
            min_sample_per_stratum=min_sample,
            random_seed=random_seed,
        ),
        adaptive=AdaptiveConfig(
            exact_max_objects=exact_max_objects,
            exact_max_candidates=exact_max_candidates,
            max_estimated_error_ratio=max_error_ratio,
            exact_scan_unit_cost=cost("exact_scan_unit_cost", "1"),
            exact_candidate_unit_cost=cost("exact_candidate_unit_cost", "0.05"),
            sampled_index_unit_cost=cost("sampled_index_unit_cost", "0.1"),
            sampled_object_unit_cost=cost("sampled_object_unit_cost", "0.2"),
        ),
        mode=mode,
    )


def parse_clock_table(path: str | Path) -> list[dict[str, int]]:
    raw = read_json(path, "clock table")
    steps_raw: Any
    if isinstance(raw, list):
        steps_raw = raw
    elif isinstance(raw, dict):
        steps_raw = raw.get("steps")
    else:
        steps_raw = None
    if not isinstance(steps_raw, list) or not steps_raw:
        raise BusinessError(
            ErrorCode.INVALID_CLOCK_TABLE,
            "clock table must contain a non-empty steps array",
        )
    result: list[dict[str, int]] = []
    for index, item in enumerate(steps_raw):
        if type(item) is int:
            step_number = index + 1
            clock = item
        elif isinstance(item, dict):
            step_value = item.get("step", index + 1)
            if type(step_value) is not int:
                raise BusinessError(
                    ErrorCode.INVALID_CLOCK_TABLE,
                    f"clock steps[{index}].step must be an integer",
                )
            step_number = step_value
            clock = item.get("clock", item.get("logical_clock"))
        else:
            raise BusinessError(
                ErrorCode.INVALID_CLOCK_TABLE,
                f"clock steps[{index}] must be an integer or object",
            )
        if type(clock) is not int or clock < 0:
            raise BusinessError(
                ErrorCode.INVALID_CLOCK_TABLE,
                f"clock steps[{index}].clock must be a non-negative integer",
            )
        result.append({"step": step_number, "clock": clock})
    if len({item["step"] for item in result}) != len(result):
        raise BusinessError(
            ErrorCode.INVALID_CLOCK_TABLE,
            "clock table step numbers must be unique",
        )
    return result


def resolve_clock(
    clock: int | None,
    clock_table: str | Path | None,
    clock_step: int | None,
) -> int:
    if clock is not None and clock_table is not None:
        raise BusinessError(
            ErrorCode.INVALID_CLOCK,
            "use either --clock or --clock-table, not both",
        )
    if clock is None and clock_table is None:
        raise BusinessError(
            ErrorCode.INVALID_CLOCK,
            "an injected --clock or --clock-table is required",
        )
    if clock is not None:
        if type(clock) is not int or clock < 0:
            raise BusinessError(
                ErrorCode.INVALID_CLOCK,
                "--clock must be a non-negative integer",
            )
        return clock
    steps = parse_clock_table(clock_table)  # type: ignore[arg-type]
    selected = 1 if clock_step is None else clock_step
    for item in steps:
        if item["step"] == selected:
            return item["clock"]
    raise BusinessError(
        ErrorCode.INVALID_CLOCK_TABLE,
        "requested clock step does not exist",
        {"step": selected, "available_steps": [item["step"] for item in steps]},
    )
