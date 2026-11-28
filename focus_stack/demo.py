"""Runnable end-to-end demonstration and lightweight acceptance harness."""

from __future__ import annotations

import argparse
import json
import os
from typing import List

import numpy as np

from .config import CoreConfig
from .incremental import incremental_replace
from .io_stack import FocusStack
from .persistence import save_result
from .pipeline import run_full
from .reference import reference_fuse
from .reporting import build_report
from .samples import SampleSpec, generate_dataset, generate_stack


def _print_frame_summaries(stack: FocusStack) -> None:
    print(f"\n[{stack.stack_id}] shape={stack.shape} planes={stack.n_planes}")
    for summary in stack.summaries:
        print(
            "  frame {idx}: focus={focus:.3f} mag={mag:.3f} "
            "dof=[{near:.3f},{far:.3f}] mean={mean:.2f} var={variance:.2f} "
            "lap={laplacian:.2f}".format(
                idx=summary.frame_index,
                focus=summary.focus_distance,
                mag=summary.magnification,
                near=summary.dof_near,
                far=summary.dof_far,
                mean=summary.mean,
                variance=summary.variance,
                laplacian=summary.laplacian_energy,
            )
        )


def _compare(name: str, actual: np.ndarray, expected: np.ndarray, tol: float) -> float:
    diff = float(np.max(np.abs(actual.astype(np.float64) - expected.astype(np.float64))))
    status = "OK" if diff <= tol else "FAIL"
    print(f"  compare {name}: max_abs_diff={diff:.3e} tol={tol:.0e} [{status}]")
    return diff


def run_demo(output_dir: str, config: CoreConfig) -> dict:
    stacks = generate_dataset(
        [
            SampleSpec("archival_microfilm", height=40, width=52, n_planes=6, seed=101),
            SampleSpec("macro_contemporary", height=36, width=48, n_planes=5, seed=202),
        ]
    )
    reports = []
    for stack in stacks:
        _print_frame_summaries(stack)
        result = run_full(stack, config)
        reference = reference_fuse(stack, config)
        print("  independent reference comparison:")
        _compare("sharpness", result.sharpness, reference.sharpness, config.compare_tol)
        _compare(
            "selection",
            result.selected_map.astype(np.float64),
            reference.selected_map.astype(np.float64),
            0.0,
        )
        _compare("fused", result.fused, reference.fused, config.compare_tol)
        _compare("ledger", result.ledger, reference.ledger, config.compare_tol)

        # Deterministic replacement: perturb one frame with a seed-local change.
        rng = np.random.default_rng(7)
        changed_plane = 1
        replacement = stack.frames[changed_plane].copy()
        replacement[8:20, 10:26] = np.clip(
            replacement[8:20, 10:26].astype(np.int64)
            + rng.integers(-40, 40, size=(12, 16)),
            0,
            4095,
        ).astype(np.uint16)
        new_stack = stack.replace_frame(changed_plane, replacement)
        increment = incremental_replace(
            stack, new_stack, changed_plane, result, config
        )
        print(
          "  incremental: bounds={bounds} changed_pixels={changed} "
          "fused_diff={fd:.3e} selection_diff={sd}".format(
                bounds=increment.recompute_bounds,
                changed=int(increment.change_mask.sum()),
                fd=increment.max_abs_diff_fused,
                sd=increment.max_abs_diff_selection,
            )
        )
        outside = ~increment.affected_mask
        inside = increment.affected_mask
        untouched_equal = bool(
            np.array_equal(result.fused[outside], increment.result.fused[outside])
            and np.array_equal(
                result.selected_map[outside],
                increment.result.selected_map[outside],
            )
            and np.array_equal(
                increment.result.fused[inside],
                increment.full_recompute.fused[inside],
            )
        )
        print(f"  untouched pixels bit-identical: {untouched_equal}")

        report = build_report(new_stack, increment.result, config)
        report["incremental"] = {
            "changed_plane": changed_plane,
            "recompute_bounds": list(increment.recompute_bounds),
            "changed_pixels": int(increment.change_mask.sum()),
            "max_abs_diff_fused": increment.max_abs_diff_fused,
            "max_abs_diff_selection": increment.max_abs_diff_selection,
            "untouched_bit_identical": untouched_equal,
        }
        reports.append(report)

        save_result(
            output_dir,
            stack.stack_id,
            version=1,
            stack=new_stack,
            config=config,
            result=increment.result,
            incremental={
                "recompute_bounds": list(increment.recompute_bounds),
                "changed_plane": changed_plane,
                "ledger_version_note": "v1 includes recomputed region annotation",
            },
        )

    # Reproducibility: regenerate one stack and rerun the full pipeline.
    spec = SampleSpec("archival_microfilm", height=40, width=52, n_planes=6, seed=101)
    rerun_a = run_full(generate_stack(spec), config)
    rerun_b = run_full(generate_stack(spec), config)
    reproducible = (
        np.array_equal(rerun_a.fused, rerun_b.fused)
        and np.array_equal(rerun_a.selected_map, rerun_b.selected_map)
        and np.array_equal(rerun_a.ledger, rerun_b.ledger)
    )
    print(f"\nreproducibility rerun bit-identical: {reproducible}")

    summary = {
        "reproducible": reproducible,
        "reports": reports,
        "output_dir": os.path.abspath(output_dir),
    }
    with open(os.path.join(output_dir, "demo_report.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True)
    return summary


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local focus-stacking kernel demo")
    parser.add_argument(
        "--output",
        default=os.path.join(os.getcwd(), "output"),
        help="directory for versioned outputs and reports",
    )
    args = parser.parse_args(argv)
    os.makedirs(args.output, exist_ok=True)
    config = CoreConfig()
    summary = run_demo(args.output, config)
    if not summary["reproducible"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
