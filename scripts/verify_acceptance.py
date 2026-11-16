from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rsseg.engine import PipelineEngine
from rsseg.errors import E_RECOMPUTE_BUDGET_PENDING
from rsseg.state import StateStore
from rsseg.util import atomic_write_json, read_json


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify budgeted incremental convergence against a fresh full run")
    parser.add_argument("--workspace", default=str(ROOT / "sample"))
    parser.add_argument("--report", default=str(ROOT / "artifacts" / "acceptance_report.json"))
    parser.add_argument("--budget", type=int, default=2)
    parser.add_argument("--max-calls", type=int, default=100)
    args = parser.parse_args()

    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="rsseg-acceptance-") as tmp:
        tmp_path = Path(tmp)
        workspace = tmp_path / "sample"
        shutil.copytree(Path(args.workspace), workspace)
        config_path = workspace / "config.json"
        config = read_json(config_path)
        config["recompute_budget"] = args.budget
        atomic_write_json(config_path, config)

        full = PipelineEngine(workspace, tmp_path / "full", mode="full", max_batches=1).run()
        runs = []
        for _ in range(args.max_calls):
            run = PipelineEngine(workspace, tmp_path / "incremental", mode="incremental", max_batches=1).run()
            runs.append(run)
            if run.converged:
                break
        if not runs or not runs[-1].converged:
            raise RuntimeError("incremental run did not converge within max-calls")
        incremental = runs[-1]
        category_equal = np.array_equal(full.global_classes, incremental.global_classes)
        tile_set_equal = sorted(full.tile_results) == sorted(incremental.tile_results)
        classes_equal = tile_set_equal and all(
            np.array_equal(full.tile_results[key].classes, incremental.tile_results[key].classes)
            for key in full.tile_results
        )
        scores_equal = tile_set_equal and all(
            np.array_equal(full.tile_results[key].scores, incremental.tile_results[key].scores)
            for key in full.tile_results
        )
        inc_snapshot = StateStore(tmp_path / "incremental").load_snapshot()
        full_snapshot = StateStore(tmp_path / "full").load_snapshot()
        snapshot_valid = inc_snapshot is not None and full_snapshot is not None
        pending_observed = any(E_RECOMPUTE_BUDGET_PENDING in run.business_codes for run in runs[:-1])
        batch_counts = [run.report["batches"][0]["recomputed_tile_count"] for run in runs]
        report = {
            "schema_version": 1,
            "budget": args.budget,
            "workspace_copy": "temporary",
            "seed_sample": read_json(workspace / "config.json").get("seed"),
            "incremental_calls": len(runs),
            "pending_observed_before_convergence": pending_observed,
            "recomputed_tiles_per_call": batch_counts,
            "incremental_snapshot_versions": [run.snapshot_version for run in runs],
            "incremental_final_version": incremental.snapshot_version,
            "full_snapshot_version": full.snapshot_version,
            "snapshots_valid": snapshot_valid,
            "category_equal": bool(category_equal),
            "tile_set_equal": bool(tile_set_equal),
            "tile_classes_equal": bool(classes_equal),
            "tile_scores_equal": bool(scores_equal),
            "area_stats": incremental.report["area_stats"],
            "full_area_stats": full.report["area_stats"],
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "passed": bool(
                pending_observed
                and category_equal
                and tile_set_equal
                and classes_equal
                and scores_equal
                and snapshot_valid
            ),
        }
        atomic_write_json(args.report, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
