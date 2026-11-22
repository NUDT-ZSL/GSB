from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np

from .calibration import CalibrationStats, estimate_calibration
from .formats import (
    LayerQuant,
    ParamSet,
    parse_quant_params,
    serialize_quant_params,
)
from .model import LAYER_ORDER, NetworkWeights, generate_image_batch
from .snapshots import build_snapshot
from .utils import array_checksum, sha256_bytes, sha256_json


NORMAL_IMAGE_SEED = 20261123
CALIBRATION_IMAGE_SEED = 20261124
CORRUPT_V2 = "corrupt_v2_missing_field.csv"


@dataclass(frozen=True)
class FixturePaths:
    root: Path
    normal_images: Path
    calibration_images: Path
    v1: Path
    v2: Path
    v3: Path
    v3_dynamic: Path
    v2_drift: Path
    corrupt_v2: Path
    unknown_version: Path
    current_snapshot: Path
    rollback_snapshot: Path
    corrupt_snapshot: Path
    calibration_stats: Path
    manifest: Path


def fixture_paths(root: str | Path) -> FixturePaths:
    base = Path(root)
    return FixturePaths(
        root=base,
        normal_images=base / "images" / "normal_batch.npy",
        calibration_images=base / "images" / "calibration_batch.npy",
        v1=base / "params" / "network_v1.txt",
        v2=base / "params" / "network_v2.csv",
        v3=base / "params" / "network_v3.json",
        v3_dynamic=base / "params" / "network_v3_dynamic.json",
        v2_drift=base / "params" / "network_v2_drift.csv",
        corrupt_v2=base / "params" / CORRUPT_V2,
        unknown_version=base / "params" / "unknown_v9.txt",
        current_snapshot=base / "snapshots" / "current_v2.json",
        rollback_snapshot=base / "snapshots" / "rollback_v1.json",
        corrupt_snapshot=base / "snapshots" / "corrupt_checksum.json",
        calibration_stats=base / "calibration" / "stats.json",
        manifest=base / "manifest.json",
    )


def _base_params(weights: NetworkWeights) -> ParamSet:
    return ParamSet(
        "v1",
        tuple(
            LayerQuant(
                name=name,
                weight_scale=weights.base_scale(name),
                zero_point=0,
            )
            for name in LAYER_ORDER
        ),
    )


def _uniform_per_channel_params(
    weights: NetworkWeights, calibration: CalibrationStats
) -> ParamSet:
    layers = []
    for name in LAYER_ORDER:
        scale = weights.base_scale(name)
        channels = int(weights.weight(name).shape[0])
        layers.append(
            LayerQuant(
                name=name,
                weight_scale=scale,
                zero_point=0,
                per_channel=True,
                channel_scales=tuple(scale for _ in range(channels)),
                calibration_samples=calibration.sample_count_for(name),
                dynamic_range_mode="none",
            )
        )
    return ParamSet("v2", tuple(layers))


def _nonuniform_dynamic_v3(
    weights: NetworkWeights, calibration: CalibrationStats
) -> ParamSet:
    layers = []
    for name in LAYER_ORDER:
        scales = weights.channel_scales(name)
        layers.append(
            LayerQuant(
                name=name,
                weight_scale=weights.base_scale(name),
                zero_point=0,
                per_channel=True,
                channel_scales=scales,
                calibration_samples=calibration.sample_count_for(name),
                dynamic_range_mode=(
                    "per_activation" if name in {"conv1", "conv2"} else "none"
                ),
            )
        )
    return ParamSet("v3", tuple(layers))


def _drift_v2(weights: NetworkWeights, calibration: CalibrationStats) -> ParamSet:
    layers: list[LayerQuant] = []
    for name in LAYER_ORDER:
        scale = weights.base_scale(name)
        if name == "conv2":
            channels = int(weights.weight(name).shape[0])
            scales = tuple(scale * (1.0 + 0.35 * index) for index in range(channels))
            layers.append(
                LayerQuant(
                    name=name,
                    weight_scale=scale * 1.4,
                    zero_point=0,
                    per_channel=True,
                    channel_scales=scales,
                    calibration_samples=calibration.sample_count_for(name),
                    dynamic_range_mode="none",
                )
            )
        else:
            channels = int(weights.weight(name).shape[0])
            layers.append(
                LayerQuant(
                    name=name,
                    weight_scale=scale,
                    zero_point=0,
                    per_channel=True,
                    channel_scales=tuple(scale for _ in range(channels)),
                    calibration_samples=calibration.sample_count_for(name),
                    dynamic_range_mode="none",
                )
            )
    return ParamSet("v2", tuple(layers))


