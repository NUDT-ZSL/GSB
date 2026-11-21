from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .errors import E_CALIBRATION_SHAPE_MISMATCH, E_INVALID_MANIFEST, BusinessError
from .manifest import LayerSpec, ManifestView, write_manifest
from .util import json_digest


DEFAULT_MODEL_TAG = "tiny-llm-lab"
DEFAULT_LAYERS: tuple[dict[str, Any], ...] = (
    {
        "layer_id": "layer_0",
        "layer_shape": (48, 32),
        "quant_dtype": "int8",
        "scale_hint": 1.0,
        "zero_point": 0,
        "calib_seed": 1103,
    },
    {
        "layer_id": "layer_1",
        "layer_shape": (40, 48),
        "quant_dtype": "int4",
        "scale_hint": 1.02,
        "zero_point": 0,
        "calib_seed": 2207,
    },
    {
        "layer_id": "layer_2",
        "layer_shape": (24, 40),
        "quant_dtype": "uint4",
        "scale_hint": 0.98,
        "zero_point": 8,
        "calib_seed": 3301,
    },
    {
        "layer_id": "layer_3",
        "layer_shape": (16, 24),
        "quant_dtype": "int8",
        "scale_hint": 1.01,
        "zero_point": 0,
        "calib_seed": 4409,
    },
)


@dataclass(frozen=True)
class CalibrationSample:
    sample_id: str
    model_tag: str
    layer_id: str
    layer_index: int
    vector: tuple[float, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "model_tag": self.model_tag,
            "layer_id": self.layer_id,
            "layer_index": self.layer_index,
            "vector": list(self.vector),
        }


def _vector_seed(model_tag: str, layer_id: str, layer_index: int, calib_seed: int) -> int:
    material = f"{model_tag}|{layer_id}|{layer_index}|{calib_seed}|calibration-v1".encode(
        "utf-8"
    )
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big")


def generate_calibration_samples(
    view: ManifestView, *, samples_per_layer: int = 6
) -> list[CalibrationSample]:
    rows: list[CalibrationSample] = []
    for layer in view.layers:
        rng = np.random.default_rng(
            _vector_seed(
                layer.model_tag, layer.layer_id, layer.layer_index, layer.calib_seed
            )
        )
        for sample_index in range(samples_per_layer):
            vector = rng.standard_normal(layer.input_dim, dtype=np.float64)
            vector = np.tanh(vector * 0.65)
            rows.append(
                CalibrationSample(
                    sample_id=f"cal-{layer.layer_index:02d}-{sample_index:03d}",
                    model_tag=layer.model_tag,
                    layer_id=layer.layer_id,
                    layer_index=layer.layer_index,
                    vector=tuple(float(value) for value in vector),
                )
            )
    return rows


def default_manifest() -> ManifestView:
    rows = []
    for index, item in enumerate(DEFAULT_LAYERS):
        row = {
            "model_tag": DEFAULT_MODEL_TAG,
            "layer_id": item["layer_id"],
            "layer_index": index,
            "layer_shape": list(item["layer_shape"]),
            "weight_revision": 1,
            "quant_dtype": item["quant_dtype"],
            "scale_hint": item["scale_hint"],
            "zero_point": item["zero_point"],
            "calib_seed": item["calib_seed"],
        }
        rows.append(row)
    view = ManifestView(
        model_tag=DEFAULT_MODEL_TAG,
        layers=tuple(LayerSpec(**row) for row in rows),
    )
    view.validate_structure()
    return view


