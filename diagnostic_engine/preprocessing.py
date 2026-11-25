"""Stage 1: deterministic windowing, completion and window statistics."""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .errors import bad_input
from .precision import Precision


REQUIRED_RECORD_FIELDS = ("v_idx", "src_name", "obs_time_ms", "values", "q_param")


def validate_records(records: list[dict[str, Any]], config: dict[str, Any]) -> None:
    known_variables = set(config.get("variables", []))
    seen_v_idx: set[int] = set()
    for position, record in enumerate(records):
        if not isinstance(record, dict):
            raise bad_input(f"record at position {position} is not an object", position=position)
        missing_fields = [field for field in REQUIRED_RECORD_FIELDS if field not in record]
        if missing_fields:
            raise bad_input("record is missing required fields", position=position, fields=missing_fields)
        v_idx = record["v_idx"]
        if not isinstance(v_idx, int) or isinstance(v_idx, bool):
            raise bad_input("v_idx must be an integer", position=position, v_idx=v_idx)
        if v_idx in seen_v_idx:
            raise bad_input("duplicate v_idx", v_idx=v_idx)
        seen_v_idx.add(v_idx)
        if record["src_name"] not in known_variables:
            raise bad_input(
                "record src_name is not declared in scenario variables",
                v_idx=v_idx,
                src_name=record["src_name"],
                known=sorted(known_variables),
            )
        obs_time = record["obs_time_ms"]
        if not isinstance(obs_time, int) or isinstance(obs_time, bool) or obs_time < 0:
            raise bad_input("obs_time_ms must be a non-negative integer", v_idx=v_idx, obs_time_ms=obs_time)
        values = record["values"]
        if not isinstance(values, list) or not values or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in values):
            raise bad_input("values must be a non-empty finite numeric array", v_idx=v_idx)
        q_param = record["q_param"]
        if not isinstance(q_param, (int, float)) or not math.isfinite(q_param):
            raise bad_input("q_param must be finite numeric", v_idx=v_idx, q_param=q_param)


def sorted_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(records, key=lambda r: (int(r["obs_time_ms"]), int(r["v_idx"]), str(r["src_name"])))


