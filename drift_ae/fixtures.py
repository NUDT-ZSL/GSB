"""Build the deterministic local fixture bundle used by examples and tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from .autoencoder import AEConfig, DeterministicAutoencoder
from .data import (
    DEFAULT_SENSORS,
    DEFAULT_TIME_STEPS,
    FIXTURE_SEED,
    default_corrections,
    generate_windows,
    historical_drift_events,
)
from .features import extract_features
from .io_utils import atomic_write_json, payload_checksum
from .preprocessing import NormalizationStats, PreprocessConfig, WindowPreprocessor
from .threshold_formats import upgrade_v1_to_v2, write_v1, write_v2
from .thresholds import (
    GLOBAL_SEGMENT,
    ThresholdModel,
    ThresholdRow,
    build_rows_from_samples,
    empirical_quantiles,
)


def build_fixture_bundle(
    output_dir: str | Path,
    *,
    seed: int = FIXTURE_SEED,
    n_windows: int = 96,
    baseline_count: int = 48,
    train_epochs: int = 500,
) -> dict[str, Any]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    windows = generate_windows(
        seed=seed,
        n_windows=n_windows,
        time_steps=DEFAULT_TIME_STEPS,
        n_sensors=DEFAULT_SENSORS,
    )
    baseline_windows = [window for window in windows[:baseline_count] if window.true_label == 0]
    stats = NormalizationStats.from_windows([window.values for window in baseline_windows])
    np.savez_compressed(out / "normalization_stats.npz", mean=stats.mean, scale=stats.scale)

    preprocessor = WindowPreprocessor(
        PreprocessConfig(seed=seed),
        stats=stats,
        input_shape=(DEFAULT_TIME_STEPS, DEFAULT_SENSORS),
    )
    baseline_inputs = np.stack(
        [preprocessor.process(window.window_id, window.values).completed for window in baseline_windows]
    )
    ae_config = AEConfig(seed=seed, epochs=train_epochs)
    ae = DeterministicAutoencoder(DEFAULT_TIME_STEPS * DEFAULT_SENSORS, ae_config)
    losses = ae.fit(baseline_inputs, epochs=train_epochs)
    ae.save_npz(out / "baseline_ae.npz")

    feature_index: dict[str, dict[str, Any]] = {}
    serialized_windows = []
    for window in windows:
        preprocessed = preprocessor.process(window.window_id, window.values)
        forward = ae.forward(preprocessed.completed)
        features = extract_features(preprocessed, forward.reconstruction)
        feature_index[window.window_id] = {
            "segment_id": window.segment_id,
            "window_index": window.window_index,
            "score": features.score,
        }
        item = window.to_dict()
        item["baseline_reconstruction_error"] = features.score
        item["baseline_features"] = features.scalar_values()
        serialized_windows.append(item)

    calibration_samples: dict[str, list[float]] = {GLOBAL_SEGMENT: []}
    for window in baseline_windows:
        score = feature_index[window.window_id]["score"]
        calibration_samples[GLOBAL_SEGMENT].append(score)
        calibration_samples.setdefault(window.segment_id, []).append(score)

    rows = build_rows_from_samples(calibration_samples, model_version=1)
    model = ThresholdModel(
        model_version=1,
        rows=rows,
        calibration_path=[f"fixture:seed={seed}", "baseline_true_normal_empirical_quantiles"],
    )
    model.recompute_checksum()

    v1_path = out / "threshold_model_v1.json"
    write_v1(v1_path, model)
    v2_dir = out / "threshold_model_v2"
    upgraded = upgrade_v1_to_v2(v1_path, v2_dir, known_segments=[f"stream-{i}" for i in range(3)])

    # Add a v2-only hierarchical/layer condition so downgrade discard rules are
    # explicit and testable.
    stream_one_scores = calibration_samples.get("stream-1", calibration_samples[GLOBAL_SEGMENT])
    layered_row = ThresholdRow(
        segment_id="stream-1",
        tier="sensor_layer",
        metric="score",
        granularity="sensor_block",
        quantiles=empirical_quantiles(stream_one_scores),
        sample_count=len(stream_one_scores),
        condition={"sensor_block": "sensors-2-3", "daypart": "night"},
    )
    layered_row.recompute_checksum()
    upgraded.rows.append(layered_row)
    upgraded.recompute_checksum()
    write_v2(v2_dir, upgraded)

    drift_events = historical_drift_events(seed)
    corrections = default_corrections()
    atomic_write_json(out / "sample_windows.json", serialized_windows)
    atomic_write_json(out / "drift_events.json", drift_events)
    atomic_write_json(out / "pseudo_label_corrections.json", corrections)
    manifest = {
        "seed": seed,
        "window_count": len(windows),
        "input_shape": [DEFAULT_TIME_STEPS, DEFAULT_SENSORS],
        "input_dim": DEFAULT_TIME_STEPS * DEFAULT_SENSORS,
        "baseline_count": baseline_count,
        "baseline_normal_count": len(baseline_windows),
        "ae_checksum": ae.checksum,
        "ae_final_loss": float(losses[-1]),
        "threshold_model_checksum": upgraded.checksum,
        "files": {
            "windows": "sample_windows.json",
            "v1": "threshold_model_v1.json",
            "v2_dir": "threshold_model_v2",
            "drift_events": "drift_events.json",
            "corrections": "pseudo_label_corrections.json",
            "ae": "baseline_ae.npz",
            "stats": "normalization_stats.npz",
        },
    }
    manifest["checksum"] = payload_checksum({k: v for k, v in manifest.items() if k != "checksum"})
    atomic_write_json(out / "manifest.json", manifest)
    return {
        "output_dir": str(out),
        "manifest": manifest,
        "windows": serialized_windows,
        "model": upgraded,
        "feature_index": feature_index,
    }


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Generate deterministic drift-AE fixtures")
    parser.add_argument("--output-dir", default="artifacts/fixtures")
    parser.add_argument("--seed", type=int, default=FIXTURE_SEED)
    parser.add_argument("--n-windows", type=int, default=96)
    args = parser.parse_args()
    result = build_fixture_bundle(args.output_dir, seed=args.seed, n_windows=args.n_windows)
    print(
        {
            "ok": True,
            "output_dir": result["output_dir"],
            "window_count": result["manifest"]["window_count"],
            "ae_checksum": result["manifest"]["ae_checksum"],
            "threshold_checksum": result["manifest"]["threshold_model_checksum"],
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
