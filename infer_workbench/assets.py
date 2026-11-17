"""Deterministic local samples, model parameters, and integrity checks."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np

from .canonical import atomic_write_json, digest_float32_sequence
from .errors import WorkbenchError


DATASET_SCHEMA = "infer-workbench.dataset.v1"
MODEL_SCHEMA = "infer-workbench.model.v1"
MICRO = 1_000_000
GOLDEN = 0x9E3779B97F4A7C15
MASK64 = (1 << 64) - 1


class StableRandom:
    """Small SplitMix64 generator, insulated from NumPy RNG algorithm changes."""

    def __init__(self, seed: int):
        self.state = int(seed) & MASK64

    def next_u64(self) -> int:
        self.state = (self.state + GOLDEN) & MASK64
        value = self.state
        value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
        value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & MASK64
        return (value ^ (value >> 31)) & MASK64

    def randint(self, low: int, high: int) -> int:
        if high < low:
            raise ValueError("high must not be smaller than low")
        span = high - low + 1
        return low + (self.next_u64() % span)


@dataclass(frozen=True)
class Sample:
    sample_id: str
    values: np.ndarray


@dataclass(frozen=True)
class LayerParameters:
    name: str
    weight: np.ndarray
    bias: np.ndarray


@dataclass(frozen=True)
class Assets:
    generation: dict[str, Any]
    samples: tuple[Sample, ...]
    layers: tuple[LayerParameters, ...]
    data_digest: str
    parameter_digest: str

    @property
    def sample_count(self) -> int:
        return len(self.samples)

    @property
    def input_dim(self) -> int:
        return int(self.samples[0].values.size)

    @property
    def output_dim(self) -> int:
        return int(self.layers[-1].bias.size)

    @property
    def layer_names(self) -> tuple[str, ...]:
        return tuple(layer.name for layer in self.layers)

    @property
    def hidden_dims(self) -> tuple[int, ...]:
        return tuple(int(layer.bias.size) for layer in self.layers[:-1])

    def sample_by_id(self) -> dict[str, Sample]:
        return {sample.sample_id: sample for sample in self.samples}


def _validate_generation_parameters(
    seed: int,
    sample_count: int,
    input_dim: int,
    hidden_dims: Sequence[int],
    output_dim: int,
) -> tuple[int, ...]:
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise WorkbenchError("E_CONFIG", "随机种子必须为整数", {"field": "seed"})
    if isinstance(sample_count, bool) or not isinstance(sample_count, int) or sample_count <= 0:
        raise WorkbenchError("E_CONFIG", "样本数必须为正整数", {"field": "sample_count"})
    if isinstance(input_dim, bool) or not isinstance(input_dim, int) or input_dim <= 0:
        raise WorkbenchError("E_CONFIG", "输入维度必须为正整数", {"field": "input_dim"})
    if isinstance(output_dim, bool) or not isinstance(output_dim, int) or output_dim <= 0:
        raise WorkbenchError("E_CONFIG", "输出维度必须为正整数", {"field": "output_dim"})
    normalized = tuple(int(dim) for dim in hidden_dims)
    if not normalized or any(dim <= 0 for dim in normalized):
        raise WorkbenchError("E_CONFIG", "隐藏层维度必须为正整数序列", {"field": "hidden_dims"})
    return normalized


def _micro_values(rng: StableRandom, count: int, low: int, high: int) -> np.ndarray:
    values = [rng.randint(low, high) / MICRO for _ in range(count)]
    return np.asarray(values, dtype=np.float32)


def generate_assets(
    *,
    seed: int,
    sample_count: int,
    input_dim: int,
    hidden_dims: Sequence[int] = (6, 5),
    output_dim: int = 3,
) -> Assets:
    """Construct samples and multi-layer MLP parameters deterministically."""

    hidden = _validate_generation_parameters(
        seed, sample_count, input_dim, hidden_dims, output_dim
    )
    sample_rng = StableRandom((seed ^ 0xD1B54A32D192ED03) & MASK64)
    parameter_rng = StableRandom((seed ^ 0x94D049BB133111EB) & MASK64)

    samples: list[Sample] = []
    for index in range(sample_count):
        values = _micro_values(sample_rng, input_dim, -1_500_000, 1_500_000)
        samples.append(Sample(sample_id=f"S{index + 1:04d}", values=values))

    dims = [input_dim, *hidden, output_dim]
    layers: list[LayerParameters] = []
    for index, (fan_in, fan_out) in enumerate(zip(dims[:-1], dims[1:]), start=1):
        weight_values = _micro_values(parameter_rng, fan_in * fan_out, -900_000, 900_000)
        bias_values = _micro_values(parameter_rng, fan_out, -250_000, 250_000)
        name = "output" if index == len(dims) - 1 else f"hidden_{index}"
        layers.append(
            LayerParameters(
                name=name,
                weight=weight_values.reshape(fan_out, fan_in),
                bias=bias_values,
            )
        )

    generation = {
        "seed": int(seed),
        "sample_count": int(sample_count),
        "input_dim": int(input_dim),
        "hidden_dims": list(hidden),
        "output_dim": int(output_dim),
        "generator": "SplitMix64-integer-micro-v1",
    }
    data_digest = _calculate_data_digest(samples)
    parameter_digest = _calculate_parameter_digest(layers)
    return Assets(
        generation=generation,
        samples=tuple(samples),
        layers=tuple(layers),
        data_digest=data_digest,
        parameter_digest=parameter_digest,
    )


def _calculate_data_digest(samples: Iterable[Sample]) -> str:
    parts: list[tuple[str, np.ndarray]] = []
    for sample in samples:
        parts.append((sample.sample_id, sample.values))
    return digest_float32_sequence(parts)


def _calculate_parameter_digest(layers: Iterable[LayerParameters]) -> str:
    parts: list[tuple[str, np.ndarray]] = []
    for layer in layers:
        parts.append((f"{layer.name}.weight", layer.weight))
        parts.append((f"{layer.name}.bias", layer.bias))
    return digest_float32_sequence(parts)


def assets_to_dicts(assets: Assets) -> tuple[dict[str, Any], dict[str, Any]]:
    dataset = {
        "schema": DATASET_SCHEMA,
        "generation": assets.generation,
        "data_digest": assets.data_digest,
        "samples": [
            {"sample_id": sample.sample_id, "input": [float(x) for x in sample.values]}
            for sample in assets.samples
        ],
    }
    model = {
        "schema": MODEL_SCHEMA,
        "generation": assets.generation,
        "parameter_digest": assets.parameter_digest,
        "layers": [
            {
                "name": layer.name,
                "weight": [[float(value) for value in row] for row in layer.weight],
                "bias": [float(value) for value in layer.bias],
            }
            for layer in assets.layers
        ],
    }
    return dataset, model


def save_assets(data_dir: str | Path, assets: Assets) -> tuple[Path, Path]:
    root = Path(data_dir)
    dataset, model = assets_to_dicts(assets)
    dataset_path = atomic_write_json(root / "dataset.json", dataset)
    model_path = atomic_write_json(root / "model.json", model)
    atomic_write_json(root / "generation.json", assets.generation)
    return dataset_path, model_path


def _read_json(path: Path, code: str) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except OSError as exc:
        raise WorkbenchError(code, f"无法读取文件: {path}", {"path": path.as_posix()}) from exc
    except json.JSONDecodeError as exc:
        raise WorkbenchError(
            code,
            f"JSON 解析失败: {path}",
            {"path": path.as_posix(), "line": exc.lineno, "column": exc.colno},
        ) from exc
    if not isinstance(value, dict):
        raise WorkbenchError(code, "JSON 顶层必须为对象", {"path": path.as_posix()})
    return value


def load_assets(
    data_dir: str | Path,
    *,
    expected_data_digest: str | None = None,
    expected_parameter_digest: str | None = None,
) -> Assets:
    root = Path(data_dir)
    dataset = _read_json(root / "dataset.json", "E_DATA")
    model = _read_json(root / "model.json", "E_DATA")
    if dataset.get("schema") != DATASET_SCHEMA:
        raise WorkbenchError("E_DATA", "样本文件 schema 不兼容", {"schema": dataset.get("schema")})
    if model.get("schema") != MODEL_SCHEMA:
        raise WorkbenchError("E_DATA", "参数文件 schema 不兼容", {"schema": model.get("schema")})

    generation = dataset.get("generation")
    if not isinstance(generation, dict):
        raise WorkbenchError("E_DATA", "样本文件缺少 generation 元数据")
    if model.get("generation") != generation:
        raise WorkbenchError("E_DATA", "样本与参数的 generation 元数据不一致")

    raw_samples = dataset.get("samples")
    if not isinstance(raw_samples, list) or not raw_samples:
        raise WorkbenchError("E_DATA", "样本列表为空或类型非法")
    samples: list[Sample] = []
    seen_ids: set[str] = set()
    input_dim: int | None = None
    for index, item in enumerate(raw_samples):
        if not isinstance(item, dict):
            raise WorkbenchError("E_DATA", "样本记录类型非法", {"index": index})
        sample_id = item.get("sample_id")
        values = item.get("input")
        if not isinstance(sample_id, str) or not sample_id:
            raise WorkbenchError("E_DATA", "样本标识非法", {"index": index})
        if sample_id in seen_ids:
            raise WorkbenchError("E_DATA", "样本标识重复", {"sample_id": sample_id})
        seen_ids.add(sample_id)
        if not isinstance(values, list) or not values:
            raise WorkbenchError("E_DATA", "样本输入非法", {"sample_id": sample_id})
        try:
            array = np.asarray(values, dtype=np.float32)
        except (TypeError, ValueError) as exc:
            raise WorkbenchError("E_DATA", "样本输入包含非数值", {"sample_id": sample_id}) from exc
        if array.ndim != 1 or not np.all(np.isfinite(array)):
            raise WorkbenchError("E_DATA", "样本输入必须是一维有限数组", {"sample_id": sample_id})
        if input_dim is None:
            input_dim = int(array.size)
        elif array.size != input_dim:
            raise WorkbenchError(
                "E_DATA", "样本输入维度不一致", {"sample_id": sample_id, "expected": input_dim}
            )
        samples.append(Sample(sample_id=sample_id, values=array))

    raw_layers = model.get("layers")
    if not isinstance(raw_layers, list) or not raw_layers:
        raise WorkbenchError("E_DATA", "模型层列表为空或类型非法")
    layers: list[LayerParameters] = []
    previous_dim = input_dim
    layer_names: set[str] = set()
    for layer_index, item in enumerate(raw_layers):
        if not isinstance(item, dict):
            raise WorkbenchError("E_DATA", "模型层类型非法", {"layer_index": layer_index})
        name = item.get("name")
        weight_values = item.get("weight")
        bias_values = item.get("bias")
        if not isinstance(name, str) or not name or name in layer_names:
            raise WorkbenchError("E_DATA", "模型层名称非法或重复", {"layer_index": layer_index})
        layer_names.add(name)
        try:
            weight = np.asarray(weight_values, dtype=np.float32)
            bias = np.asarray(bias_values, dtype=np.float32)
        except (TypeError, ValueError) as exc:
            raise WorkbenchError("E_DATA", "模型层包含非数值", {"layer": name}) from exc
        if weight.ndim != 2 or bias.ndim != 1:
            raise WorkbenchError("E_DATA", "模型权重/偏置维度非法", {"layer": name})
        if weight.shape[0] != bias.size or weight.shape[1] != previous_dim:
            raise WorkbenchError(
                "E_DATA",
                "模型层形状不连续",
                {"layer": name, "weight_shape": list(weight.shape), "expected_fan_in": previous_dim},
            )
        if not np.all(np.isfinite(weight)) or not np.all(np.isfinite(bias)):
            raise WorkbenchError("E_DATA", "模型参数包含非有限值", {"layer": name})
        layers.append(LayerParameters(name=name, weight=weight, bias=bias))
        previous_dim = int(bias.size)

    data_digest = _calculate_data_digest(samples)
    parameter_digest = _calculate_parameter_digest(layers)
    declared_data = dataset.get("data_digest")
    declared_parameter = model.get("parameter_digest")
    checks = [
        ("data_digest", declared_data, data_digest, expected_data_digest),
        ("parameter_digest", declared_parameter, parameter_digest, expected_parameter_digest),
    ]
    for field, declared, actual, expected in checks:
        if declared != actual:
            raise WorkbenchError(
                "E_DATA",
                f"{field} 与当前文件内容不一致，拒绝本次推理",
                {"field": field, "declared": declared, "actual": actual},
            )
        if expected is not None and expected != actual:
            raise WorkbenchError(
                "E_DATA",
                f"{field} 与调用方期望值不一致，拒绝本次推理",
                {"field": field, "expected": expected, "actual": actual},
            )

    normalized_generation = {
        **generation,
        "sample_count": len(samples),
        "input_dim": int(input_dim or 0),
        "hidden_dims": [int(layer.bias.size) for layer in layers[:-1]],
        "output_dim": int(layers[-1].bias.size),
    }
    return Assets(
        generation=normalized_generation,
        samples=tuple(samples),
        layers=tuple(layers),
        data_digest=data_digest,
        parameter_digest=parameter_digest,
    )


def load_generation_file(path: str | Path) -> dict[str, Any]:
    return _read_json(Path(path), "E_CONFIG")
