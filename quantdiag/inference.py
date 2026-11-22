from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np

from .calibration import calibration_activation_range
from .errors import (
    DiagnosticError,
    E_LINEAGE_BROKEN,
    E_SEMANTIC_DRIFT,
    E_STALE_RESULT,
)
from .formats import LayerQuant
from .model import (
    CONV_LAYERS,
    LAYER_ORDER,
    NetworkWeights,
    conv2d_valid,
    forward_float,
    linear_exact,
    max_pool2d,
    relu,
    validate_images,
)
from .snapshots import Snapshot, SnapshotStore
from .utils import (
    array_bytes,
    array_checksum,
    decode_array,
    encode_array,
    max_abs,
    quantiles,
    sha256_bytes,
    sha256_json,
)


def _qdq_static(
    values: np.ndarray, scale: float, zero_point: int = 0
) -> np.ndarray:
    safe_scale = max(float(scale), 1e-12)
    quantized = np.clip(np.rint(values / safe_scale) + zero_point, -128, 127)
    return (quantized - zero_point) * safe_scale


def _qdq_weight(weights: np.ndarray, layer: LayerQuant) -> np.ndarray:
    if not layer.per_channel:
        return _qdq_static(weights, layer.weight_scale, layer.zero_point)
    scales = np.asarray(layer.channel_scales, dtype=np.float64)
    if scales.ndim != 1 or scales.size != weights.shape[0]:
        raise ValueError(
            f"layer {layer.name!r} has {scales.size} channel scales for {weights.shape[0]} output channels"
        )
    reshaped = scales.reshape((scales.size,) + (1,) * (weights.ndim - 1))
    quantized = np.clip(
        np.rint(weights / reshaped) + layer.zero_point, -128, 127
    )
    return (quantized - layer.zero_point) * reshaped


def _dynamic_activation_qdq(
    values: np.ndarray,
    snapshot: Snapshot,
    layer: LayerQuant,
) -> tuple[np.ndarray, float, int]:
    activation_range = calibration_activation_range(snapshot.calibration, layer.name)
    if activation_range is None:
        raise ValueError(
            f"layer {layer.name!r} requests per-activation dynamic quantization but has no calibration range"
        )
    minimum, maximum = activation_range
    if maximum <= minimum + 1e-15:
        scale = max(layer.weight_scale, 1e-12)
        zero_point = 0
    else:
        scale = (maximum - minimum) / 255.0
        zero_point = int(np.clip(np.rint(-128.0 - minimum / scale), -128, 127))
    return _qdq_static(values, scale, zero_point), scale, zero_point


@dataclass(frozen=True)
class CacheKeyComponents:
    layer: str
    operation: str
    image_hash: str
    input_hash: str
    upstream_hash: str
    upstream_key: str
    quant_version: str
    calibration_version: str
    per_channel: bool
    dynamic_range_mode: str
    model_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "operation": self.operation,
            "image_hash": self.image_hash,
            "input_hash": self.input_hash,
            "upstream_hash": self.upstream_hash,
            "upstream_key": self.upstream_key,
            "quant_version": self.quant_version,
            "calibration_version": self.calibration_version,
            "per_channel": self.per_channel,
            "dynamic_range_mode": self.dynamic_range_mode,
            "model_fingerprint": self.model_fingerprint,
        }


@dataclass
class CacheArtifact:
    key: str
    components: CacheKeyComponents
    output: np.ndarray
    output_hash: str
    activation_checksum: str
    hits: int = 0


