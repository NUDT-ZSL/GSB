"""End-to-end, repeatable demonstration and acceptance runner.

Runs entirely on local deterministic NumPy samples.  It exercises:

1. deterministic generation + byte-identical reruns;
2. forced termination in a child process + resume equality;
3. weight revision invalidation with downstream-only recomputation;
4. corruption and rollback protection;
5. non-blocking stage lock and resource budgets;
6. parameter-driven fault injection and attribution;
7. cross-module consistency self-check.

A machine-readable report is written next to the working directories.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import replace

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qpipe import (  # noqa: E402
    E_CACHE_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
    QuantPipeline,
    ResourceBudget,
    RunContext,
    samplegen,
)
from qpipe.consistency import run_all_checks  # noqa: E402
from qpipe.storage import FileLock, ProgressStore, WorkDir  # noqa: E402


def _new_pipeline(data_dir, work_dir, seed, **kwargs):
    return QuantPipeline(
        data_dir, work_dir, context=RunContext(seed=seed), **kwargs
    )


def main() -> int:
    root = tempfile.mkdtemp(prefix="qpipe_demo_")
    data_dir = os.path.join(root, "data")
    seed = samplegen.DEFAULT_SEED
    samplegen.generate_workspace(data_dir, seed=seed, depth=5)
    report: dict = {"root": root, "checks": []}

    def record(name, ok, detail):
        report["checks"].append({"name": name, "ok": bool(ok), "detail": detail})

    # 1. Determinism: two independent work dirs produce identical bytes.
    run_a = _new_pipeline(data_dir, os.path.join(root, "wa"), seed).run_all()
    run_b = _new_pipeline(data_dir, os.path.join(root, "wb"), seed).run_all()
    dict_a, dict_b = run_a.to_dict(), run_b.to_dict()
    record(
        "deterministic_rerun_byte_identical",
        dict_a["prediction_checksum"] == dict_b["prediction_checksum"]
        and dict_a["activation_checksums"] == dict_b["activation_checksums"]
        and dict_a["quant_errors"] == dict_b["quant_errors"],
        {"prediction_checksum": dict_a["prediction_checksum"]},
    )

    # 2. Forced termination in a child, then resume equality.
    kill_work = os.path.join(root, "wk")
    child = subprocess.run(
        [
            sys.executable,
            "-m",
            "qpipe.cli",
            "--data-dir",
            data_dir,
            "--work-dir",
            kill_work,
            "--seed",
            str(seed),
            "run",
            "--kill-after-layer",
            "1",
            "--kill-stage",
            "infer",
        ],
        capture_output=True,
        text=True,
    )
    interrupted = ProgressStore(WorkDir(kill_work)).head()
    resumed = _new_pipeline(data_dir, kill_work, seed).resume().to_dict()
    record(
        "forced_kill_resume_byte_identical",
        child.returncode == 137
        and interrupted["stage"] == "calibrate"
        and resumed["prediction_checksum"] == dict_a["prediction_checksum"]
        and resumed["activation_checksums"]
        == dict_a["activation_checksums"],
        {
            "child_exit_code": child.returncode,
            "interrupted_stage": interrupted["stage"],
        },
    )

    # 3. Revision invalidation: only changed + downstream recompute.
    rev_pipe = _new_pipeline(data_dir, os.path.join(root, "wr"), seed)
    package = samplegen.load_manifest(rev_pipe._manifest_path)
    rev_pipe.run_all()
    changed = 2
    specs, weights = [], {}
    for spec in package.layers:
        if spec.layer_index == changed:
            spec = replace(spec, weight_revision=spec.weight_revision + 1)
            weights[changed] = samplegen.generate_weight(spec, seed=seed)
        specs.append(spec)
    new_package = type(package)(model_tag=package.model_tag, layers=tuple(specs))
    rev_summary = rev_pipe.apply_revision(
        new_package, changed_layer=changed, new_weights=weights
    ).to_dict()
    record(
        "revision_downstream_only_recompute",
        rev_summary["recomputed_layers"]
        == new_package.downstream_of(changed)
        and rev_summary["reused_layers"] == [0, 1],
        {
            "recomputed": rev_summary["recomputed_layers"],
            "reused": rev_summary["reused_layers"],
        },
    )

    # 4a. Rollback protection.
    older = [
        replace(s, weight_revision=s.weight_revision - 1)
        if s.layer_index == changed
        else s
        for s in new_package.layers
    ]
    rollback_code = None
    try:
        rev_pipe.apply_revision(
            type(package)(model_tag=package.model_tag, layers=tuple(older)),
            changed_layer=changed,
            new_weights={
                changed: samplegen.generate_weight(older[changed], seed=seed)
            },
        )
    except Exception as exc:  # noqa: BLE001 - business code asserted below
        rollback_code = getattr(exc, "code", None)
    record(
        "rollback_rejected",
        rollback_code == E_SNAPSHOT_ROLLBACK,
        {"code": rollback_code},
    )

    # 4b. Corruption detection on cache entry.
    corrupt_pipe = _new_pipeline(data_dir, os.path.join(root, "wc"), seed)
    corrupt_pipe.run_all()
    entry_path = corrupt_pipe.work.cache_entry_path("quantize", 3)
    with open(entry_path, "r", encoding="utf-8") as handle:
        tampered = json.load(handle)
    tampered["payload"]["scale"] = 123.0
    with open(entry_path, "w", encoding="utf-8") as handle:
        json.dump(tampered, handle)
    corrupt_code = None
    try:
        corrupt_pipe.cache.read_entry("quantize", 3)
    except Exception as exc:  # noqa: BLE001
        corrupt_code = getattr(exc, "code", None)
    record(
        "cache_corruption_detected",
        corrupt_code == E_CACHE_CORRUPT,
        {"code": corrupt_code, "layer_index": 3},
    )

    # 5. Non-blocking stage lock.
    work = WorkDir(os.path.join(root, "lock"))
    first = FileLock(work, "stage_infer")
    first.acquire()
    busy_code = None
    try:
        FileLock(work, "stage_infer").acquire()
    except Exception as exc:  # noqa: BLE001
        busy_code = getattr(exc, "code", None)
    first.release()
    record(
        "non_blocking_stage_lock",
        busy_code == E_STAGE_BUSY,
        {"code": busy_code},
    )

    # 6. Fault injection attribution across all four stages.
    faults = [
        {
            "stage": stage,
            "layer_index": idx,
            "kind": "injected",
            "code": "E_SHAPE_MISMATCH",
            "message": f"injected fault at {stage}",
        }
        for idx, stage in enumerate(
            ("validate", "quantize", "calibrate", "infer")
        )
    ]
    fault_pipe = QuantPipeline(
        data_dir,
        os.path.join(root, "wf"),
        context=RunContext(seed=seed, faults=faults),
    )
    fault_summary = fault_pipe.run_all().to_dict()
    stages_attributed = {r["stage"] for r in fault_summary["fault_records"]}
    record(
        "fault_attribution_all_stages",
        stages_attributed == set(("validate", "quantize", "calibrate", "infer")),
        {"stages": sorted(stages_attributed)},
    )

    # 7. Consistency self-check on a clean run.
    check_pipe = _new_pipeline(data_dir, os.path.join(root, "wcheck"), seed)
    check_pipe.run_all()
    check_package = samplegen.load_manifest(check_pipe._manifest_path)
    calibration = samplegen.load_calibration(check_pipe._calibration_path)
    consistency = run_all_checks(
        check_package, check_pipe.store, check_pipe.cache, calibration
    )
    record("consistency_self_check", consistency["ok"], consistency["checks"])

    # 8. Resource observations attached to the summary.
    observations = dict_a["resource_observations"]
    record(
        "resource_observations_present",
        "estimated_total_dequant_bytes" in observations
        and observations["stage_timings_ms"],
        {
            "estimated_total_dequant_bytes": observations[
                "estimated_total_dequant_bytes"
            ],
            "stage_timings_ms": observations["stage_timings_ms"],
        },
    )

    report["all_ok"] = all(item["ok"] for item in report["checks"])
    report_path = os.path.join(root, "acceptance_report.json")
    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, sort_keys=True, indent=2)
    print(json.dumps(report, sort_keys=True, indent=2))
    print(f"\nreport written to {report_path}")
    return 0 if report["all_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
