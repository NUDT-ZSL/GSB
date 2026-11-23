"""End-to-end demonstration of the gbtdiag domain component.

Run with::

    python examples/run_demo.py

The script regenerates the deterministic local sample, cleans the run
directory, exercises train/predict/validate/inject and writes JSON
snapshots plus numpy caches under ``runs/demo``.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gbtdiag import (  # noqa: E402
    TrainConfig,
    inject_diagnose,
    make_injection,
    make_regression_dataset,
    predict,
    train,
    validate,
)
from gbtdiag.io_utils import (  # noqa: E402
    cache_array,
    prepare_run_dir,
    snapshot,
    stable_hash,
)


def heading(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def main() -> int:
    run_dir = prepare_run_dir(ROOT / "runs" / "demo")

    config = TrainConfig(
        n_trees=25,
        max_depth=4,
        learning_rate=0.1,
        subsample=0.8,
        colsample=0.9,
        min_leaf_samples=6,
        n_thresholds=10,
        precision="float64",
        seed=20261124,
    )
    dataset = make_regression_dataset(
        n_rows=512, n_numeric=6, n_categorical=3, config=config
    )
    print(
        f"dataset: rows={dataset.n_rows} features={dataset.n_features} "
        f"categorical={dataset.categorical_names}"
    )

    heading("1. deterministic training + inference")
    model = train(dataset, config, run_dir=run_dir / "clean")
    inference = predict(model, dataset.X)
    start_loss = model.round_details[0].loss_before
    end_loss = model.round_details[-1].loss_after
    print(f"RMSE {start_loss:.6f} -> {end_loss:.6f}")
    print(f"final prediction sha256: {stable_hash(model.final_prediction)}")
    print(f"inference matches training: {np.allclose(inference, model.final_prediction)}")
    cache_array(run_dir, "final_prediction", model.final_prediction)

    heading("2. reproducibility + error attribution validation")
    validation = validate(
        dataset, config, run_dir=run_dir / "validation"
    )
    print(f"verification code : {validation['verification']['code']}")
    print(f"verification pass : {validation['verification']['passed']}")
    print(f"error analysis    : {validation['error_analysis']['code']}")
    print(f"dominant stage    : {validation['error_analysis']['dominant_stage']}")

    heading("3. reduced precision (float32) error propagation")
    fp32_config = replace(
        config, precision="float32", rounding="half_even"
    )
    fp32_report = validate(
        dataset, fp32_config, run_dir=run_dir / "float32"
    )
    first_round = fp32_report["error_analysis"]["rounds"][0]
    for deviation in first_round["deviations"]:
        print(
            f"  {deviation['stage']:<13} rel={deviation['rel_deviation']:.3e} "
            f"amp={deviation['amplification']:.2f} "
            f"interval=({deviation['interval'][0]:.2e},"
            f"{deviation['interval'][1]:.2e})"
        )
    print(f"dominant error source: {fp32_report['error_analysis']['dominant_stage']}")

    heading("4. four-stage anomaly injection + degradation chain")
    specs = [
        make_injection(
            "category_oor",
            feature_index=6,
            sample_indices=[2, 14, 88],
            value=999,
        ),
        make_injection(
            "duplicate_threshold", round_index=3, feature_index=0
        ),
        make_injection(
            "nonfinite_hessian",
            round_index=6,
            sample_indices=[5, 21, 77],
            value=float("nan"),
        ),
        make_injection(
            "negative_learning_rate", round_index=9, value=-0.5
        ),
        make_injection("raw_fault", stage="gain", round_index=12),
    ]
    injection_report = inject_diagnose(
        dataset,
        config,
        specs,
        run_dir=run_dir / "injection",
    )
    attribution = injection_report["attribution"]
    print("stage counts :", attribution["stage_counts"])
    print("code counts  :", attribution["code_counts"])
    for degradation in attribution["degradations"]:
        print(
            f"  degrade {degradation['code']} -> "
            f"{degradation['degradation']['category']} "
            f"({degradation['degradation']['action']})"
        )
    print("unattributed :", len(attribution["unattributed"]))
    print(
        "verification :",
        injection_report["verification"]["code"],
        injection_report["verification"]["passed"],
    )

    heading("5. tolerance violation path")
    tight = replace(
        fp32_config, stage_tolerances=(1e-12,) * 5
    )
    tight_report = validate(
        dataset, tight, run_dir=run_dir / "tight"
    )
    print(f"code: {tight_report['error_analysis']['code']}")
    violation = tight_report["error_analysis"]["violations"][0]
    print(
        f"first violation: stage={violation['stage']} "
        f"round={violation['round_index']} row={violation['sample_index']} "
        f"rel={violation['rel_deviation']:.3e}"
    )

    snapshot(run_dir, "demo_summary", {
        "clean_prediction_hash": stable_hash(model.final_prediction),
        "verification": validation["verification"]["code"],
        "attribution_codes": attribution["code_counts"],
        "tight_code": tight_report["error_analysis"]["code"],
    })
    print(f"\nsnapshots written under: {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
