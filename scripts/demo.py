#!/usr/bin/env python3
"""Run the fixed local acceptance scenario twice and compare byte-for-byte."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"
ARTIFACTS = ROOT / "artifacts" / "demo"


def run(
    args: list[str],
    *,
    allow_nonzero: bool = False,
) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, "-m", "serverless_engine", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    parsed: Any = None
    if result.stdout.strip():
        parsed = json.loads(result.stdout)
    if result.returncode != 0 and not allow_nonzero:
        raise RuntimeError(
            "command failed: "
            + " ".join(args)
            + f"\nexit={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
        )
    return {
        "argv": args,
        "exit_code": result.returncode,
        "output": parsed,
    }


def scenario(run_dir: Path) -> dict[str, Any]:
    standard = run_dir / "standard"
    crash = run_dir / "crash"
    standard.mkdir(parents=True)
    crash.mkdir(parents=True)
    records: list[dict[str, Any]] = []

    common_load = [
        "load",
        "--functions",
        "samples/functions.json",
        "--layers",
        "samples/layers.json",
        "--snapshot-v1",
        "samples/snapshots/v1.json",
        "--snapshot-v2-index",
        "samples/snapshots/v2/index.json",
    ]
    records.append(run([*common_load, "--state-dir", str(standard)]))
    records.append(
        run(
            [
                "invoke",
                "--state-dir",
                str(standard),
                "--trace",
                "samples/traces/before.json",
            ]
        )
    )
    records.append(run(["status", "--state-dir", str(standard)]))
    records.append(
        run(
            [
                "apply",
                "--state-dir",
                str(standard),
                "--patch",
                "samples/patches/layer-update.json",
            ]
        )
    )
    records.append(
        run(
            [
                "invoke",
                "--state-dir",
                str(standard),
                "--trace",
                "samples/traces/after.json",
            ]
        )
    )
    records.append(run(["status", "--state-dir", str(standard)]))
    records.append(run(["verify", "--state-dir", str(standard)]))

    records.append(run([*common_load, "--state-dir", str(crash)]))
    records.append(
        run(
            [
                "invoke",
                "--state-dir",
                str(crash),
                "--trace",
                "samples/traces/crash-report.json",
                "--failpoint",
                "init-started",
            ],
            allow_nonzero=True,
        )
    )
    records.append(
        run(
            [
                "apply",
                "--state-dir",
                str(crash),
                "--patch",
                "samples/patches/runtime-update.json",
                "--failpoint",
                "mid-apply",
            ],
            allow_nonzero=True,
        )
    )
    records.append(run(["status", "--state-dir", str(crash)]))
    records.append(run(["resume", "--state-dir", str(crash)]))
    records.append(
        run(
            [
                "invoke",
                "--state-dir",
                str(crash),
                "--trace",
                "samples/traces/crash-report-after.json",
            ]
        )
    )
    records.append(run(["status", "--state-dir", str(crash)]))
    records.append(run(["verify", "--state-dir", str(crash)]))
    for record in records:
        record["argv"] = [
            "<STATE>" if str(run_dir) in argument else argument
            for argument in record["argv"]
        ]
    return {"records": records}


def assertions(bundle: dict[str, Any]) -> dict[str, Any]:
    records = bundle["records"]
    before = records[1]["output"]["calls"]
    layer_apply = records[3]["output"]
    after = records[4]["output"]["calls"]
    crash_invoke = records[8]
    crash_apply = records[9]
    resume = records[11]["output"]
    crash_after = records[12]["output"]["calls"][0]
    standard_verify = records[6]["output"]
    crash_verify = records[14]["output"]
    checks = {
        "warm_reuse": before[0]["outcome"] == "REUSED_WARM"
        and before[1]["outcome"] == "REUSED_WARM",
        "function_budget_abandon": before[2]["outcome"] == "BUDGET_NOT_STARTED"
        and before[2]["reason_code"] == "E_FUNCTION_CONCURRENCY",
        "global_budget_abandon": before[4]["outcome"] == "BUDGET_NOT_STARTED"
        and before[4]["reason_code"] == "E_GLOBAL_BUDGET",
        "cold_start_success": before[5]["outcome"] == "COLD_START_SUCCEEDED",
        "layer_closure": layer_apply["affected_functions"]
        == ["legacy-handler", "resize-image"],
        "tombstone_blocks_new_call": after[3]["outcome"] == "FAILED_PRE_START"
        and after[3]["reason_code"] == "E_LAYER_TOMBSTONED",
        "modified_layer_forces_cold_start": after[2]["outcome"]
        == "COLD_START_SUCCEEDED"
        and after[2]["start_mode"] == "COLD_START",
        "unaffected_warm_reuse": after[0]["outcome"] == "REUSED_WARM"
        and after[1]["outcome"] == "REUSED_WARM",
        "forced_invoke_termination": crash_invoke["exit_code"] != 0,
        "forced_apply_termination": crash_apply["exit_code"] != 0,
        "resume_reapplies_patch": any(
            item["action"] == "reapplied_patch"
            for item in resume["recovered_jobs"]
        ),
        "in_flight_call_completed": any(
            call["outcome"] == "COLD_START_SUCCEEDED"
            for call in resume["drained_calls"]
        ),
        "obsolete_instance_invalidated_after_completion": any(
            instance["function"] == "report-csv"
            and instance["stage"] == "INVALIDATED"
            and instance["bound_layer_versions"]["base-runtime"] == 3
            for instance in records[13]["output"]["instances"]
        ),
        "post_resume_cold_start": crash_after["outcome"] == "COLD_START_SUCCEEDED"
        and crash_after["cache_hit"] is False,
        "standard_verify": bool(standard_verify["ok"]),
        "crash_verify": bool(crash_verify["ok"]),
    }
    return checks


def main() -> int:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [sys.executable, "scripts/generate_samples.py", "--out", str(SAMPLES)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    bundles = []
    hashes = []
    for run_number in (1, 2):
        run_dir = ARTIFACTS / f"run{run_number}"
        if run_dir.exists():
            shutil.rmtree(run_dir)
        bundle = scenario(run_dir)
        text = json.dumps(bundle, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
        (ARTIFACTS / f"run{run_number}.json").write_text(
            text, encoding="utf-8", newline="\n"
        )
        bundles.append(bundle)
        hashes.append(hashlib.sha256(text.encode("utf-8")).hexdigest())
    checks = assertions(bundles[0])
    identical = hashes[0] == hashes[1]
    summary = {
        "ok": identical and all(checks.values()),
        "runs_identical": identical,
        "run_sha256": hashes[0],
        "checks": checks,
        "outputs": [
            "artifacts/demo/run1.json",
            "artifacts/demo/run2.json",
        ],
    }
    (ARTIFACTS / "summary.json").write_text(
        json.dumps(summary, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(summary, sort_keys=True, indent=2, ensure_ascii=False))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
