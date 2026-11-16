"""Single-command acceptance driver.

Runs the complete validation set against the deterministic local samples and
writes a consolidated JSON acceptance report:

* clean end-to-end pipeline (parse -> convert -> fuse -> snapshot -> report)
* corrupt packages asserting E_RLE_OUT_OF_RANGE / E_MASK_LENGTH_MISMATCH /
  E_PALETTE_MISSING business codes
* single-thread vs thread-pool determinism
* boundary diagnostics (threshold and policy sensitive instances)
* incremental vs full rebuild consistency matrix (2 compression levels x
  2 source formats x 5 edit steps)
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from panseg.models import PipelineConfig
from panseg.pipeline import PanopticPipeline, run_conversion_roundtrip
from panseg.report import build_report, write_report
from scripts.consistency_matrix import run_matrix


def _load_config(sample_dir: Path) -> PipelineConfig:
    return PipelineConfig.load(sample_dir / "pipeline_config.json")


def _clean_run(sample_dir: Path, work_dir: Path) -> dict:
    config = _load_config(sample_dir)
    pipeline = PanopticPipeline(sample_dir, config, work_dir / "clean")
    upgrade, downgrade, _ = run_conversion_roundtrip(sample_dir, work_dir / "clean", config)
    result = pipeline.run()
    result.conversion_losses.extend(upgrade)
    result.conversion_losses.extend(downgrade)
    report = build_report(result, pipeline.current_config_hash, 0)
    write_report(work_dir / "clean" / "validation_report.json", report)
    return report


def _corrupt_run(sample_dir: Path, work_dir: Path) -> list[dict]:
    config = _load_config(sample_dir)
    overrides = {
        "scene-00.v1.bin": sample_dir / "corrupt" / "scene-00.v1.bin",
        "scene-01.v2.json": sample_dir / "corrupt" / "scene-01.v2.json",
        "scene-02.v2.json": sample_dir / "corrupt" / "scene-02.v2.json",
    }
    pipeline = PanopticPipeline(sample_dir, config, work_dir / "corrupt", file_overrides=overrides)
    result = pipeline.run()
    report = build_report(result, pipeline.current_config_hash, 2)
    write_report(work_dir / "corrupt" / "validation_report.json", report)
    return [failure.to_dict() for failure in result.failures]


def _determinism_run(sample_dir: Path, work_dir: Path) -> dict:
    outputs = {}
    for workers in (1, 8):
        config = PipelineConfig(**{**_load_config(sample_dir).to_dict(), "workers": workers})
        target = work_dir / f"determinism-w{workers}"
        shutil.rmtree(target, ignore_errors=True)
        pipeline = PanopticPipeline(sample_dir, config, target)
        result = pipeline.run()
        report = build_report(result, pipeline.current_config_hash, 0)
        outputs[workers] = {
            "fused_order": [
                [fused["fused_id"] for fused in scene["fused_instances"]]
                for scene in report["fused_scenes"]
            ],
            "conflict_order": [item["sort_key"] for item in report["conflicts"]],
            "failure_order": [item["sort_key"] for item in report["failures"]],
            "pixels": [
                [row for row in _label_rows(pipeline, scene)] for scene in sorted(result.fusion_results)
            ],
        }
    equal = outputs[1] == outputs[8]
    return {"single_vs_threadpool_equal": equal, "workers_1": outputs[1], "workers_8": outputs[8]}


def _label_rows(pipeline: PanopticPipeline, scene_id: str):
    return pipeline.store.recover()[2][scene_id].label_map.tolist()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", default="sample_data")
    parser.add_argument("--out", default="validation/acceptance")
    args = parser.parse_args()
    sample_dir = Path(args.sample)
    out_dir = Path(args.out)
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    clean = _clean_run(sample_dir, out_dir)
    corrupt = _corrupt_run(sample_dir, out_dir)
    determinism = _determinism_run(sample_dir, out_dir)
    matrix = run_matrix(sample_dir, out_dir / "consistency")

    expected_codes = {"E_RLE_OUT_OF_RANGE", "E_MASK_LENGTH_MISMATCH", "E_PALETTE_MISSING"}
    observed_codes = {item["code"] for item in corrupt}
    acceptance = {
        "sample_summary": json.loads((sample_dir / "sample_summary.json").read_text()),
        "clean_run": {
            "instance_count": clean["instance_count"],
            "snapshot_version": clean["snapshot_version"],
            "downgrade_loss_count": len(clean["downgrade_losses"]),
            "recomputed_fusions": clean["recomputed_fusions"],
        },
        "business_codes": {
            "observed": sorted(observed_codes),
            "expected": sorted(expected_codes),
            "all_present": expected_codes.issubset(observed_codes),
            "records": corrupt,
        },
        "boundary_diagnostics": clean["boundary_diagnostics"],
        "determinism": {
            "single_vs_threadpool_equal": determinism["single_vs_threadpool_equal"],
        },
        "consistency_matrix": {
            key: all(step["equal"] for step in steps) for key, steps in matrix.items()
        },
        "passed": False,
    }
    acceptance["passed"] = (
        acceptance["business_codes"]["all_present"]
        and determinism["single_vs_threadpool_equal"]
        and all(acceptance["consistency_matrix"].values())
        and bool(acceptance["boundary_diagnostics"]["threshold_sensitive"])
        and bool(acceptance["boundary_diagnostics"]["policy_sensitive"])
    )
    write_report(out_dir / "acceptance_report.json", acceptance)
    print(json.dumps(acceptance, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
