from __future__ import annotations

import io
import json
import shutil
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .errors import (
    DeployError,
    E_PACKAGE_CALIBRATION_MISSING,
    E_PACKAGE_MANIFEST_TRUNCATED,
    E_PACKAGE_SCHEMA,
    E_SNAPSHOT_ROLLBACK,
)
from .util import canonical_json, digest_bytes, digest_json, stable_int


REQUIRED_FIELDS = (
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
class ModelLayer:
    model_tag: str
    layer_id: str
    layer_index: int
    layer_shape: tuple[int, int]
    weight_revision: int
    quant_dtype: str
    scale_hint: str
    zero_point: int
    calib_seed: int

    @property
    def out_features(self) -> int:
        return self.layer_shape[0]

    @property
    def in_features(self) -> int:
        return self.layer_shape[1]

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["layer_shape"] = list(self.layer_shape)
        return result

    def cache_identity(self) -> dict[str, Any]:
        return self.to_dict()


@dataclass
class LoadedBundle:
    root: Path
    layers: list[ModelLayer]
    meta: dict[str, Any]
    manifest_digest: str
    manifest_path: Path

    @property
    def layer_ids(self) -> list[str]:
        return [layer.layer_id for layer in self.layers]

    @property
    def model_tag(self) -> str:
        return self.layers[0].model_tag

    @property
    def layer_by_id(self) -> dict[str, ModelLayer]:
        return {layer.layer_id: layer for layer in self.layers}

    @property
    def calib_samples(self) -> int:
        return int(self.meta.get("calibration_samples", 8))

    @property
    def calibration_revisions(self) -> dict[str, int]:
        raw = self.meta.get("calibration_revisions") or {}
        return {
            layer.layer_id: int(raw.get(layer.layer_id, 0)) for layer in self.layers
        }

    def weight_path(self, layer: ModelLayer) -> Path:
        return self.root / "weights" / f"{layer.layer_id}.npy"

    def calibration_path(self, layer: ModelLayer) -> Path:
        return self.root / "calibration" / f"{layer.layer_id}.npy"

    def initial_input_path(self) -> Path:
        return self.root / "initial_input.npy"

    def weight_bytes(self, layer: ModelLayer) -> bytes:
        path = self.weight_path(layer)
        if not path.exists():
            raise DeployError(
                "E_PACKAGE_WEIGHT_MISSING",
                "model_validation",
                f"weight file does not exist: {path}",
                layer_id=layer.layer_id,
                field="weights",
            )
        return path.read_bytes()

    def load_weight(
        self,
        layer: ModelLayer,
        *,
        truncate: bool = False,
    ) -> tuple[np.ndarray, str]:
        raw = self.weight_bytes(layer)
        if truncate:
            raw = raw[: max(1, len(raw) // 2)]
        try:
            array = np.load(io.BytesIO(raw), allow_pickle=False)
        except Exception as exc:
            raise DeployError(
                "E_PACKAGE_WEIGHT_TRUNCATED",
                "model_validation",
                f"weight array cannot be decoded: {exc}",
                layer_id=layer.layer_id,
                field="weights",
            ) from exc
        if tuple(array.shape) != layer.layer_shape:
            raise DeployError(
                "E_PACKAGE_WEIGHT_SHAPE",
                "model_validation",
                f"weight shape {tuple(array.shape)} != manifest {layer.layer_shape}",
                layer_id=layer.layer_id,
                field="layer_shape",
            )
        array = np.ascontiguousarray(array, dtype="<f8")
        return array, digest_bytes(raw)

    def load_calibration(self, layer: ModelLayer) -> tuple[np.ndarray, str]:
        path = self.calibration_path(layer)
        if not path.exists():
            raise DeployError(
                E_PACKAGE_CALIBRATION_MISSING,
                "calibration",
                f"calibration file does not exist: {path}",
                layer_id=layer.layer_id,
                field="calibration",
            )
        raw = path.read_bytes()
        try:
            array = np.load(io.BytesIO(raw), allow_pickle=False)
        except Exception as exc:
            raise DeployError(
                "E_CALIBRATION_TRUNCATED",
                "calibration",
                f"calibration array cannot be decoded: {exc}",
                layer_id=layer.layer_id,
                field="calibration",
            ) from exc
        return np.ascontiguousarray(array, dtype="<f8"), digest_bytes(raw)

    def load_initial_input(self) -> np.ndarray:
        path = self.initial_input_path()
        try:
            array = np.load(path, allow_pickle=False)
        except Exception as exc:
            raise DeployError(
                "E_PACKAGE_INITIAL_INPUT",
                "model_validation",
                f"initial input cannot be decoded: {exc}",
                field="initial_input",
            ) from exc
        return np.ascontiguousarray(array, dtype="<f8")


def _require_type(
    record: dict[str, Any],
    name: str,
    expected: type | tuple[type, ...],
    *,
    line_no: int,
    layer_id: str | None = None,
) -> Any:
    if name not in record:
        raise DeployError(
            E_PACKAGE_SCHEMA,
            "model_validation",
            f"manifest line {line_no} missing field {name}",
            layer_id=layer_id,
            field=name,
        )
    value = record[name]
    if isinstance(value, bool) and expected is int:
        bad = True
    else:
        bad = not isinstance(value, expected)
    if bad:
        raise DeployError(
            E_PACKAGE_SCHEMA,
            "model_validation",
            f"manifest field {name} has invalid type {type(value).__name__}",
            layer_id=layer_id,
            field=name,
        )
    return value


def parse_manifest_line(record: dict[str, Any], line_no: int) -> ModelLayer:
    layer_id = record.get("layer_id")
    if not isinstance(layer_id, str) or not layer_id:
        raise DeployError(
            E_PACKAGE_SCHEMA,
            "model_validation",
            f"manifest line {line_no} has invalid layer_id",
            field="layer_id",
        )
    model_tag = _require_type(record, "model_tag", str, line_no=line_no, layer_id=layer_id)
    layer_index = _require_type(
        record, "layer_index", int, line_no=line_no, layer_id=layer_id
    )
    shape = _require_type(
        record, "layer_shape", list, line_no=line_no, layer_id=layer_id
    )
    if len(shape) != 2 or any(
        not isinstance(v, int) or isinstance(v, bool) or v <= 0 for v in shape
    ):
        raise DeployError(
            E_PACKAGE_SCHEMA,
            "model_validation",
            f"layer_shape must be two positive integers, got {shape!r}",
            layer_id=layer_id,
            field="layer_shape",
        )
    revision = _require_type(
        record, "weight_revision", int, line_no=line_no, layer_id=layer_id
    )
    dtype = _require_type(
        record, "quant_dtype", str, line_no=line_no, layer_id=layer_id
    )
    scale_hint = _require_type(
        record, "scale_hint", str, line_no=line_no, layer_id=layer_id
    )
    zero_point = _require_type(
        record, "zero_point", int, line_no=line_no, layer_id=layer_id
    )
    calib_seed = _require_type(
        record, "calib_seed", int, line_no=line_no, layer_id=layer_id
    )
    if layer_index < 0 or revision < 1:
        raise DeployError(
            E_PACKAGE_SCHEMA,
            "model_validation",
            "layer_index must be >= 0 and weight_revision must be >= 1",
            layer_id=layer_id,
            field="weight_revision" if revision < 1 else "layer_index",
        )
    return ModelLayer(
        model_tag=model_tag,
        layer_id=layer_id,
        layer_index=layer_index,
        layer_shape=(shape[0], shape[1]),
        weight_revision=revision,
        quant_dtype=dtype,
        scale_hint=scale_hint,
        zero_point=zero_point,
        calib_seed=calib_seed,
    )


def load_bundle(root: str | Path) -> LoadedBundle:
    bundle_root = Path(root)
    manifest_path = bundle_root / "manifest.jsonl"
    if not manifest_path.exists():
        raise DeployError(
            E_PACKAGE_MANIFEST_TRUNCATED,
            "model_validation",
            f"manifest does not exist: {manifest_path}",
            field="manifest",
        )
    raw = manifest_path.read_bytes()
    digest = digest_bytes(raw)
    layers: list[ModelLayer] = []
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DeployError(
            E_PACKAGE_MANIFEST_TRUNCATED,
            "model_validation",
            f"manifest is not valid UTF-8: {exc}",
            field="manifest",
        ) from exc
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DeployError(
                E_PACKAGE_MANIFEST_TRUNCATED,
                "model_validation",
                f"manifest line {line_no} is truncated or invalid JSON: {exc.msg}",
                field="manifest",
                details={"line": line_no, "column": exc.colno},
            ) from exc
        if not isinstance(record, dict):
            raise DeployError(
                E_PACKAGE_SCHEMA,
                "model_validation",
                f"manifest line {line_no} must be a JSON object",
                field="manifest",
            )
        layers.append(parse_manifest_line(record, line_no))
    if not layers:
        raise DeployError(
            E_PACKAGE_MANIFEST_TRUNCATED,
            "model_validation",
            "manifest contains no layers",
            field="manifest",
        )
    layers.sort(key=lambda layer: layer.layer_index)
    seen_ids: set[str] = set()
    for expected_index, layer in enumerate(layers):
        if layer.layer_id in seen_ids:
            raise DeployError(
                E_PACKAGE_SCHEMA,
                "model_validation",
                f"duplicate layer_id {layer.layer_id}",
                layer_id=layer.layer_id,
                field="layer_id",
            )
        seen_ids.add(layer.layer_id)
        if layer.layer_index != expected_index:
            raise DeployError(
                E_PACKAGE_SCHEMA,
                "model_validation",
                f"layer_index must be contiguous from 0; got {layer.layer_index} "
                f"at sorted position {expected_index}",
                layer_id=layer.layer_id,
                field="layer_index",
            )
    tags = {layer.model_tag for layer in layers}
    if len(tags) != 1:
        raise DeployError(
            E_PACKAGE_SCHEMA,
            "model_validation",
            f"manifest contains multiple model_tag values: {sorted(tags)}",
            field="model_tag",
        )
    for previous, current in zip(layers, layers[1:]):
        if previous.out_features != current.in_features:
            raise DeployError(
                E_PACKAGE_SCHEMA,
                "model_validation",
                f"shape chain break: {previous.layer_id} output "
                f"{previous.out_features} != {current.layer_id} input "
                f"{current.in_features}",
                layer_id=current.layer_id,
                field="layer_shape",
            )
    meta_path = bundle_root / "bundle_meta.json"
    meta: dict[str, Any] = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise DeployError(
                E_PACKAGE_MANIFEST_TRUNCATED,
                "model_validation",
                f"bundle_meta.json is invalid: {exc.msg}",
                field="bundle_meta",
            ) from exc
    return LoadedBundle(
        root=bundle_root,
        layers=layers,
        meta=meta,
        manifest_digest=digest,
        manifest_path=manifest_path,
    )


def _weight_seed(layer: ModelLayer) -> int:
    return stable_int(
        layer.model_tag,
        layer.layer_id,
        layer.layer_index,
        list(layer.layer_shape),
        layer.weight_revision,
        "weight-v1",
    )


def _bias_seed(layer: ModelLayer) -> int:
    return stable_int(
        layer.model_tag,
        layer.layer_id,
        layer.layer_index,
        list(layer.layer_shape),
        layer.weight_revision,
        "bias-v1",
    )


def generate_weight(layer: ModelLayer) -> np.ndarray:
    rng = np.random.default_rng(_weight_seed(layer))
    weight = rng.standard_normal(layer.layer_shape, dtype=np.float64)
    weight *= 0.35
    return np.ascontiguousarray(weight)


def generate_bias(layer: ModelLayer) -> np.ndarray:
    rng = np.random.default_rng(_bias_seed(layer))
    return np.ascontiguousarray(rng.standard_normal(layer.out_features) * 0.03)


def generate_calibration(
    layer: ModelLayer,
    *,
    seed: int | None = None,
    samples: int = 8,
) -> np.ndarray:
    effective_seed = layer.calib_seed if seed is None else seed
    rng = np.random.default_rng(
        stable_int(
            layer.model_tag,
            layer.layer_id,
            effective_seed,
            layer.in_features,
            samples,
            "calibration-v1",
        )
    )
    return np.ascontiguousarray(rng.standard_normal((samples, layer.in_features)) * 0.5)


def generate_initial_input(model_tag: str, dim: int) -> np.ndarray:
    rng = np.random.default_rng(stable_int(model_tag, dim, "initial-input-v1"))
    return np.ascontiguousarray(rng.standard_normal(dim) * 0.2)


DEFAULT_LAYER_SPECS: tuple[dict[str, Any], ...] = (
    {
        "layer_id": "L00",
        "layer_index": 0,
        "layer_shape": [64, 64],
        "weight_revision": 1,
        "quant_dtype": "int8",
        "scale_hint": "per_tensor:group=64",
        "zero_point": 0,
        "calib_seed": 1101,
    },
    {
        "layer_id": "L01",
        "layer_index": 1,
        "layer_shape": [64, 64],
        "weight_revision": 1,
        "quant_dtype": "int8",
        "scale_hint": "per_channel:group=16",
        "zero_point": 0,
        "calib_seed": 1102,
    },
    {
        "layer_id": "L02",
        "layer_index": 2,
        "layer_shape": [48, 64],
        "weight_revision": 1,
        "quant_dtype": "int4",
        "scale_hint": "per_channel:group=8",
        "zero_point": 0,
        "calib_seed": 1103,
    },
    {
        "layer_id": "L03",
        "layer_index": 3,
        "layer_shape": [48, 48],
        "weight_revision": 1,
        "quant_dtype": "int4",
        "scale_hint": "per_tensor",
        "zero_point": 0,
        "calib_seed": 1104,
    },
    {
        "layer_id": "L04",
        "layer_index": 4,
        "layer_shape": [32, 48],
        "weight_revision": 1,
        "quant_dtype": "int8",
        "scale_hint": "per_channel",
        "zero_point": 0,
        "calib_seed": 1105,
    },
)


def _layer_records(
    model_tag: str,
    specs: Iterable[dict[str, Any]],
) -> list[ModelLayer]:
    records: list[ModelLayer] = []
    for index, spec in enumerate(specs):
        data = {
            "model_tag": model_tag,
            "layer_id": spec.get("layer_id", f"L{index:02d}"),
            "layer_index": spec.get("layer_index", index),
            "layer_shape": list(spec["layer_shape"]),
            "weight_revision": spec.get("weight_revision", 1),
            "quant_dtype": spec.get("quant_dtype", "int8"),
            "scale_hint": spec.get("scale_hint", "per_tensor"),
            "zero_point": spec.get("zero_point", 0),
            "calib_seed": spec.get("calib_seed", 1000 + index),
        }
        records.append(parse_manifest_line(data, index + 1))
    return records


def _write_npy(path: Path, array: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.save(path, np.ascontiguousarray(array, dtype="<f8"), allow_pickle=False)


def write_bundle(
    out_dir: str | Path,
    *,
    model_tag: str = "llm-local-demo-v1",
    specs: Iterable[dict[str, Any]] | None = None,
    calib_samples: int = 8,
    force: bool = False,
) -> LoadedBundle:
    root = Path(out_dir)
    if root.exists() and any(root.iterdir()):
        if not force:
            raise FileExistsError(f"bundle directory is not empty: {root}")
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    layers = _layer_records(model_tag, specs or DEFAULT_LAYER_SPECS)
    manifest_lines = [
        canonical_json(
            {
                **layer.to_dict(),
                "layer_shape": list(layer.layer_shape),
            }
        )
        for layer in layers
    ]
    (root / "manifest.jsonl").write_text(
        "\n".join(manifest_lines) + "\n", encoding="utf-8"
    )
    calibration_revisions: dict[str, int] = {}
    for layer in layers:
        _write_npy(root / "weights" / f"{layer.layer_id}.npy", generate_weight(layer))
        _write_npy(
            root / "calibration" / f"{layer.layer_id}.npy",
            generate_calibration(layer, samples=calib_samples),
        )
        calibration_revisions[layer.layer_id] = 0
    async_meta = {
        "schema": 1,
        "calibration_samples": calib_samples,
        "calibration_revisions": calibration_revisions,
        "generator": "numpy-default-rng-v1",
    }
    (root / "bundle_meta.json").write_text(
        json.dumps(async_meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _write_npy(
        root / "initial_input.npy",
        generate_initial_input(model_tag, layers[0].in_features),
    )
    _write_corrupt_fixtures(root, layers)
    return load_bundle(root)


def _write_corrupt_fixtures(root: Path, layers: list[ModelLayer]) -> None:
    corrupt = root / "corrupt"
    corrupt.mkdir(parents=True, exist_ok=True)
    manifest_lines = (root / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
    truncated = "\n".join(manifest_lines[:-1]) + "\n" + manifest_lines[-1][:31]
    (corrupt / "manifest_truncated.jsonl").write_text(truncated, encoding="utf-8")
    unknown = json.loads(manifest_lines[1])
    unknown["quant_dtype"] = "int3"
    (corrupt / "manifest_unknown_dtype.jsonl").write_text(
        "\n".join([manifest_lines[0], canonical_json(unknown), *manifest_lines[2:]])
        + "\n",
        encoding="utf-8",
    )
    bad_shape = json.loads(manifest_lines[2])
    bad_shape["layer_shape"] = [47, 64]
    (corrupt / "manifest_shape_mismatch.jsonl").write_text(
        "\n".join(
            [manifest_lines[0], manifest_lines[1], canonical_json(bad_shape), *manifest_lines[3:]]
        )
        + "\n",
        encoding="utf-8",
    )
    (corrupt / "README.txt").write_text(
        "这些 JSONL 仅用于异常演示：截断最后一行、未知 quant_dtype、形状链断裂。\n",
        encoding="utf-8",
    )


def write_revision_bundle(
    base_bundle: str | Path,
    out_dir: str | Path,
    *,
    layer_id: str,
    weight_revision: int,
    force: bool = True,
) -> LoadedBundle:
    base = load_bundle(base_bundle)
    if layer_id not in base.layer_by_id:
        raise KeyError(f"unknown layer_id: {layer_id}")
    target = base.layer_by_id[layer_id]
    if weight_revision <= target.weight_revision:
        raise DeployError(
            E_SNAPSHOT_ROLLBACK,
            "model_validation",
            f"weight_revision must increase above {target.weight_revision}; "
            f"got {weight_revision}",
            layer_id=layer_id,
            field="weight_revision",
            details={
                "current_revision": target.weight_revision,
                "requested_revision": weight_revision,
            },
        )
    root = Path(out_dir)
    if root.exists():
        if not force:
            raise FileExistsError(f"revision bundle directory exists: {root}")
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    manifest_lines: list[str] = []
    for layer in base.layers:
        updated = replace(layer, weight_revision=weight_revision) if layer.layer_id == layer_id else layer
        manifest_lines.append(
            canonical_json({**updated.to_dict(), "layer_shape": list(updated.layer_shape)})
        )
        _write_npy(root / "weights" / f"{updated.layer_id}.npy", generate_weight(updated))
        source_calib = base.calibration_path(layer)
        dest_calib = root / "calibration" / f"{layer.layer_id}.npy"
        dest_calib.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_calib, dest_calib)
    (root / "manifest.jsonl").write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")
    meta = dict(base.meta)
    meta["source_bundle"] = str(base.root)
    meta["revision_source_layer"] = layer_id
    (root / "bundle_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    shutil.copyfile(base.initial_input_path(), root / "initial_input.npy")
    return load_bundle(root)


def bundle_summary(bundle: LoadedBundle) -> dict[str, Any]:
    return {
        "root": str(bundle.root),
        "model_tag": bundle.model_tag,
        "manifest_digest": bundle.manifest_digest,
        "layers": [
            {
                **layer.to_dict(),
                "layer_shape": list(layer.layer_shape),
            }
            for layer in bundle.layers
        ],
        "calibration_revisions": bundle.calibration_revisions,
        "calibration_samples": bundle.calib_samples,
    }