class LayerCache:
    def __init__(self) -> None:
        self._items: dict[str, CacheArtifact] = {}
        self._latest_by_layer: dict[str, str] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> CacheArtifact | None:
        with self._lock:
            item = self._items.get(key)
            if item is not None:
                item.hits += 1
            return item

    def put(
        self,
        key: str,
        components: CacheKeyComponents,
        output: np.ndarray,
        *,
        output_hash: str | None = None,
        activation_checksum: str | None = None,
    ) -> CacheArtifact:
        canonical = np.ascontiguousarray(output, dtype=np.float64).copy()
        artifact = CacheArtifact(
            key=key,
            components=components,
            output=canonical,
            output_hash=output_hash or array_checksum(canonical),
            activation_checksum=activation_checksum or array_checksum(canonical),
        )
        with self._lock:
            existing = self._items.get(key)
            if existing is not None:
                existing.hits += 1
                return existing
            self._items[key] = artifact
            self._latest_by_layer[components.layer] = key
        return artifact

    def explain(self, layer: str, key: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            selected = key or self._latest_by_layer.get(layer)
            if selected is None:
                return None
            item = self._items.get(selected)
            if item is None:
                return None
            return {
                "layer": layer,
                "key": item.key,
                "components": item.components.to_dict(),
                "product_hash": item.output_hash,
                "activation_checksum": item.activation_checksum,
                "hit_count": item.hits,
                "shape": list(item.output.shape),
            }

    def artifacts_for_layer(self, layer: str) -> list[CacheArtifact]:
        with self._lock:
            return [
                item
                for item in self._items.values()
                if item.components.layer == layer
            ]

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


@dataclass(frozen=True)
class LayerTrace:
    layer: str
    status: str
    cache_key: str
    key_components: tuple[tuple[str, Any], ...]
    output_hash: str
    activation_checksum: str
    quant_version: str
    calibration_version: str
    per_channel: bool
    dynamic_range_mode: str

    def components_dict(self) -> dict[str, Any]:
        return dict(self.key_components)

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "status": self.status,
            "cache_key": self.cache_key,
            "key_components": self.components_dict(),
            "output_hash": self.output_hash,
            "activation_checksum": self.activation_checksum,
            "quant_version": self.quant_version,
            "calibration_version": self.calibration_version,
            "per_channel": self.per_channel,
            "dynamic_range_mode": self.dynamic_range_mode,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "LayerTrace":
        return cls(
            layer=str(payload["layer"]),
            status=str(payload["status"]),
            cache_key=str(payload["cache_key"]),
            key_components=tuple(
                sorted((str(k), v) for k, v in payload["key_components"].items())
            ),
            output_hash=str(payload["output_hash"]),
            activation_checksum=str(payload["activation_checksum"]),
            quant_version=str(payload["quant_version"]),
            calibration_version=str(payload["calibration_version"]),
            per_channel=bool(payload["per_channel"]),
            dynamic_range_mode=str(payload["dynamic_range_mode"]),
        )


@dataclass(frozen=True)
class InferenceResult:
    snapshot_version: int
    params_version: str
    calibration_version: str
    model_fingerprint: str
    batch_hash: str
    image_hashes: tuple[str, ...]
    scores: tuple[tuple[float, ...], ...]
    scores_checksum: str
    layers: tuple[LayerTrace, ...]
    layer_outputs: tuple[tuple[str, tuple[int, ...], str], ...]
    reused_layers: tuple[str, ...]
    recomputed_layers: tuple[str, ...]
    result_checksum: str

    @property
    def scores_array(self) -> np.ndarray:
        return np.asarray(self.scores, dtype=np.float64)

    def layer(self, name: str) -> LayerTrace:
        for item in self.layers:
            if item.layer == name:
                return item
        raise KeyError(name)


@dataclass(frozen=True)
class TraceRecord:
    record_id: str
    snapshot_version: int
    params_version: str
    calibration_version: str
    model_fingerprint: str
    batch_hash: str
    image_hashes: tuple[str, ...]
    input_shape: tuple[int, ...]
    input_b64: str
    scores: tuple[tuple[float, ...], ...]
    scores_checksum: str
    layers: tuple[LayerTrace, ...]
    layer_outputs: tuple[tuple[str, tuple[int, ...], str], ...]
    result_checksum: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "snapshot_version": self.snapshot_version,
            "params_version": self.params_version,
            "calibration_version": self.calibration_version,
            "model_fingerprint": self.model_fingerprint,
            "batch_hash": self.batch_hash,
            "image_hashes": list(self.image_hashes),
            "input_shape": list(self.input_shape),
            "input_b64": self.input_b64,
            "scores": [list(row) for row in self.scores],
            "scores_checksum": self.scores_checksum,
            "layers": [layer.to_dict() for layer in self.layers],
            "layer_outputs": {
                name: {"shape": list(shape), "data_b64": payload}
                for name, shape, payload in self.layer_outputs
            },
            "result_checksum": self.result_checksum,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TraceRecord":
        return cls(
            record_id=str(payload["record_id"]),
            snapshot_version=int(payload["snapshot_version"]),
            params_version=str(payload["params_version"]),
            calibration_version=str(payload["calibration_version"]),
            model_fingerprint=str(payload["model_fingerprint"]),
            batch_hash=str(payload["batch_hash"]),
            image_hashes=tuple(str(v) for v in payload["image_hashes"]),
            input_shape=tuple(int(v) for v in payload["input_shape"]),
            input_b64=str(payload["input_b64"]),
            scores=tuple(tuple(float(v) for v in row) for row in payload["scores"]),
            scores_checksum=str(payload["scores_checksum"]),
            layers=tuple(LayerTrace.from_dict(item) for item in payload["layers"]),
            layer_outputs=tuple(
                (
                    str(name),
                    tuple(int(v) for v in item["shape"]),
                    str(item["data_b64"]),
                )
                for name, item in payload["layer_outputs"].items()
            ),
            result_checksum=str(payload["result_checksum"]),
        )


class TraceRegistry:
    def __init__(self) -> None:
        self._records: dict[str, TraceRecord] = {}
        self._lock = threading.Lock()

    def put(self, record: TraceRecord) -> None:
        with self._lock:
            self._records[record.record_id] = record

    def get(self, record_id: str) -> TraceRecord | None:
        with self._lock:
            return self._records.get(record_id)

    def delete(self, record_id: str) -> None:
        with self._lock:
            self._records.pop(record_id, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)


@dataclass(frozen=True)
class ChangeEvent:
    layer: str
    kind: str
    field: str
    old_value: Any
    new_value: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "kind": self.kind,
            "field": self.field,
            "old_value": self.old_value,
            "new_value": self.new_value,
        }


