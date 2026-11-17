from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import numpy as np

from .config import DataShard, TrainingConfig
from .errors import ConfigError
from .io import canonical_json_bytes, sha256_bytes


def _substitute(template: str, values: dict[str, int]) -> str:
    try:
        return template.format(**values)
    except KeyError as exc:
        raise ConfigError(f"unknown tensor template variable {exc.args[0]!r} in {template!r}") from exc
    except ValueError as exc:
        raise ConfigError(f"invalid tensor template {template!r}: {exc}") from exc


def _resolve_dimension(token: str, values: dict[str, int]) -> int:
    if token.isdigit():
        return int(token)
    if token not in values:
        raise ConfigError(f"unknown tensor dimension variable {token!r}")
    value = values[token]
    if value < 1:
        raise ConfigError(f"tensor dimension {token!r} resolved to non-positive value")
    return value


def _deterministic_int64(material: bytes, count: int, span: int) -> np.ndarray:
    if count == 0:
        return np.empty(0, dtype=np.int64)
    raw = hashlib.shake_256(material).digest(count * 8)
    values = np.frombuffer(raw, dtype="<i8").astype(np.int64, copy=True)
    modulus = 2 * span + 1
    return values % modulus - span


@dataclass(frozen=True)
class TensorInfo:
    name: str
    shape: tuple[int, ...]
    offset: int
    size_bytes: int

    @property
    def numel(self) -> int:
        return int(np.prod(self.shape, dtype=np.int64))

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "shape": list(self.shape),
            "offset": self.offset,
            "size_bytes": self.size_bytes,
        }


class DeterministicModel:
    """An integer, fixed-point parameter store with SHAKE-derived updates."""

    def __init__(self, config: TrainingConfig):
        self.config = config
        self.tensors = self._build_layout(config)
        self.parameter_bytes = sum(item.size_bytes for item in self.tensors)
        self.params = np.empty(self.parameter_bytes // 8, dtype=np.int64)
        for tensor in self.tensors:
            first = tensor.offset // 8
            count = tensor.numel
            material = canonical_json_bytes(
                {
                    "kind": "init",
                    "seed": config.seed,
                    "name": tensor.name,
                    "shape": list(tensor.shape),
                    "scale": config.fixed_point_scale,
                }
            )
            self.params[first : first + count] = _deterministic_int64(
                material, count, config.fixed_point_scale
            )

    @staticmethod
    def _build_layout(config: TrainingConfig) -> tuple[TensorInfo, ...]:
        layout: list[TensorInfo] = []
        offset = 0
        seen: set[str] = set()
        for spec in config.tensor_specs:
            layer_indices = range(config.layers) if "{layer}" in spec.name or any(
                "{layer}" in token for token in spec.shape_template
            ) else (None,)
            for layer_index in layer_indices:
                values = {
                    "layers": config.layers,
                    "hidden_size": config.hidden_size,
                    "vocab_size": config.vocab_size,
                    "ffn_size": config.hidden_size * 2,
                }
                if layer_index is not None:
                    values["layer"] = layer_index
                name = _substitute(spec.name, values)
                shape = tuple(
                    _resolve_dimension(_substitute(token, values), values)
                    for token in spec.shape_template
                )
                if name in seen:
                    raise ConfigError(f"expanded tensor name {name!r} is duplicated")
                seen.add(name)
                numel = int(np.prod(shape, dtype=np.int64))
                size_bytes = numel * 8
                if size_bytes % 8 != 0:
                    raise ConfigError(f"tensor {name!r} is not aligned to 8 bytes")
                layout.append(TensorInfo(name=name, shape=shape, offset=offset, size_bytes=size_bytes))
                offset += size_bytes
        if offset == 0:
            raise ConfigError("model layout is empty")
        return tuple(layout)

    @property
    def digest(self) -> str:
        return sha256_bytes(self.to_bytes())

    def to_bytes(self) -> bytes:
        return self.params.astype("<i8", copy=False).tobytes(order="C")

    def tensor_bytes(self, tensor: TensorInfo) -> bytes:
        first = tensor.offset // 8
        count = tensor.numel
        return self.params[first : first + count].astype("<i8", copy=False).tobytes(order="C")

    def apply_round(self, round_number: int, data_shards: list[DataShard]) -> dict[str, Any]:
        if round_number < 1:
            raise ValueError("round_number must be >= 1")
        worker_updates: list[dict[str, Any]] = []
        for worker_id in range(self.config.workers):
            shard = data_shards[((round_number - 1) * self.config.workers + worker_id) % len(data_shards)]
            worker_digest = hashlib.sha256()
            for tensor in self.tensors:
                material = canonical_json_bytes(
                    {
                        "kind": "pseudo_gradient",
                        "seed": self.config.seed,
                        "round": round_number,
                        "worker": worker_id,
                        "data_shard": shard.shard_id,
                        "sample_count": shard.sample_count,
                        "feature_digest": shard.feature_digest,
                        "label_digest": shard.label_digest,
                        "tensor": tensor.name,
                        "shape": list(tensor.shape),
                    }
                )
                count = tensor.numel
                delta_nano = _deterministic_int64(material, count, 5_000_000)
                first = tensor.offset // 8
                segment = self.params[first : first + count]
                raw_sum = segment * 1000 + delta_nano
                rounded = np.empty_like(raw_sum)
                nonnegative = raw_sum >= 0
                rounded[nonnegative] = (raw_sum[nonnegative] + 500) // 1000
                rounded[~nonnegative] = -((-raw_sum[~nonnegative] + 500) // 1000)
                self.params[first : first + count] = rounded
                worker_digest.update(sha256_bytes(delta_nano.astype("<i8", copy=False).tobytes()).encode("ascii"))
            worker_updates.append(
                {
                    "worker_id": worker_id,
                    "data_shard": shard.shard_id,
                    "sample_count": shard.sample_count,
                    "gradient_digest": worker_digest.hexdigest(),
                    "parameter_digest_after_worker": self.digest,
                }
            )
        return {
            "round": round_number,
            "worker_updates": worker_updates,
            "parameter_digest": self.digest,
        }

    @classmethod
    def from_bytes(cls, config: TrainingConfig, payload: bytes) -> tuple["DeterministicModel", list[str]]:
        model = cls(config)
        if len(payload) != model.parameter_bytes:
            raise ValueError(
                f"parameter payload has {len(payload)} bytes, expected {model.parameter_bytes}"
            )
        model.params = np.frombuffer(payload, dtype="<i8").astype(np.int64, copy=True)
        return model, []

    def layout_dict(self) -> dict[str, Any]:
        return {
            "parameter_bytes": self.parameter_bytes,
            "numel": int(self.params.size),
            "tensors": [tensor.as_dict() for tensor in self.tensors],
            "parameter_digest": self.digest,
        }
