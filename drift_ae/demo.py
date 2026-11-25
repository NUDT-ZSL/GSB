"""End-to-end example runner with crash injection and replay summary."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import numpy as np

from .autoencoder import AEConfig, DeterministicAutoencoder
from .data import FIXTURE_SEED
from .drift import build_baseline, evaluate_drift
from .execution_control import BudgetConfig
from .io_utils import atomic_write_json, read_json
from .pipeline import DetectionPipeline
from .preprocessing import NormalizationStats
from .threshold_formats import load_threshold_model


def prepare_pipeline(
    state_dir: str | Path,
    fixtures_dir: str | Path,
    *,
    fresh: bool = False,
    batch_size: int = 8,
    strategy: str = "strict",
    threshold_view: str = "v2",
    budget_config: BudgetConfig | None = None,
) -> DetectionPipeline:
    state = Path(state_dir)
    fixtures = Path(fixtures_dir)
    if fresh and state.exists():
        shutil.rmtree(state)

    pipeline = DetectionPipeline(
        state,
        seed=FIXTURE_SEED,
        batch_size=batch_size,
        strategy=strategy,
        budget_config=budget_config,
    )

    if pipeline.stats is None:
        with np.load(fixtures / "normalization_stats.npz") as data:
            pipeline.set_stats(NormalizationStats(mean=data["mean"], scale=data["scale"]))
    if pipeline.ae is None:
        pipeline.set_ae(
            DeterministicAutoencoder.load_npz(
                fixtures / "baseline_ae.npz",
                AEConfig(seed=FIXTURE_SEED),
            )
        )
    if pipeline.model is None:
        model_path = fixtures / ("threshold_model_v1.json" if threshold_view == "v1" else "threshold_model_v2")
        pipeline.set_threshold_model(load_threshold_model(model_path))
    return pipeline


def run_demo(
    *,
    state_dir: str | Path,
    fixtures_dir: str | Path = "artifacts/fixtures",
    fresh: bool = False,
    crash_after_batches: int | None = None,
    calibrate: bool = True,
    batch_size: int = 8,
    strategy: str = "strict",
    threshold_view: str = "v2",
    budget_config: BudgetConfig | None = None,
) -> dict[str, Any]:
    fixtures = Path(fixtures_dir)
    windows = read_json(fixtures / "sample_windows.json")
    corrections = read_json(fixtures / "pseudo_label_corrections.json")
    historical_drift = read_json(fixtures / "drift_events.json")

    pipeline = prepare_pipeline(
        state_dir,
        fixtures,
        fresh=fresh,
        batch_size=batch_size,
        strategy=strategy,
        threshold_view=threshold_view,
        budget_config=budget_config,
    )
    run_summary = pipeline.run_pending(windows, crash_after_batches=crash_after_batches)

    summary: dict[str, Any] = {
        "ok": True,
        "state_dir": str(state_dir),
        "run": {
            "processed_count": len(run_summary["processed"]),
            "reused_count": len(run_summary["reused"]),
            "reconstructed_count": len(run_summary["reconstructed_this_run"]),
            "reused_committed_count": len(run_summary["reused_committed_this_run"]),
            "processed_preview": run_summary["processed"][:3],
            "reused_preview": run_summary["reused"][:3],
            "processed_total": run_summary["processed_total"],
            "processed": run_summary["processed"],
            "reused": run_summary["reused"],
            "reconstructed_this_run": run_summary["reconstructed_this_run"],
            "reused_committed_this_run": run_summary["reused_committed_this_run"],
        },
    }

    calibration_summaries = []
    if calibrate:
        feature_index = pipeline.result["feature_index"]
        window_by_id = {window["window_id"]: window for window in windows}
        normal_baseline_ids = {
            window_id
            for window_id, window in window_by_id.items()
            if int(window.get("window_index", 10**9)) < 48 and int(window.get("true_label", 0)) == 0
        }
        baseline_items = [
            item
            for window_id, item in feature_index.items()
            if window_id in normal_baseline_ids
        ]
        recent_items = [
            item
            for item in feature_index.values()
            if 64 <= int(item.get("window_index", -1)) <= 79
        ]
        drift_result = evaluate_drift(
            recent_items,
            build_baseline(baseline_items),
            event_id="DRIFT-RUNTIME-CURRENT",
        )
        summary["drift"] = {"runtime": drift_result, "historical_event": historical_drift[-1]}

        completed_reasons = {
            item.get("reason") for item in pipeline.result.get("calibration_history", [])
        }
        false_positive = next(item for item in corrections if item["window_id"] == "w0003")
        planned_steps = [
            (
                "single_false_positive_fix",
                {
                    "reason": "single_false_positive_fix",
                    "window_updates": [false_positive],
                    "batch_id": len(pipeline.result.get("committed_batches", [])),
                },
            ),
            (
                "drift_alert_local_recalibration",
                {
                    "reason": "drift_alert_local_recalibration",
                    "drift_alert": historical_drift[-1],
                    "requested_segments": historical_drift[-1]["affected_segments"],
                    "batch_id": len(pipeline.result.get("committed_batches", [])),
                },
            ),
        ]
        for reason, request in planned_steps:
            if reason in completed_reasons:
                continue
            calibration = pipeline.apply_calibration(request)
            if not calibration.get("ok"):
                summary["calibration"] = calibration
                summary["calibrations"] = calibration_summaries
                summary["ok"] = False
                break
            item_summary = {
                "ok": True,
                "reason": reason,
                "new_model_version": pipeline.model.model_version,
                "budget": calibration["budget"],
                "propagation": calibration["propagation"],
                "readjudicated_window_ids": calibration["readjudicated_window_ids"],
                "reused_decision_window_ids": calibration["reused_decision_window_ids"],
            }
            calibration_summaries.append(item_summary)
        else:
            summary["calibrations"] = calibration_summaries
            if calibration_summaries:
                summary["calibration"] = calibration_summaries[-1]

    replay = pipeline.replay_diagnostics()
    summary["replay"] = {
        "ok": replay["ok"],
        "record_count": len(replay["results"]),
        "mismatch_count": sum(1 for item in replay["results"] if not item["matches"]),
    }
    summary["snapshot_versions"] = {
        kind: (pipeline.snapshots.load(kind) or {}).get("version")
        for kind in ("plan", "threshold", "result")
    }
    atomic_write_json(Path(state_dir) / "demo_summary.json", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run drift-AE end-to-end example")
    parser.add_argument("--fixtures-dir", default="artifacts/fixtures")
    parser.add_argument("--state-dir", default="artifacts/runtime-demo")
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--crash-after", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--strategy", choices=["strict", "tolerance_band"], default="strict")
    parser.add_argument("--threshold-view", choices=["v1", "v2"], default="v2")
    parser.add_argument("--skip-calibration", action="store_true")
    parser.add_argument("--budget-max-comparisons", type=int, default=None)
    parser.add_argument("--budget-max-memory-bytes", type=int, default=None)
    args = parser.parse_args(argv)

    budget = None
    if args.budget_max_comparisons is not None or args.budget_max_memory_bytes is not None:
        budget = BudgetConfig(
            max_reconstruction_comparisons=args.budget_max_comparisons or 200_000,
            max_resident_feature_bytes=args.budget_max_memory_bytes or 20_000_000,
        )

    summary = run_demo(
        state_dir=args.state_dir,
        fixtures_dir=args.fixtures_dir,
        fresh=args.fresh,
        crash_after_batches=args.crash_after,
        calibrate=not args.skip_calibration,
        batch_size=args.batch_size,
        strategy=args.strategy,
        threshold_view=args.threshold_view,
        budget_config=budget,
    )
    printable_run = {
        key: value
        for key, value in summary.get("run", {}).items()
        if not key.endswith("this_run") and key not in {"processed", "reused"}
    }
    printable = {
        "ok": summary.get("ok"),
        "run": printable_run,
        "replay": summary.get("replay"),
        "snapshot_versions": summary.get("snapshot_versions"),
        "calibration_count": len(summary.get("calibrations", [])),
    }
    print(json.dumps(printable, indent=2, ensure_ascii=False))
    if not summary["ok"]:
        print(json.dumps(summary.get("calibration"), indent=2, ensure_ascii=False))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