@dataclass(frozen=True)
class ChangeAnalysis:
    old_version: int
    new_version: int
    events: tuple[ChangeEvent, ...]
    affected_layers: tuple[str, ...]
    reusable_layers: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "old_version": self.old_version,
            "new_version": self.new_version,
            "events": [event.to_dict() for event in self.events],
            "affected_layers": list(self.affected_layers),
            "reusable_layers": list(self.reusable_layers),
        }


class DeterministicEngine:
    def __init__(
        self,
        weights: NetworkWeights | None = None,
        *,
        cache: LayerCache | None = None,
        traces: TraceRegistry | None = None,
    ) -> None:
        self.weights = weights if weights is not None else NetworkWeights()
        self.cache = cache if cache is not None else LayerCache()
        self.traces = traces if traces is not None else TraceRegistry()

    def _key_components(
        self,
        *,
        snapshot: Snapshot,
        layer: LayerQuant,
        image_hash: str,
        input_hash: str,
        upstream_hash: str,
        upstream_key: str,
    ) -> CacheKeyComponents:
        return CacheKeyComponents(
            layer=layer.name,
            operation="conv_relu_pool" if layer.name in CONV_LAYERS else "linear",
            image_hash=image_hash,
            input_hash=input_hash,
            upstream_hash=upstream_hash,
            upstream_key=upstream_key,
            quant_version=layer.quant_version,
            calibration_version=layer.calibration_version,
            per_channel=layer.per_channel,
            dynamic_range_mode=layer.dynamic_range_mode,
            model_fingerprint=snapshot.model_fingerprint,
        )

    @staticmethod
    def _cache_key(components: CacheKeyComponents) -> str:
        return "lk-" + sha256_json(components.to_dict())

    def _execute_layer(
        self,
        snapshot: Snapshot,
        layer_name: str,
        inputs: np.ndarray,
        *,
        tracking: dict[str, list[np.ndarray]] | None = None,
    ) -> np.ndarray:
        layer = snapshot.layer(layer_name)
        if layer_name in CONV_LAYERS:
            quantized_weights = _qdq_weight(self.weights.weight(layer_name), layer)
            convolved = conv2d_valid(
                inputs,
                quantized_weights,
                self.weights.bias(layer_name),
            )
            activation = relu(convolved)
            if layer.dynamic_range_mode == "per_activation":
                quantized_activation, _, _ = _dynamic_activation_qdq(
                    activation, snapshot, layer
                )
                activation_error = quantized_activation - activation
            else:
                quantized_activation = activation
                activation_error = np.zeros_like(activation)
            if tracking is not None:
                tracking.setdefault(layer_name, []).append(
                    activation_error.reshape(-1)
                )
            return max_pool2d(quantized_activation, 2)

        flat = np.asarray(inputs, dtype=np.float64).reshape(inputs.shape[0], -1)
        quantized_weights = _qdq_weight(self.weights.weight("fc"), layer)
        return linear_exact(
            flat, quantized_weights, self.weights.bias("fc")
        )

    def infer(
        self,
        snapshot: Snapshot,
        images: np.ndarray,
        *,
        cache: LayerCache | None = None,
    ) -> InferenceResult:
        snapshot.verify_checksum()
        selected_cache = cache if cache is not None else self.cache
        batch = validate_images(images)
        batch_hash = array_checksum(batch)
        image_hashes = tuple(array_checksum(batch[index]) for index in range(batch.shape[0]))
        current = batch[:, None, :, :]
        upstream_hash = batch_hash
        upstream_key = f"root:{batch_hash}"
        traces: list[LayerTrace] = []
        encoded_outputs: list[tuple[str, tuple[int, ...], str]] = []
        reused: list[str] = []
        recomputed: list[str] = []

        for layer_name in LAYER_ORDER:
            layer = snapshot.layer(layer_name)
            input_hash = array_checksum(current)
            components = self._key_components(
                snapshot=snapshot,
                layer=layer,
                image_hash=batch_hash,
                input_hash=input_hash,
                upstream_hash=upstream_hash,
                upstream_key=upstream_key,
            )
            key = self._cache_key(components)
            artifact = selected_cache.get(key)
            if artifact is None:
                output = self._execute_layer(snapshot, layer_name, current)
                artifact = selected_cache.put(key, components, output)
                status = "recomputed"
                recomputed.append(layer_name)
            else:
                output = artifact.output.copy()
                status = "reused"
                reused.append(layer_name)
            traces.append(
                LayerTrace(
                    layer=layer_name,
                    status=status,
                    cache_key=key,
                    key_components=tuple(sorted(components.to_dict().items())),
                    output_hash=artifact.output_hash,
                    activation_checksum=artifact.activation_checksum,
                    quant_version=layer.quant_version,
                    calibration_version=layer.calibration_version,
                    per_channel=layer.per_channel,
                    dynamic_range_mode=layer.dynamic_range_mode,
                )
            )
            encoded_outputs.append((layer_name, tuple(output.shape), encode_array(output)))
            current = output
            upstream_hash = artifact.activation_checksum
            upstream_key = key

        scores_array = np.ascontiguousarray(current, dtype=np.float64)
        scores_checksum = sha256_bytes(array_bytes(scores_array))
        result_checksum = sha256_json(
            {
                "batch_hash": batch_hash,
                "snapshot_version": snapshot.version,
                "params_version": snapshot.params_version,
                "calibration_version": snapshot.calibration_version,
                "scores_checksum": scores_checksum,
                "layer_checksums": [item.activation_checksum for item in traces],
            }
        )
        return InferenceResult(
            snapshot_version=snapshot.version,
            params_version=snapshot.params_version,
            calibration_version=snapshot.calibration_version,
            model_fingerprint=snapshot.model_fingerprint,
            batch_hash=batch_hash,
            image_hashes=image_hashes,
            scores=tuple(tuple(float(v) for v in row) for row in scores_array),
            scores_checksum=scores_checksum,
            layers=tuple(traces),
            layer_outputs=tuple(encoded_outputs),
            reused_layers=tuple(reused),
            recomputed_layers=tuple(recomputed),
            result_checksum=result_checksum,
        )

    def make_trace(
        self, result: InferenceResult, images: np.ndarray, *, register: bool = True
    ) -> TraceRecord:
        batch = validate_images(images)
        payload = {
            "snapshot_version": result.snapshot_version,
            "params_version": result.params_version,
            "calibration_version": result.calibration_version,
            "model_fingerprint": result.model_fingerprint,
            "batch_hash": result.batch_hash,
            "image_hashes": list(result.image_hashes),
            "input_shape": list(batch.shape),
            "input_b64": encode_array(batch),
            "scores": [list(row) for row in result.scores],
            "scores_checksum": result.scores_checksum,
            "layers": [layer.to_dict() for layer in result.layers],
            "layer_outputs": {
                name: {"shape": list(shape), "data_b64": payload}
                for name, shape, payload in result.layer_outputs
            },
            "result_checksum": result.result_checksum,
        }
        record_id = "trace-" + sha256_json(payload)[:24]
        record = TraceRecord(
            record_id=record_id,
            snapshot_version=result.snapshot_version,
            params_version=result.params_version,
            calibration_version=result.calibration_version,
            model_fingerprint=result.model_fingerprint,
            batch_hash=result.batch_hash,
            image_hashes=result.image_hashes,
            input_shape=tuple(batch.shape),
            input_b64=str(payload["input_b64"]),
            scores=result.scores,
            scores_checksum=result.scores_checksum,
            layers=result.layers,
            layer_outputs=result.layer_outputs,
            result_checksum=result.result_checksum,
        )
        if register:
            self.traces.put(record)
        return record

    def analyze_change(self, old: Snapshot, new: Snapshot) -> ChangeAnalysis:
        events: list[ChangeEvent] = []
        changed_indices: list[int] = []
        order = [layer.name for layer in old.params.layers]
        for index, layer_name in enumerate(order):
            old_layer = old.layer(layer_name)
            try:
                new_layer = new.layer(layer_name)
            except KeyError:
                events.append(
                    ChangeEvent(layer_name, "scale", "layer", "present", "missing")
                )
                changed_indices.append(index)
                continue
            scale_changed = (
                old_layer.weight_scale != new_layer.weight_scale
                or old_layer.channel_scales != new_layer.channel_scales
            )
            if scale_changed:
                events.append(
                    ChangeEvent(
                        layer_name,
                        "scale",
                        "weight_scale/channel_scales",
                        {
                            "weight_scale": old_layer.weight_scale,
                            "channel_scales": list(old_layer.channel_scales),
                        },
                        {
                            "weight_scale": new_layer.weight_scale,
                            "channel_scales": list(new_layer.channel_scales),
                        },
                    )
                )
                changed_indices.append(index)
            old_samples = old.calibration.sample_count_for(layer_name)
            new_samples = new.calibration.sample_count_for(layer_name)
            dynamic_changed = (
                old_layer.dynamic_range_mode != new_layer.dynamic_range_mode
            )
            per_channel_changed = old_layer.per_channel != new_layer.per_channel
            if old_samples != new_samples:
                events.append(
                    ChangeEvent(
                        layer_name,
                        "calibration_samples",
                        "calibration.samples",
                        old_samples,
                        new_samples,
                    )
                )
                changed_indices.append(index)
            elif (
                old_layer.calibration_version != new_layer.calibration_version
                or old_layer.quant_version != new_layer.quant_version
            ) and not scale_changed and not dynamic_changed and not per_channel_changed:
                events.append(
                    ChangeEvent(
                        layer_name,
                        "calibration_stats",
                        "calibration.version",
                        old_layer.calibration_version,
                        new_layer.calibration_version,
                    )
                )
                changed_indices.append(index)
            if dynamic_changed:
                events.append(
                    ChangeEvent(
                        layer_name,
                        "dynamic_range_mode",
                        "dynamic_range_mode",
                        old_layer.dynamic_range_mode,
                        new_layer.dynamic_range_mode,
                    )
                )
                changed_indices.append(index)
            if per_channel_changed:
                events.append(
                    ChangeEvent(
                        layer_name,
                        "scale",
                        "per_channel",
                        old_layer.per_channel,
                        new_layer.per_channel,
                    )
                )
                changed_indices.append(index)

        layer_names = [layer.name for layer in old.params.layers]
        if changed_indices:
            first = min(changed_indices)
            affected = tuple(layer_names[first:])
            reusable = tuple(layer_names[:first])
        else:
            affected = ()
            reusable = tuple(layer_names)
        return ChangeAnalysis(
            old_version=old.version,
            new_version=new.version,
            events=tuple(events),
            affected_layers=affected,
            reusable_layers=reusable,
        )

    def compute_error_spectrum(
        self, snapshot: Snapshot, images: np.ndarray
    ) -> dict[str, Any]:
        batch = validate_images(images)
        reference_scores, reference_outputs = forward_float(self.weights, batch)
        tracking: dict[str, list[np.ndarray]] = {}
        layer_outputs: dict[str, np.ndarray] = {}
        current = batch[:, None, :, :]
        for layer_name in LAYER_ORDER:
            current = self._execute_layer(
                snapshot, layer_name, current, tracking=tracking
            )
            layer_outputs[layer_name] = current.copy()
        quantized_scores = layer_outputs["fc"]
        layer_reports: list[dict[str, Any]] = []
        previous_output_error_p99: float | None = None
        for layer_name in LAYER_ORDER:
            layer = snapshot.layer(layer_name)
            weights = self.weights.weight(layer_name)
            quantized_weights = _qdq_weight(weights, layer)
            weight_error = np.abs(weights - quantized_weights)
            base_quantized = _qdq_static(weights, layer.weight_scale, layer.zero_point)
            if layer.per_channel:
                channel_error = np.abs(quantized_weights - base_quantized)
            else:
                channel_error = np.zeros_like(weights)
            activation_errors = tracking.get(layer_name, [])
            if layer_name in CONV_LAYERS:
                activation_error = np.abs(np.concatenate(activation_errors))
            else:
                activation_error = np.zeros((1,), dtype=np.float64)
            output_error = np.abs(
                layer_outputs[layer_name] - reference_outputs[layer_name]
            )
            output_q = quantiles(output_error)
            if previous_output_error_p99 is None or previous_output_error_p99 <= 1e-15:
                amplification = 1.0
            else:
                amplification = output_q["p99"] / previous_output_error_p99
            previous_output_error_p99 = max(output_q["p99"], 1e-15)
            source_stats = {
                "weight_rounding": quantiles(weight_error),
                "per_channel_scaling": quantiles(channel_error),
                "calibration_range": quantiles(activation_error),
            }
            dominant = max(
                source_stats,
                key=lambda name: source_stats[name]["p99"],
            )
            layer_reports.append(
                {
                    "layer": layer_name,
                    "dynamic_range_mode": layer.dynamic_range_mode,
                    "per_channel": layer.per_channel,
                    "weight_rounding": source_stats["weight_rounding"],
                    "per_channel_scaling": source_stats["per_channel_scaling"],
                    "calibration_range": source_stats["calibration_range"],
                    "output_error": output_q,
                    "amplification_ratio": float(amplification),
                    "dominant_source_level": dominant,
                }
            )
        score_error = np.abs(quantized_scores - reference_scores)
        return {
            "snapshot_version": snapshot.version,
            "params_version": snapshot.params_version,
            "calibration_version": snapshot.calibration_version,
            "reference_scores": reference_scores.tolist(),
            "quantized_scores": quantized_scores.tolist(),
            "score_error": quantiles(score_error),
            "max_abs_score_error": float(np.max(score_error)),
            "layers": layer_reports,
            "dominant_source_level": max(
                layer_reports,
                key=lambda item: item["output_error"]["p99"],
            )["dominant_source_level"],
        }

    def forward_with_outputs(
        self, snapshot: Snapshot, images: np.ndarray
    ) -> tuple[np.ndarray, dict[str, np.ndarray]]:
        """Run the quantized path while retaining each layer product for replay deltas."""

        batch = validate_images(images)
        outputs: dict[str, np.ndarray] = {}
        current = batch[:, None, :, :]
        for layer_name in LAYER_ORDER:
            current = self._execute_layer(snapshot, layer_name, current)
            outputs[layer_name] = current.copy()
        return current, outputs


