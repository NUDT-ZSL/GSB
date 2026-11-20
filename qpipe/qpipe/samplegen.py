"""Deterministic local sample generation.

All artefacts derive from a fixed seed and the declared layer shapes through
explicit :class:`numpy.random.Generator` instances.  No global RNG, wall clock
or network access is involved, so regenerating overwrites byte-identical files.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .deterministic import checksum_array
from .errors import E_PACKAGE_FORMAT, PipelineError
from .models import LayerSpec, ModelPackage

DEFAULT_SEED = 20261121


def stable_rng(*parts: int) -> np.random.Generator:
    """Derive an independent, reproducible RNG from integer seed parts."""
    digest = 0
    for part in parts:
        digest = (digest * 1_000_003) ^ int(part)
        digest &= 0xFFFFFFFFFFFFFFFF
    return np.random.default_rng(digest)


def default_layer_shapes(
    width: int = 8, depth: int = 5
) -> List[Tuple[int, int]]:
    """A small MLP: ``width`` wide hidden layers and a 2-class head."""
    shapes: List[Tuple[int, int]] = []
    for index in range(depth - 1):
        shapes.append((width, width))
    shapes.append((width, 2))
    return shapes


def build_manifest(
    model_tag: str = "mlp-det-001",
    shapes: Optional[Sequence[Tuple[int, int]]] = None,
    *,
    seed: int = DEFAULT_SEED,
    revisions: Optional[Dict[int, int]] = None,
    calibration_split: int = 4,
) -> ModelPackage:
    """Construct a :class:`ModelPackage` with deterministic quant settings."""
    shapes = list(shapes or default_layer_shapes())
    revisions = revisions or {}
    layers: List[LayerSpec] = []
    for index, shape in enumerate(shapes):
        rng = stable_rng(seed, index)
        # Pick quant dtype deterministically per layer (int8 / uint8 / int16).
        dtype_choice = ("int8", "uint8", "int16")[index % 3]
        scale_hint = float(
            np.round(0.02 + rng.uniform(0.0, 0.05), decimals=6)
        )
        zero_point = 0 if dtype_choice.startswith("int") else 128
        layers.append(
            LayerSpec(
                model_tag=model_tag,
                layer_id=f"L{index:02d}",
                layer_index=index,
                layer_shape=(int(shape[0]), int(shape[1])),
                weight_revision=int(revisions.get(index, 1)),
                quant_dtype=dtype_choice,
                zero_point=zero_point,
                scale_hint=scale_hint,
                calibration_split=calibration_split,
            )
        )
    return ModelPackage(model_tag=model_tag, layers=tuple(layers))


def generate_weight(
    spec: LayerSpec, *, seed: int = DEFAULT_SEED
) -> np.ndarray:
    """Generate the float32 weight matrix for one layer/revision."""
    rng = stable_rng(
        seed, spec.layer_index, spec.weight_revision, spec.numel()
    )
    weight = rng.normal(
        loc=0.0, scale=0.5, size=spec.layer_shape
    ).astype(np.float32)
    return np.ascontiguousarray(weight)


def generate_calibration_input(
    package: ModelPackage, *, seed: int = DEFAULT_SEED
) -> np.ndarray:
    """Deterministic calibration tensor ``(split, first-layer in_features)``."""
    first = package.layers[0]
    rng = stable_rng(seed, 9917, first.in_features, first.calibration_split)
    tensor = rng.uniform(
        -1.0, 1.0, size=(first.calibration_split, first.in_features)
    ).astype(np.float32)
    return np.ascontiguousarray(tensor)


def _write_jsonl(path: str, rows: List[Dict[str, Any]]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _write_npy(path: str, arr: np.ndarray) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as handle:
        np.save(handle, arr, allow_pickle=False)
    os.replace(tmp, path)


def weight_filename(spec: LayerSpec) -> str:
    return (
        f"{spec.layer_id}_r{spec.weight_revision}.npy"
    )


def generate_workspace(
    out_dir: str,
    *,
    model_tag: str = "mlp-det-001",
    shapes: Optional[Sequence[Tuple[int, int]]] = None,
    seed: int = DEFAULT_SEED,
    revisions: Optional[Dict[int, int]] = None,
    calibration_split: int = 4,
    depth: Optional[int] = None,
    width: int = 8,
) -> Dict[str, Any]:
    """Create manifest JSONL, per-layer ``.npy`` weights and calibration input.

    Returns a small descriptor (paths + weight checksums) useful in tests.
    """
    if shapes is None and depth is not None:
        shapes = default_layer_shapes(width=width, depth=depth)
    package = build_manifest(
        model_tag,
        shapes,
        seed=seed,
        revisions=revisions,
        calibration_split=calibration_split,
    )
    weights_dir = os.path.join(out_dir, "weights")
    os.makedirs(weights_dir, exist_ok=True)

    rows = [spec.to_manifest_dict() for spec in package.layers]
    manifest_path = os.path.join(out_dir, "manifest.jsonl")
    _write_jsonl(manifest_path, rows)

    weight_checksums: Dict[str, str] = {}
    for spec in package.layers:
        weight = generate_weight(spec, seed=seed)
        path = os.path.join(weights_dir, weight_filename(spec))
        _write_npy(path, weight)
        weight_checksums[spec.layer_id] = checksum_array(weight)

    calibration = generate_calibration_input(package, seed=seed)
    cal_path = os.path.join(out_dir, "calibration.npy")
    _write_npy(cal_path, calibration)

    descriptor = {
        "out_dir": out_dir,
        "manifest": manifest_path,
        "weights_dir": weights_dir,
        "calibration": cal_path,
        "seed": seed,
        "model_tag": model_tag,
        "layer_count": package.layer_count,
        "weight_checksums": weight_checksums,
        "calibration_checksum": checksum_array(calibration),
    }
    with open(
        os.path.join(out_dir, "sample_descriptor.json"), "w", encoding="utf-8"
    ) as handle:
        json.dump(descriptor, handle, sort_keys=True, indent=2)
    return descriptor


def load_manifest(path: str) -> ModelPackage:
    """Load and order a manifest JSONL without performing deep validation."""
    if not os.path.exists(path):
        raise PipelineError(
            E_PACKAGE_FORMAT,
            f"manifest not found: {path}",
            stage="validate",
        )
    rows: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise PipelineError(
                    E_PACKAGE_FORMAT,
                    f"manifest line {line_number} is not valid JSON: {exc}",
                    stage="validate",
                ) from exc
    specs = [LayerSpec.from_manifest_dict(row) for row in rows]
    specs.sort(key=lambda spec: spec.layer_index)
    indices = [spec.layer_index for spec in specs]
    if len(set(indices)) != len(indices):
        raise PipelineError(
            E_PACKAGE_FORMAT,
            "duplicate layer_index values in manifest",
            stage="validate",
        )
    if indices != list(range(len(specs))):
        raise PipelineError(
            E_PACKAGE_FORMAT,
            f"layer_index must be contiguous 0..N-1, got {indices}",
            stage="validate",
        )
    tags = {spec.model_tag for spec in specs}
    if len(tags) != 1:
        raise PipelineError(
            E_PACKAGE_FORMAT,
            f"mixed model_tag values in manifest: {sorted(tags)}",
            stage="validate",
        )
    return ModelPackage(model_tag=specs[0].model_tag, layers=tuple(specs))


def load_weight(weights_dir: str, spec: LayerSpec) -> np.ndarray:
    path = os.path.join(weights_dir, weight_filename(spec))
    if not os.path.exists(path):
        raise PipelineError(
            E_PACKAGE_FORMAT,
            f"weight file missing for layer {spec.layer_id}: {path}",
            stage="validate",
            model_tag=spec.model_tag,
            layer_id=spec.layer_id,
            layer_index=spec.layer_index,
        )
    arr = np.load(path, allow_pickle=False)
    return np.ascontiguousarray(arr, dtype=np.float32)


def load_calibration(path: str) -> np.ndarray:
    return np.ascontiguousarray(np.load(path, allow_pickle=False), dtype=np.float32)
