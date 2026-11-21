from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .errors import E_INVALID_MANIFEST, BusinessError
from .util import json_digest


REQUIRED_MANIFEST_FIELDS = (
    "model_tag",
    "layer_id",
    "layer_index",
    "layer_shape",
    "weight_revision",
    "quant_dtype",
    "scale_hint",
    "zero_point",
    "calib_seed",
)


@dataclass(frozen=True)
class LayerSpec:
    model_tag: str
    layer_id: str
    layer_index: int
    layer_shape: tuple[int, int]
    weight_revision: int
    quant_dtype: str
    scale_hint: float
    zero_point: int
    calib_seed: int

    @property
    def input_dim(self) -> int:
        return self.layer_shape[1]

    @property
    def output_dim(self) -> int:
        return self.layer_shape[0]

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_tag": self.model_tag,
            "layer_id": self.layer_id,
            "layer_index": self.layer_index,
            "layer_shape": list(self.layer_shape),
            "weight_revision": self.weight_revision,
            "quant_dtype": self.quant_dtype,
            "scale_hint": self.scale_hint,
            "zero_point": self.zero_point,
            "calib_seed": self.calib_seed,
        }

    def weight_identity(self) -> dict[str, Any]:
        return {
            "model_tag": self.model_tag,
            "layer_id": self.layer_id,
            "layer_index": self.layer_index,
            "layer_shape": list(self.layer_shape),
            "weight_revision": self.weight_revision,
            "quant_dtype": self.quant_dtype,
            "scale_hint": self.scale_hint,
            "zero_point": self.zero_point,
            "calib_seed": self.calib_seed,
        }


@dataclass(frozen=True)
class ManifestView:
    model_tag: str
    layers: tuple[LayerSpec, ...]

    @property
    def layer_ids(self) -> list[str]:
        return [layer.layer_id for layer in self.layers]

    @property
    def digest(self) -> str:
        return json_digest([layer.as_dict() for layer in self.layers])

    def by_id(self, layer_id: str) -> LayerSpec:
        for layer in self.layers:
            if layer.layer_id == layer_id:
                return layer
        raise BusinessError(
            E_INVALID_MANIFEST,
            f"unknown layer_id: {layer_id}",
            stage="model_package_validation",
            layer_id=layer_id,
        )

    def by_index(self, layer_index: int) -> LayerSpec:
        return self.layers[layer_index]

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_tag": self.model_tag,
            "layers": [layer.as_dict() for layer in self.layers],
        }

    @staticmethod
    def from_layers(model_tag: str, layers: Iterable[LayerSpec]) -> "ManifestView":
        ordered = tuple(sorted(layers, key=lambda item: item.layer_index))
        view = ManifestView(model_tag=model_tag, layers=ordered)
        view.validate_structure()
        return view

    def validate_structure(self) -> None:
        if not self.layers:
            raise BusinessError(
                E_INVALID_MANIFEST,
                "manifest has no layers",
                stage="model_package_validation",
            )
        seen_ids: set[str] = set()
        seen_indices: set[int] = set()
        for layer in self.layers:
            if layer.model_tag != self.model_tag:
                raise BusinessError(
                    E_INVALID_MANIFEST,
                    "model_tag differs inside one manifest",
                    stage="model_package_validation",
                    layer_id=layer.layer_id,
                    layer_index=layer.layer_index,
                    details={
                        "expected_model_tag": self.model_tag,
                        "actual_model_tag": layer.model_tag,
                    },
                )
            if layer.layer_id in seen_ids:
                raise BusinessError(
                    E_INVALID_MANIFEST,
                    "duplicate layer_id",
                    stage="model_package_validation",
                    layer_id=layer.layer_id,
                )
            if layer.layer_index in seen_indices:
                raise BusinessError(
                    E_INVALID_MANIFEST,
                    "duplicate layer_index",
                    stage="model_package_validation",
                    layer_id=layer.layer_id,
                    layer_index=layer.layer_index,
                )
            seen_ids.add(layer.layer_id)
            seen_indices.add(layer.layer_index)
            if layer.weight_revision <= 0:
                raise BusinessError(
                    E_INVALID_MANIFEST,
                    "weight_revision must be positive",
                    stage="model_package_validation",
                    layer_id=layer.layer_id,
                    layer_index=layer.layer_index,
                )
            if layer.scale_hint <= 0:
                raise BusinessError(
                    E_INVALID_MANIFEST,
                    "scale_hint must be positive",
                    stage="model_package_validation",
                    layer_id=layer.layer_id,
                    layer_index=layer.layer_index,
                )
        expected_indices = list(range(len(self.layers)))
        if [layer.layer_index for layer in self.layers] != expected_indices:
            raise BusinessError(
                E_INVALID_MANIFEST,
                "layer_index values must be contiguous and start at zero",
                stage="model_package_validation",
                details={"layer_indices": [layer.layer_index for layer in self.layers]},
            )
        for previous, current in zip(self.layers, self.layers[1:]):
            if previous.output_dim != current.input_dim:
                raise BusinessError(
                    E_INVALID_MANIFEST,
                    "adjacent layer shapes are not chain-compatible",
                    stage="model_package_validation",
                    layer_id=current.layer_id,
                    layer_index=current.layer_index,
                    details={
                        "previous_output_dim": previous.output_dim,
                        "current_input_dim": current.input_dim,
                    },
                )