def adjudicate_semantic_equivalence(
    engine: DeterministicEngine,
    snapshots: Iterable[Snapshot],
    images: np.ndarray,
    *,
    tolerance: float = 1e-12,
) -> dict[str, Any]:
    candidates = list(snapshots)
    if len(candidates) < 2:
        raise ValueError("at least two snapshots are required")
    results: list[InferenceResult] = []
    spectra: list[dict[str, Any]] = []
    reference_scores, _ = forward_float(engine.weights, validate_images(images))
    score_errors: list[float] = []
    for snapshot in candidates:
        result = engine.infer(snapshot, images, cache=LayerCache())
        results.append(result)
        spectrum = engine.compute_error_spectrum(snapshot, images)
        spectra.append(spectrum)
        score_errors.append(
            float(np.max(np.abs(result.scores_array - reference_scores)))
        )

    baseline = results[0]
    first_difference_layer: str | None = None
    for result in results[1:]:
        for expected, actual in zip(baseline.layers, result.layers):
            if (
                expected.activation_checksum != actual.activation_checksum
                or expected.output_hash != actual.output_hash
            ):
                first_difference_layer = expected.layer
                break
        if first_difference_layer is not None:
            break
        if baseline.scores_checksum != result.scores_checksum:
            first_difference_layer = "fc"
            break
    if first_difference_layer is None:
        for error in score_errors[1:]:
            if abs(error - score_errors[0]) > tolerance:
                first_difference_layer = "fc"
                break
    if first_difference_layer is not None:
        raise DiagnosticError(
            E_SEMANTIC_DRIFT,
            (
                "v1/v2/v3 semantic equivalence failed at "
                f"{first_difference_layer}"
            ),
            layer=first_difference_layer,
            details={
                "snapshots": [snapshot.version for snapshot in candidates],
                "score_errors": score_errors,
                "layer_checksums": [
                    [layer.activation_checksum for layer in result.layers]
                    for result in results
                ],
            },
        )
    return {
        "equivalent": True,
        "tolerance": tolerance,
        "snapshots": [snapshot.version for snapshot in candidates],
        "scores_checksums": [result.scores_checksum for result in results],
        "layer_checksums": [
            [layer.activation_checksum for layer in result.layers]
            for result in results
        ],
        "score_errors": score_errors,
        "spectra": spectra,
    }