def build_window_specs(config: dict[str, Any], records: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    time_cfg = config["time"]
    origin = int(time_cfg["origin_ms"])
    window_ms = int(time_cfg["window_ms"])
    hop_ms = int(time_cfg.get("hop_ms", window_ms))
    if window_ms <= 0 or hop_ms <= 0:
        raise bad_input("window_ms and hop_ms must be positive", window_ms=window_ms, hop_ms=hop_ms)

    if records is not None and records:
        last_time = max(int(r["obs_time_ms"]) for r in records)
        count = max(1, math.ceil((last_time + 1 - origin) / hop_ms))
        declared = int(time_cfg.get("windows", count))
        count = max(count, declared)
    else:
        count = int(time_cfg.get("windows", 1))

    specs: list[dict[str, Any]] = []
    previous_end: int | None = None
    for index in range(count):
        start = origin + index * hop_ms
        end = start + window_ms
        if previous_end is not None and start < 0:
            raise bad_input("window time moved backwards", window_index=index, start=start)
        if end <= start:
            raise bad_input("window end must be greater than start", window_index=index)
        if previous_end is not None and end < previous_end:
            raise bad_input("window end moved backwards", window_index=index, end=end, previous_end=previous_end)
        specs.append(
            {
                "window_id": f"w{index:03d}-{start}-{end}",
                "window_index": index,
                "start_ms": start,
                "end_ms": end,
                "window_ms": window_ms,
                "grid_rows": int(time_cfg["grid_rows"]),
            }
        )
        previous_end = end
    return specs


def assert_window_not_regressed(committed_end_ms: int | None, requested_end_ms: int) -> None:
    if committed_end_ms is not None and requested_end_ms < committed_end_ms:
        raise bad_input(
            "window time regression rejected",
            committed_end_ms=committed_end_ms,
            requested_end_ms=requested_end_ms,
        )


def _complete_variable(
    observed: np.ndarray,
    observed_q: np.ndarray,
    completion_cfg: dict[str, Any],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    rows = observed.shape[0]
    values = observed.astype(np.float64, copy=True)
    q_values = observed_q.astype(np.float64, copy=True)
    missing_mask = np.isnan(values)
    decay_weights = np.ones(rows, dtype=np.float64)
    method = completion_cfg.get("method", "ffill")
    constant = float(completion_cfg.get("constant", 0.0))
    decay = float(completion_cfg.get("noise_decay", 0.5))

    last_value: float | None = None
    last_q: float = 1.0
    distance = 0
    for row in range(rows):
        if not missing_mask[row]:
            last_value = float(values[row])
            last_q = float(q_values[row])
            distance = 0
            decay_weights[row] = 1.0
            continue

        distance += 1
        decay_weights[row] = decay**distance
        q_values[row] = last_q * decay_weights[row]
        if method == "constant":
            values[row] = constant
        elif method == "ffill":
            if last_value is None:
                values[row] = constant
            else:
                values[row] = last_value
        else:
            raise bad_input(f"unsupported completion method: {method}", method=method)

    return values, missing_mask.astype(np.float64), decay_weights, q_values


def _quantile_stats(values: np.ndarray, precision: Precision) -> dict[str, Any]:
    quantile_levels = [0.05, 0.25, 0.5, 0.75, 0.95]
    raw_quantiles = np.quantile(values, quantile_levels, method="linear")
    quantiles = precision.q(raw_quantiles)
    return {
        "levels": quantile_levels,
        "quantiles": [float(x) for x in quantiles],
        "median": float(quantiles[2]),
        "q25": float(quantiles[1]),
        "q75": float(quantiles[3]),
        "iqr": float(precision.sub(quantiles[3], quantiles[1])),
    }


def preprocess_window(
    records: list[dict[str, Any]],
    spec: dict[str, Any],
    config: dict[str, Any],
    precision: Precision,
) -> dict[str, Any]:
    validate_records(records, config)
    variables = list(config["variables"])
    rows = int(spec["grid_rows"])
    start = int(spec["start_ms"])
    end = int(spec["end_ms"])
    window_ms = int(spec["window_ms"])
    bin_ms = window_ms / rows
    width = int(config.get("time", {}).get("values_width", 1))
    subsample_ms = int(config.get("time", {}).get("subsample_ms", 25))

    window_records = [
        r for r in records if start <= int(r["obs_time_ms"]) < end
    ]
    window_records = sorted_records(window_records)
    v_idx_set = sorted(int(r["v_idx"]) for r in window_records)

    raw_bins: dict[str, list[list[float]]] = {name: [[] for _ in range(rows)] for name in variables}
    q_bins: dict[str, list[list[float]]] = {name: [[] for _ in range(rows)] for name in variables}
    for record in window_records:
        name = str(record["src_name"])
        obs_time = int(record["obs_time_ms"])
        q_param = float(record["q_param"])
        for sample_index, sample in enumerate(record["values"]):
            sample_time = obs_time + sample_index * (subsample_ms if width > 1 else 0)
            offset = sample_time - start
            if offset < 0 or offset >= window_ms:
                continue
            bin_index = min(rows - 1, max(0, int(math.floor(offset / bin_ms))))
            raw_bins[name][bin_index].append(float(sample))
            q_bins[name][bin_index].append(q_param)

    matrix = np.zeros((rows, len(variables)), dtype=np.float64)
    missing = np.zeros((rows, len(variables)), dtype=np.float64)
    decays = np.ones((rows, len(variables)), dtype=np.float64)
    q_matrix = np.ones((rows, len(variables)), dtype=np.float64)
    stats: dict[str, Any] = {}
    completion_cfg = config.get("completion", {})

    for col, name in enumerate(variables):
        observed = np.full(rows, np.nan, dtype=np.float64)
        observed_q = np.full(rows, np.nan, dtype=np.float64)
        for row in range(rows):
            if raw_bins[name][row]:
                observed[row] = float(precision.mean(np.asarray(raw_bins[name][row], dtype=np.float64)))
                observed_q[row] = float(precision.mean(np.asarray(q_bins[name][row], dtype=np.float64)))

        completed, mask, decay, q_values = _complete_variable(observed, observed_q, completion_cfg)
        completed = precision.q(completed)
        matrix[:, col] = completed
        missing[:, col] = mask
        decays[:, col] = decay
        q_matrix[:, col] = precision.q(q_values)

        mean = precision.mean(completed)
        variance = precision.var(completed)
        qstats = _quantile_stats(completed, precision)
        stats[name] = {
            "mean": float(mean),
            "variance": float(variance),
            "std": float(precision.sqrt(variance)),
            **qstats,
            "missing_count": int(mask.sum()),
            "observed_count": int(rows - mask.sum()),
            "q_param_mean": float(precision.mean(q_values)),
        }

    return {
        "window": spec,
        "variables": variables,
        "v_idx": v_idx_set,
        "record_count": len(window_records),
        "matrix": matrix,
        "missing_mask": missing,
        "decay_weights": decays,
        "q_matrix": q_matrix,
        "stats": stats,
        "precision": precision.config_view(),
    }


def select_window_records(records: list[dict[str, Any]], spec: dict[str, Any]) -> list[dict[str, Any]]:
    start = int(spec["start_ms"])
    end = int(spec["end_ms"])
    return [r for r in records if start <= int(r["obs_time_ms"]) < end]