def _parse_layer(row: dict[str, Any], line_number: int) -> LayerSpec:
    missing = [field for field in REQUIRED_MANIFEST_FIELDS if field not in row]
    if missing:
        raise BusinessError(
            E_INVALID_MANIFEST,
            f"manifest line {line_number} is missing fields",
            stage="model_package_validation",
            details={"line": line_number, "missing_fields": missing},
        )
    shape_value = row["layer_shape"]
    if not isinstance(shape_value, (list, tuple)) or len(shape_value) != 2:
        raise BusinessError(
            E_INVALID_MANIFEST,
            "layer_shape must contain exactly two dimensions",
            stage="model_package_validation",
            layer_id=str(row["layer_id"]),
            details={"line": line_number, "layer_shape": shape_value},
        )
    try:
        shape = (int(shape_value[0]), int(shape_value[1]))
    except (TypeError, ValueError) as exc:
        raise BusinessError(
            E_INVALID_MANIFEST,
            "layer_shape values must be integers",
            stage="model_package_validation",
            layer_id=str(row["layer_id"]),
            details={"line": line_number},
        ) from exc
    if shape[0] <= 0 or shape[1] <= 0:
        raise BusinessError(
            E_INVALID_MANIFEST,
            "layer_shape dimensions must be positive",
            stage="model_package_validation",
            layer_id=str(row["layer_id"]),
            details={"line": line_number, "layer_shape": list(shape)},
        )
    try:
        layer = LayerSpec(
            model_tag=str(row["model_tag"]),
            layer_id=str(row["layer_id"]),
            layer_index=int(row["layer_index"]),
            layer_shape=shape,
            weight_revision=int(row["weight_revision"]),
            quant_dtype=str(row["quant_dtype"]),
            scale_hint=float(row["scale_hint"]),
            zero_point=int(row["zero_point"]),
            calib_seed=int(row["calib_seed"]),
        )
    except (TypeError, ValueError) as exc:
        raise BusinessError(
            E_INVALID_MANIFEST,
            "manifest field type is invalid",
            stage="model_package_validation",
            layer_id=str(row.get("layer_id")),
            details={"line": line_number},
        ) from exc
    return layer


def load_manifest(path: Path) -> ManifestView:
    rows: list[dict[str, Any]] = []
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
                        E_INVALID_MANIFEST,
                        f"manifest line {line_number} is not valid JSON",
                        stage="model_package_validation",
                        details={"line": line_number},
                    ) from exc
                if not isinstance(row, dict):
                    raise BusinessError(
                        E_INVALID_MANIFEST,
                        f"manifest line {line_number} must be an object",
                        stage="model_package_validation",
                        details={"line": line_number},
                    )
                rows.append(row)
    except FileNotFoundError as exc:
        raise BusinessError(
            E_INVALID_MANIFEST,
            f"manifest not found: {path}",
            stage="model_package_validation",
        ) from exc
    if not rows:
        raise BusinessError(
            E_INVALID_MANIFEST,
            "manifest is empty",
            stage="model_package_validation",
        )
    model_tags = {str(row["model_tag"]) for row in rows if "model_tag" in row}
    if len(model_tags) != 1:
        raise BusinessError(
            E_INVALID_MANIFEST,
            "manifest must contain exactly one model_tag",
            stage="model_package_validation",
            details={"model_tags": sorted(model_tags)},
        )
    layers = [_parse_layer(row, index + 1) for index, row in enumerate(rows)]
    return ManifestView.from_layers(next(iter(model_tags)), layers)


def write_manifest(path: Path, view: ManifestView) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for layer in view.layers:
            handle.write(
                json.dumps(
                    layer.as_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