def generate_fixtures(root: str | Path, *, force: bool = False) -> dict[str, Any]:
    paths = fixture_paths(root)
    if paths.manifest.exists() and not force:
        try:
            existing = json.loads(paths.manifest.read_text(encoding="utf-8"))
            complete = all(
                (paths.root / relative).exists()
                for relative in existing.get("files", {})
            )
        except (OSError, ValueError):
            complete = False
        if complete:
            return {
                "status": "present",
                "root": str(paths.root),
                "manifest": str(paths.manifest),
            }
    for directory in (
        paths.root / "images",
        paths.root / "params",
        paths.root / "snapshots",
        paths.root / "calibration",
    ):
        directory.mkdir(parents=True, exist_ok=True)

    weights = NetworkWeights()
    images, labels = generate_image_batch(8, seed=NORMAL_IMAGE_SEED)
    calibration_images, calibration_labels = generate_image_batch(
        16, seed=CALIBRATION_IMAGE_SEED
    )
    np.save(paths.normal_images, images, allow_pickle=False)
    np.save(paths.calibration_images, calibration_images, allow_pickle=False)

    calibration = estimate_calibration(weights, calibration_images)
    v1_params = _base_params(weights)
    v1_text = serialize_quant_params(v1_params, version="v1")
    parsed_v1 = parse_quant_params(v1_text).params
    v2_params = _uniform_per_channel_params(weights, calibration)
    v2_text = serialize_quant_params(v2_params, version="v2")
    v3_params = replace(v2_params, format_version="v3")
    v3_text = serialize_quant_params(v3_params, calibration=calibration)
    dynamic_params = _nonuniform_dynamic_v3(weights, calibration)
    dynamic_text = serialize_quant_params(dynamic_params, calibration=calibration)
    drift_text = serialize_quant_params(
        _drift_v2(weights, calibration), version="v2"
    )

    paths.v1.write_text(v1_text, encoding="utf-8")
    paths.v2.write_text(v2_text, encoding="utf-8")
    paths.v3.write_text(v3_text, encoding="utf-8")
    paths.v3_dynamic.write_text(dynamic_text, encoding="utf-8")
    paths.v2_drift.write_text(drift_text, encoding="utf-8")
    paths.unknown_version.write_text(
        "version=v9\nlayer,scale,zero_point\nconv1,0.1,0\n", encoding="utf-8"
    )

    corrupt_lines = v2_text.splitlines()
    for index, line in enumerate(corrupt_lines):
        if line.startswith("conv2,"):
            corrupt_lines[index] = ",".join(line.split(",")[:-1])
            break
    paths.corrupt_v2.write_text("\n".join(corrupt_lines) + "\n", encoding="utf-8")
    paths.calibration_stats.write_text(
        json.dumps(
            calibration.to_dict(), indent=2, sort_keys=True, ensure_ascii=False
        )
        + "\n",
        encoding="utf-8",
    )

    # The two format-specific snapshots are fully materialized so rollback and
    # corruption tests do not depend on in-memory state.
    model_fingerprint = weights.fingerprint()
    current = build_snapshot(
        v2_params,
        calibration,
        version=2,
        model_fingerprint=model_fingerprint,
    )
    rollback = build_snapshot(
        parsed_v1,
        calibration,
        version=1,
        model_fingerprint=model_fingerprint,
    )
    paths.current_snapshot.write_text(current.to_json(), encoding="utf-8")
    paths.rollback_snapshot.write_text(rollback.to_json(), encoding="utf-8")
    corrupt_payload = current.to_dict()
    corrupt_payload["checksum"] = "snap-sha256:" + ("0" * 64)
    paths.corrupt_snapshot.write_text(
        json.dumps(
            corrupt_payload, indent=2, sort_keys=True, ensure_ascii=False
        )
        + "\n",
        encoding="utf-8",
    )

    files = [
        paths.normal_images,
        paths.calibration_images,
        paths.v1,
        paths.v2,
        paths.v3,
        paths.v3_dynamic,
        paths.v2_drift,
        paths.corrupt_v2,
        paths.unknown_version,
        paths.current_snapshot,
        paths.rollback_snapshot,
        paths.corrupt_snapshot,
        paths.calibration_stats,
    ]
    manifest = {
        "generator": "quantdiag.fixtures",
        "normal_image_seed": NORMAL_IMAGE_SEED,
        "calibration_image_seed": CALIBRATION_IMAGE_SEED,
        "network_seed": weights.seed,
        "model_fingerprint": model_fingerprint,
        "normal_labels": labels.tolist(),
        "calibration_labels": calibration_labels.tolist(),
        "normal_batch_checksum": array_checksum(images),
        "calibration_batch_checksum": array_checksum(calibration_images),
        "calibration_version": calibration.version,
        "files": {
            str(path.relative_to(paths.root)): {
                "sha256": sha256_bytes(path.read_bytes()),
                "size": path.stat().st_size,
            }
            for path in files
        },
        "expected_contract": {
            "unknown_version": "E_VERSION_UNSUPPORTED",
            "corrupt_v2": "E_FORMAT_CORRUPT",
            "rollback_snapshot": "E_SNAPSHOT_ROLLBACK",
            "corrupt_snapshot": "E_SNAPSHOT_CORRUPT",
        },
    }
    paths.manifest.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    return {
        "status": "generated",
        "root": str(paths.root),
        "manifest": str(paths.manifest),
        "manifest_checksum": sha256_json(manifest),
        "files": sorted(manifest["files"]),
    }