def verify_trace(
    engine: DeterministicEngine,
    snapshot: Snapshot | SnapshotStore,
    record: TraceRecord | str | None,
    *,
    cache: LayerCache | None = None,
) -> dict[str, Any]:
    if isinstance(snapshot, SnapshotStore):
        current_snapshot = snapshot.current
        if current_snapshot is None:
            raise DiagnosticError(
                E_LINEAGE_BROKEN,
                "snapshot store has no committed snapshot",
                details={"first_deviation_layer": None, "deviation": None},
            )
        snapshot = current_snapshot
    if isinstance(record, str):
        record = engine.traces.get(record)
    if record is None:
        raise DiagnosticError(
            E_LINEAGE_BROKEN,
            "trace record is missing",
            details={"first_deviation_layer": None, "deviation": None},
        )
    if tuple(layer.layer for layer in record.layers) != LAYER_ORDER:
        raise DiagnosticError(
            E_LINEAGE_BROKEN,
            "trace layer sequence is incomplete or out of order",
            layer=record.layers[0].layer if record.layers else None,
            details={
                "first_deviation_layer": (
                    record.layers[0].layer if record.layers else None
                ),
                "deviation": None,
            },
        )
    if tuple(name for name, _, _ in record.layer_outputs) != LAYER_ORDER:
        raise DiagnosticError(
            E_LINEAGE_BROKEN,
            "trace layer product set is incomplete or out of order",
            details={"first_deviation_layer": None, "deviation": None},
        )

    for layer_trace in record.layers:
        try:
            current = snapshot.layer(layer_trace.layer)
        except KeyError as exc:
            raise DiagnosticError(
                E_LINEAGE_BROKEN,
                f"trace references absent layer {layer_trace.layer!r}",
                layer=layer_trace.layer,
                details={"first_deviation_layer": layer_trace.layer},
            ) from exc
        checks = (
            ("quant_version", layer_trace.quant_version, current.quant_version),
            (
                "calibration_version",
                layer_trace.calibration_version,
                current.calibration_version,
            ),
            ("per_channel", layer_trace.per_channel, current.per_channel),
            (
                "dynamic_range_mode",
                layer_trace.dynamic_range_mode,
                current.dynamic_range_mode,
            ),
        )
        for dependency, source_version, current_version in checks:
            if source_version != current_version:
                raise DiagnosticError(
                    E_STALE_RESULT,
                    (
                        f"result for layer {layer_trace.layer!r} is stale: "
                        f"{dependency} changed"
                    ),
                    layer=layer_trace.layer,
                    source_version=source_version,
                    current_version=current_version,
                    details={
                        "dependency": dependency,
                        "record_id": record.record_id,
                        "snapshot_version": snapshot.version,
                    },
                )

    try:
        images = decode_array(record.input_b64, record.input_shape)
    except Exception as exc:
        raise DiagnosticError(
            E_LINEAGE_BROKEN,
            f"trace input cannot be decoded: {exc}",
            details={"first_deviation_layer": None, "deviation": None},
        ) from exc

    recorded_scores = np.asarray(record.scores, dtype=np.float64)
    if array_checksum(images) != record.batch_hash:
        raise DiagnosticError(
            E_LINEAGE_BROKEN,
            "recorded trace input does not match the recorded batch hash",
            layer=record.layers[0].layer if record.layers else None,
            details={
                "first_deviation_layer": (
                    record.layers[0].layer if record.layers else None
                ),
                "deviation": None,
            },
        )

    replay = engine.infer(snapshot, images, cache=LayerCache())
    _, replay_outputs = engine.forward_with_outputs(snapshot, images)
    try:
        recorded_outputs = {
            name: decode_array(payload, shape)
            for name, shape, payload in record.layer_outputs
        }
    except Exception as exc:
        raise DiagnosticError(
            E_LINEAGE_BROKEN,
            f"trace layer products cannot be decoded: {exc}",
            details={"first_deviation_layer": None, "deviation": None},
        ) from exc
    for expected, actual in zip(record.layers, replay.layers):
        if expected.activation_checksum != actual.activation_checksum:
            delta: float | None = (
                max_abs(recorded_outputs[expected.layer], replay_outputs[expected.layer])
                if expected.layer in recorded_outputs
                else None
            )
            if delta is not None and not math.isfinite(delta):
                delta = None
            if delta is None and cache is not None:
                old_artifact = cache.get(expected.cache_key)
                if old_artifact is not None:
                    try:
                        delta = max_abs(
                            old_artifact.output, replay_outputs[expected.layer]
                        )
                    except Exception:
                        delta = None
            raise DiagnosticError(
                E_LINEAGE_BROKEN,
                f"trace replay diverged at layer {expected.layer!r}",
                layer=expected.layer,
                details={
                    "first_deviation_layer": expected.layer,
                    "deviation": delta,
                    "expected_checksum": expected.activation_checksum,
                    "actual_checksum": actual.activation_checksum,
                },
            )
    if sha256_bytes(array_bytes(recorded_scores)) != record.scores_checksum:
        raise DiagnosticError(
            E_LINEAGE_BROKEN,
            "recorded score bytes do not match the trace score checksum",
            layer="fc",
            details={
                "first_deviation_layer": "fc",
                "deviation": max_abs(recorded_scores, replay.scores_array),
                "expected_checksum": record.scores_checksum,
                "actual_checksum": replay.scores_checksum,
            },
        )
    if record.scores_checksum != replay.scores_checksum:
        delta = max_abs(recorded_scores, replay.scores_array)
        raise DiagnosticError(
            E_LINEAGE_BROKEN,
            "trace replay final scores differ from the recorded scores",
            layer="fc",
            details={
                "first_deviation_layer": "fc",
                "deviation": delta,
                "expected_checksum": record.scores_checksum,
                "actual_checksum": replay.scores_checksum,
            },
        )
    if record.result_checksum != replay.result_checksum:
        raise DiagnosticError(
            E_LINEAGE_BROKEN,
            "trace replay result checksum differs",
            layer="fc",
            details={
                "first_deviation_layer": "fc",
                "deviation": 0.0,
                "expected_checksum": record.result_checksum,
                "actual_checksum": replay.result_checksum,
            },
        )
    return {
        "replayed": True,
        "record_id": record.record_id,
        "scores_checksum": replay.scores_checksum,
        "layer_checksums": [layer.activation_checksum for layer in replay.layers],
        "scores": [list(row) for row in replay.scores],
    }
