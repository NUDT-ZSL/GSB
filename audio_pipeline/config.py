"""Task description parsing and domain validation."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .errors import PipelineError
from .models import InputSpec, MeasurementDefaults, SnapshotConfig, TaskSpec
from .util import (
    as_int,
    as_number,
    finite_number,
    load_json,
    safe_resolve,
    sha256_file,
)


_KNOWN_LAYOUTS = {
    "mono": 1,
    "1": 1,
    "1.0": 1,
    "stereo": 2,
    "2": 2,
    "2.0": 2,
}


def _get(mapping: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return default


def _optional_int(value: Any, name: str, code: str) -> int | None:
    if value is None:
        return None
    return as_int(value, name, minimum=1, code=code)


def _parse_layout(value: Any, channels_value: Any,
                  input_id: str) -> tuple[str, int, Any]:
    weights = None
    if isinstance(value, dict):
        weights = value.get("weights")
        channels_value = value.get("channels", channels_value)
        value = value.get("name", value.get("layout"))
    key = str(value).strip().lower() if value is not None else ""
    if key not in _KNOWN_LAYOUTS:
        raise PipelineError("E_LAYOUT", f"unsupported layout {value!r}", input_id=input_id)
    layout_channels = _KNOWN_LAYOUTS[key]
    if channels_value is not None:
        try:
            declared_channels = as_int(channels_value, "channels", minimum=1,
                                       maximum=2, code="E_CHANNELS")
        except PipelineError as exc:
            exc.input_id = input_id
            raise
        if declared_channels != layout_channels:
            raise PipelineError(
                "E_CHANNELS",
                f"layout {key} implies {layout_channels} channels, got {declared_channels}",
                input_id=input_id,
            )
    return "mono" if layout_channels == 1 else "stereo", layout_channels, weights


def _parse_input(item: Any, order: int, base: Path) -> InputSpec:
    if not isinstance(item, dict):
        raise PipelineError("E_INPUT", f"input #{order + 1} must be an object")
    input_id = item.get("id", item.get("input_id"))
    if not isinstance(input_id, str) or not input_id.strip():
        raise PipelineError("E_INPUT", f"input #{order + 1} has no non-empty id")
    input_id = input_id.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", input_id):
        raise PipelineError(
            "E_INPUT",
            "input id must contain only ASCII letters, digits, '.', '_' or '-'",
            input_id=input_id,
        )
    reserved = {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
    if input_id.split(".", 1)[0].upper() in reserved or input_id.endswith("."):
        raise PipelineError(
            "E_INPUT", "input id is not a portable Windows file stem",
            input_id=input_id,
        )
    path_value = item.get("path")
    if not isinstance(path_value, str) or not path_value.strip():
        raise PipelineError("E_INPUT", "input path must be a non-empty string",
                            input_id=input_id)
    canonical_id = item.get("canonical_id", item.get("canonical_input_id", input_id))
    if not isinstance(canonical_id, str) or not canonical_id.strip():
        raise PipelineError("E_INPUT", "canonical_id must be a non-empty string",
                            input_id=input_id)

    layout_value = _get(item, "layout", "channel_layout", default=None)
    channels_value = item.get("channels")
    if layout_value is None and channels_value is not None:
        layout_value = "mono" if channels_value == 1 else "stereo"
    if layout_value is None:
        raise PipelineError("E_LAYOUT", "input layout is required", input_id=input_id)
    layout, layout_channels, layout_weights = _parse_layout(
        layout_value, channels_value, input_id
    )

    weights_value = _get(item, "weights", "channel_weights", default=layout_weights)
    if weights_value is None:
        weights_value = [1.0 / layout_channels] * layout_channels
    if not isinstance(weights_value, (list, tuple)):
        raise PipelineError("E_WEIGHTS", "weights must be an array", input_id=input_id)
    if len(weights_value) != layout_channels:
        raise PipelineError(
            "E_WEIGHTS",
            f"weights length {len(weights_value)} != channel count {layout_channels}",
            input_id=input_id,
        )
    weights: list[float] = []
    for index, value in enumerate(weights_value):
        if not finite_number(value) or float(value) < 0:
            raise PipelineError(
                "E_WEIGHTS",
                f"weight[{index}] must be finite and non-negative",
                input_id=input_id,
            )
        weights.append(float(value))
    if sum(weights) <= 0:
        raise PipelineError("E_WEIGHTS", "at least one channel weight must be positive",
                            input_id=input_id)

    declared_rate = _optional_int(
        _get(item, "declared_sample_rate", "sample_rate"), "declared_sample_rate", "E_SR"
    )
    declared_depth = _optional_int(
        _get(item, "declared_bit_depth", "bit_depth"), "declared_bit_depth", "E_BIT_DEPTH"
    )
    return InputSpec(
        order=order,
        input_id=input_id,
        canonical_id=canonical_id.strip(),
        path=safe_resolve(base, path_value.strip()),
        layout=layout,
        channels=layout_channels,
        weights=tuple(weights),
        declared_sample_rate=declared_rate,
        declared_bit_depth=declared_depth,
        config_errors=(),
        raw=dict(item),
    )


def _parse_measurement(raw: Any, allowed_rates: frozenset[int],
                       allowed_depths: frozenset[int]) -> MeasurementDefaults:
    if not isinstance(raw, dict):
        raise PipelineError("E_CONFIG", "measurement must be an object")

    window_ms = as_number(_get(raw, "window_ms", default=400), "window_ms",
                          exclusive_minimum=True, code="E_WINDOW")
    hop_ms = as_number(_get(raw, "hop_ms", default=100), "hop_ms",
                       exclusive_minimum=True, code="E_WINDOW")
    policy = _get(raw, "partial_window_policy", "tail_policy", default="zero_pad")
    if policy not in ("zero_pad", "drop", "shorten"):
        raise PipelineError(
            "E_WINDOW",
            "partial_window_policy must be zero_pad, drop, or shorten",
        )
    relative_gate = as_number(
        _get(raw, "relative_gate_lu", "relative_gate_db", default=-10.0),
        "relative_gate_lu", maximum=0.0, code="E_GATE",
    )
    absolute_gate = as_number(
        _get(raw, "absolute_gate_lufs", "absolute_gate_db", default=-70.0),
        "absolute_gate_lufs", minimum=-120.0, maximum=20.0, code="E_GATE",
    )
    dr_min = as_number(
        _get(raw, "dynamic_range_min_db", "dr_min_db", default=0.0),
        "dynamic_range_min_db", minimum=0.0, code="E_DRANGE",
    )
    dr_max = as_number(
        _get(raw, "dynamic_range_max_db", "dr_max_db", default=24.0),
        "dynamic_range_max_db", minimum=0.0, code="E_DRANGE",
    )
    if dr_min > dr_max:
        raise PipelineError("E_DRANGE", "dynamic_range_min_db must be <= max")
    target_lufs = as_number(
        _get(raw, "target_lufs", "target_loudness_lufs", default=-23.0),
        "target_lufs", minimum=-70.0, maximum=0.0, code="E_GATE",
    )
    comp_threshold = as_number(
        _get(raw, "compression_threshold_lufs", default=-18.0),
        "compression_threshold_lufs", minimum=-70.0, maximum=0.0, code="E_GATE",
    )
    comp_ratio = as_number(
        _get(raw, "compression_ratio", default=2.0),
        "compression_ratio", minimum=1.0, code="E_GATE",
    )
    target_sr = as_int(
        _get(raw, "target_sample_rate", "output_sample_rate", default=48000),
        "target_sample_rate", minimum=1, code="E_SR",
    )
    if target_sr not in allowed_rates:
        raise PipelineError("E_SR", f"target sample rate {target_sr} is not allowed")
    output_depth = as_int(
        _get(raw, "output_bit_depth", "target_bit_depth", default=24),
        "output_bit_depth", minimum=1, code="E_BIT_DEPTH",
    )
    if output_depth not in allowed_depths:
        raise PipelineError("E_BIT_DEPTH", f"output bit depth {output_depth} is not allowed")
    max_workers = as_int(
        _get(raw, "max_workers", "concurrency", default=2),
        "max_workers", minimum=1, maximum=64, code="E_CONFIG",
    )
    decode_budget = as_int(
        _get(raw, "decode_buffer_bytes", "in_flight_decode_bytes", default=4 * 1024 * 1024),
        "decode_buffer_bytes", minimum=1, code="E_BUDGET",
    )
    sample_budget = as_int(
        _get(raw, "sample_buffer_bytes", "max_sample_bytes", default=64 * 1024 * 1024),
        "sample_buffer_bytes", minimum=1, code="E_BUDGET",
    )
    checkpoint_windows = as_int(
        _get(raw, "checkpoint_windows", "chunk_windows", default=8),
        "checkpoint_windows", minimum=1, code="E_WINDOW",
    )
    block_frames = as_int(
        _get(raw, "decode_block_frames", default=4096),
        "decode_block_frames", minimum=1, code="E_CONFIG",
    )
    return MeasurementDefaults(
        window_ms=window_ms,
        hop_ms=hop_ms,
        relative_gate_lu=relative_gate,
        absolute_gate_lufs=absolute_gate,
        partial_window_policy=policy,
        dynamic_range_min_db=dr_min,
        dynamic_range_max_db=dr_max,
        target_lufs=target_lufs,
        compression_threshold_lufs=comp_threshold,
        compression_ratio=comp_ratio,
        target_sample_rate=target_sr,
        output_bit_depth=output_depth,
        max_workers=max_workers,
        decode_buffer_bytes=decode_budget,
        sample_buffer_bytes=sample_budget,
        checkpoint_windows=checkpoint_windows,
        decode_block_frames=block_frames,
    )


def _parse_snapshots(raw: Any, base: Path) -> SnapshotConfig:
    if not isinstance(raw, dict):
        raise PipelineError("E_CONFIG", "snapshots must be an object")
    v1 = raw.get("v1")
    v2 = raw.get("v2")
    if not isinstance(v1, dict):
        raise PipelineError("E_CONFIG", "snapshots.v1 must be an object")
    if not isinstance(v2, dict):
        raise PipelineError("E_CONFIG", "snapshots.v2 must be an object")
    v1_path_value = _get(v1, "path", "file")
    v1_sha = _get(v1, "sha256", "digest")
    if not isinstance(v1_path_value, str) or not v1_path_value:
        raise PipelineError("E_CONFIG", "snapshots.v1.path is required")
    if not isinstance(v1_sha, str) or len(v1_sha) != 64:
        raise PipelineError("E_DIGEST", "snapshots.v1.sha256 is required")
    v2_db_value = _get(v2, "database", "path", "db")
    v2_sha = _get(v2, "sha256", "db_sha256", "digest")
    if not isinstance(v2_db_value, str) or not v2_db_value:
        raise PipelineError("E_CONFIG", "snapshots.v2.database is required")
    if not isinstance(v2_sha, str) or len(v2_sha) != 64:
        raise PipelineError("E_DIGEST", "snapshots.v2.sha256 is required")
    shard_root_value = _get(v2, "shard_root", "shards", default=".")
    if not isinstance(shard_root_value, str):
        raise PipelineError("E_CONFIG", "snapshots.v2.shard_root must be a string")
    expected = v1.get("expected_entries", v1.get("expected_windows"))
    if expected is not None:
        expected = as_int(expected, "v1.expected_entries", minimum=0,
                          code="E_SNAPSHOT")
    origin = as_int(
        _get(v1, "logical_origin_ms", "origin_ms", default=0),
        "v1.logical_origin_ms", minimum=0, code="E_SNAPSHOT",
    )
    return SnapshotConfig(
        v1_path=safe_resolve(base, v1_path_value),
        v1_sha256=v1_sha.lower(),
        v1_expected_entries=expected,
        v1_logical_origin_ms=origin,
        v2_db_path=safe_resolve(base, v2_db_value),
        v2_db_sha256=v2_sha.lower(),
        v2_shard_root=safe_resolve(base, shard_root_value),
    )


def load_task(path: Path, workspace_root: Path | None = None) -> TaskSpec:
    path = path.resolve()
    raw = load_json(path)
    if not isinstance(raw, dict):
        raise PipelineError("E_CONFIG", "task description must be a JSON object")
    base = path.parent
    root = (workspace_root or base).resolve()

    allowed_rates_raw = _get(
        raw, "allowed_sample_rates", "sample_rates_allowed",
        default=[8000, 16000, 22050, 24000, 32000, 44100, 48000, 88200, 96000],
    )
    if not isinstance(allowed_rates_raw, list) or not allowed_rates_raw:
        raise PipelineError("E_CONFIG", "allowed_sample_rates must be a non-empty array")
    allowed_rates = frozenset(
        as_int(value, "allowed_sample_rates[]", minimum=1, code="E_SR")
        for value in allowed_rates_raw
    )
    depths_raw = _get(raw, "allowed_bit_depths", default=[16, 24])
    if not isinstance(depths_raw, list):
        raise PipelineError("E_CONFIG", "allowed_bit_depths must be an array")
    allowed_depths = frozenset(
        as_int(value, "allowed_bit_depths[]", minimum=1, code="E_BIT_DEPTH")
        for value in depths_raw
    )
    if not allowed_depths or not allowed_depths.issubset({16, 24}):
        raise PipelineError("E_BIT_DEPTH", "allowed bit depths may only be 16 and/or 24")

    measurement = _parse_measurement(raw.get("measurement", {}), allowed_rates,
                                     allowed_depths)
    inputs_raw = raw.get("inputs")
    if not isinstance(inputs_raw, list) or not inputs_raw:
        raise PipelineError("E_CONFIG", "inputs must be a non-empty array")
    inputs: list[InputSpec] = []
    seen: set[str] = set()
    for index, entry in enumerate(inputs_raw):
        try:
            spec = _parse_input(entry, index, base)
        except PipelineError as exc:
            if not isinstance(entry, dict) or not isinstance(
                entry.get("id", entry.get("input_id")), str
            ):
                raise
            input_id = str(entry.get("id", entry.get("input_id"))).strip()
            canonical = str(entry.get("canonical_id",
                                      entry.get("canonical_input_id", input_id)))
            path_text = entry.get("path")
            path = (
                safe_resolve(base, path_text)
                if isinstance(path_text, str) and path_text.strip()
                else base / "__invalid_input__"
            )
            if exc.input_id is None:
                exc.input_id = input_id or None
            spec = InputSpec(
                order=index,
                input_id=input_id or f"input-{index + 1}",
                canonical_id=canonical,
                path=path,
                layout="invalid",
                channels=0,
                weights=(),
                declared_sample_rate=None,
                declared_bit_depth=None,
                config_errors=(exc,),
                raw=dict(entry),
            )
        if spec.input_id in seen:
            error = PipelineError(
                "E_INPUT", f"duplicate input id {spec.input_id}",
                input_id=spec.input_id,
            )
            spec = InputSpec(
                order=spec.order,
                input_id=spec.input_id,
                canonical_id=spec.canonical_id,
                path=spec.path,
                layout=spec.layout,
                channels=spec.channels,
                weights=spec.weights,
                declared_sample_rate=spec.declared_sample_rate,
                declared_bit_depth=spec.declared_bit_depth,
                config_errors=spec.config_errors + (error,),
                raw=spec.raw,
            )
        seen.add(spec.input_id)
        inputs.append(spec)

    snapshots = _parse_snapshots(raw.get("snapshots"), base)
    anomaly_value = raw.get("anomalies", raw.get("anomaly_injections"))
    anomaly_file: Path | None = None
    anomaly_items: list[dict[str, Any]] = []
    if isinstance(anomaly_value, str):
        anomaly_file = safe_resolve(base, anomaly_value)
        anomaly_raw = load_json(anomaly_file, code="E_ANOMALY")
    elif isinstance(anomaly_value, list):
        anomaly_raw = {"items": anomaly_value}
    elif isinstance(anomaly_value, dict):
        anomaly_raw = anomaly_value
    elif anomaly_value is None:
        anomaly_raw = {"items": []}
    else:
        raise PipelineError("E_ANOMALY", "anomalies must be a path, array, or object")
    if not isinstance(anomaly_raw, dict):
        raise PipelineError("E_ANOMALY", "anomaly manifest must be an object")
    items = anomaly_raw.get("items", [])
    if not isinstance(items, list):
        raise PipelineError("E_ANOMALY", "anomaly items must be an array")
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise PipelineError("E_ANOMALY", f"anomaly item #{index + 1} must be an object")
        if not isinstance(item.get("kind"), str):
            raise PipelineError("E_ANOMALY", f"anomaly item #{index + 1} lacks kind")
        anomaly_items.append(dict(item))

    task_revision = sha256_file(path)
    return TaskSpec(
        path=path,
        workspace_root=root,
        task_id=str(raw.get("task_id", path.stem)),
        task_revision=task_revision,
        allowed_sample_rates=allowed_rates,
        allowed_bit_depths=allowed_depths,
        inputs=tuple(inputs),
        measurement=measurement,
        snapshots=snapshots,
        anomaly_file=anomaly_file,
        anomaly_items=tuple(anomaly_items),
        report_schema=str(raw.get("report_schema", "loudness-report/1")),
        raw=raw,
    )