def write_default_samples(
    root: Path, *, samples_per_layer: int = 6
) -> tuple[Path, Path]:
    manifest_path = root / "model_manifest.jsonl"
    calibration_path = root / "calibration_samples.jsonl"
    view = default_manifest()
    write_manifest(manifest_path, view)
    samples = generate_calibration_samples(view, samples_per_layer=samples_per_layer)
    calibration_path.parent.mkdir(parents=True, exist_ok=True)
    with calibration_path.open("w", encoding="utf-8", newline="\n") as handle:
        for sample in samples:
            handle.write(
                json.dumps(
                    sample.as_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
    return manifest_path, calibration_path


def load_calibration(
    path: Path, view: ManifestView | None = None
) -> list[CalibrationSample]:
    rows: list[CalibrationSample] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    row = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    raise BusinessError(
                        E_CALIBRATION_SHAPE_MISMATCH,
                        f"calibration line {line_number} is not valid JSON",
                        stage="calibration_statistics",
                        details={"line": line_number},
                    ) from exc
                try:
                    vector = tuple(float(value) for value in row["vector"])
                    sample = CalibrationSample(
                        sample_id=str(row["sample_id"]),
                        model_tag=str(row["model_tag"]),
                        layer_id=str(row["layer_id"]),
                        layer_index=int(row["layer_index"]),
                        vector=vector,
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise BusinessError(
                        E_CALIBRATION_SHAPE_MISMATCH,
                        f"calibration line {line_number} has invalid fields",
                        stage="calibration_statistics",
                        details={"line": line_number},
                    ) from exc
                rows.append(sample)
    except FileNotFoundError as exc:
        raise BusinessError(
            E_CALIBRATION_SHAPE_MISMATCH,
            f"calibration file not found: {path}",
            stage="calibration_statistics",
        ) from exc
    rows.sort(key=lambda item: (item.layer_index, item.sample_id))
    if view is not None:
        known = set(view.layer_ids)
        for sample in rows:
            if sample.layer_id not in known:
                raise BusinessError(
                    E_CALIBRATION_SHAPE_MISMATCH,
                    "calibration sample references an unknown layer",
                    stage="calibration_statistics",
                    layer_id=sample.layer_id,
                    layer_index=sample.layer_index,
                )
            if sample.model_tag != view.model_tag:
                raise BusinessError(
                    E_CALIBRATION_SHAPE_MISMATCH,
                    "calibration sample model_tag does not match manifest",
                    stage="calibration_statistics",
                    layer_id=sample.layer_id,
                    layer_index=sample.layer_index,
                )
    return rows


def write_calibration(path: Path, samples: list[CalibrationSample]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for sample in sorted(samples, key=lambda item: (item.layer_index, item.sample_id)):
            handle.write(
                json.dumps(
                    sample.as_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )


def samples_by_layer(
    samples: list[CalibrationSample], view: ManifestView
) -> dict[str, list[CalibrationSample]]:
    grouped: dict[str, list[CalibrationSample]] = {layer_id: [] for layer_id in view.layer_ids}
    for sample in samples:
        grouped.setdefault(sample.layer_id, []).append(sample)
    for layer_id in grouped:
        grouped[layer_id].sort(key=lambda item: item.sample_id)
    return grouped


def sample_layer_digest(samples: list[CalibrationSample]) -> str:
    return json_digest([sample.as_dict() for sample in sorted(samples, key=lambda item: item.sample_id)])


def calibration_vectors(
    samples: list[CalibrationSample],
    view: ManifestView,
    layer,
    config,
    *,
    force_count_mismatch: bool = False,
) -> np.ndarray:
    selected = [
        item for item in samples if item.layer_id == layer.layer_id
    ]
    selected.sort(key=lambda item: item.sample_id)
    if force_count_mismatch:
        selected = selected[: max(0, config.min_calibration_samples - 1)]
    if len(selected) < config.min_calibration_samples:
        raise BusinessError(
            E_CALIBRATION_SHAPE_MISMATCH,
            "calibration sample count is below the configured minimum",
            stage="calibration_statistics",
            layer_id=layer.layer_id,
            layer_index=layer.layer_index,
            details={
                "sample_count": len(selected),
                "minimum": config.min_calibration_samples,
            },
            trace=["calibration:shape_guard"],
        )
    vectors = []
    for sample in selected:
        if len(sample.vector) != layer.input_dim:
            raise BusinessError(
                E_CALIBRATION_SHAPE_MISMATCH,
                "calibration sample width does not match layer input dimension",
                stage="calibration_statistics",
                layer_id=layer.layer_id,
                layer_index=layer.layer_index,
                details={
                    "sample_id": sample.sample_id,
                    "sample_width": len(sample.vector),
                    "expected_width": layer.input_dim,
                },
                trace=["calibration:shape_guard"],
            )
        vectors.append(np.asarray(sample.vector, dtype=np.float64))
    return np.ascontiguousarray(np.stack(vectors), dtype=np.float64)
