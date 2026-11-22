#!/usr/bin/env python3
"""Run the local acceptance matrix and write an auditable JSON report."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any, Callable

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from prefworkbench.canonical import read_json, sealed, verify_sealed  # noqa: E402
from prefworkbench.config import load_config, validate_config  # noqa: E402
from prefworkbench.errors import (  # noqa: E402
    E_CHECKSUM_MISMATCH,
    E_STALE_RESULT,
    E_VERSION_REGRESSION,
    E_WORKER_FAILURE,
    W_DEGRADED_GROUPING,
    BusinessError,
)
from prefworkbench.generator import write_sample  # noqa: E402
from prefworkbench.pipeline import run_pipeline  # noqa: E402
from prefworkbench.snapshots import SnapshotStore  # noqa: E402


WORK = ROOT / "artifacts" / "acceptance_work"
REPORT = ROOT / "artifacts" / "acceptance_report.json"


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(
                json.dumps(
                    row, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                + "\n"
            )


def expect_error(code: str, action: Callable[[], Any]) -> dict[str, Any]:
    try:
        action()
    except BusinessError as exc:
        return {"ok": exc.code == code, "code": exc.code, "details": exc.to_dict()}
    return {"ok": False, "code": None, "details": {"expected": code}}


def main() -> int:
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    report: dict[str, Any] = {
        "ok": True,
        "environment": {
            "python": sys.version.split()[0],
            "numpy": np.__version__,
        },
        "checks": {},
    }

    data_path = WORK / "sample.jsonl"
    config_path = WORK / "config.json"
    write_sample(data_path, config_path, count=120, seed=20261123)

    baseline = run_pipeline(
        input_path=data_path,
        config_path=config_path,
        out_dir=WORK / "runs" / "baseline",
        cache_root=WORK / "cache" / "baseline",
    )
    repeated = run_pipeline(
        input_path=data_path,
        config_path=config_path,
        out_dir=WORK / "runs" / "repeat",
        cache_root=WORK / "cache" / "baseline",
    )
    cold = run_pipeline(
        input_path=data_path,
        config_path=config_path,
        out_dir=WORK / "runs" / "cold",
        cache_root=WORK / "cache" / "cold",
        cold_cache=True,
    )
    report["checks"]["A_determinism_and_cold_equivalence"] = {
        "ok": (
            baseline["manifest_file_sha256"]
            == repeated["manifest_file_sha256"]
            == cold["manifest_file_sha256"]
        ),
        "baseline": baseline["manifest_file_sha256"],
        "repeat": repeated["manifest_file_sha256"],
        "cold": cold["manifest_file_sha256"],
        "repeat_reused_stages": repeated["diagnostics"]["incremental"]["reused_stages"],
    }

    stress_config = json.loads(config_path.read_text(encoding="utf-8"))
    stress_config["scheduler"]["mac_budget"] = 125000
    stress_config["scheduler"]["memory_budget_bytes"] = 3145728
    stress_path = WORK / "config_stress.json"
    stress_path.write_text(
        json.dumps(stress_config, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    stress = run_pipeline(
        input_path=data_path,
        config_path=stress_path,
        out_dir=WORK / "runs" / "stress",
        cache_root=WORK / "cache" / "stress",
    )
    stress_plan = stress["manifest"]["schedule"]["selected_plan"]
    report["checks"]["B_budget_adjudication"] = {
        "ok": (
            stress_plan["rejected_pair_ids"]
            and W_DEGRADED_GROUPING in stress["manifest"]["business_codes"]
            and "E_BUDGET_FLOP_EXCEEDED" in stress["manifest"]["business_codes"]
            and any(
                item["action"] == "degrade_grouping"
                for item in stress["manifest"]["schedule"]["adjudications"]
            )
        ),
        "selected_plan": stress_plan,
        "adjudications": stress["manifest"]["schedule"]["adjudications"],
        "rejected_codes": stress["manifest"]["schedule"]["rejected_codes"],
        "business_codes": stress["manifest"]["business_codes"],
    }

    rows = [
        json.loads(line)
        for line in data_path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    rows[0]["chosen"] += " single-group correction marker"
    modified_path = WORK / "sample_modified.jsonl"
    write_jsonl(modified_path, rows)
    incremental = run_pipeline(
        input_path=modified_path,
        config_path=config_path,
        out_dir=WORK / "runs" / "incremental",
        cache_root=WORK / "cache" / "baseline",
    )
    full_modified = run_pipeline(
        input_path=modified_path,
        config_path=config_path,
        out_dir=WORK / "runs" / "full_modified",
        cache_root=WORK / "cache" / "full_modified",
        cold_cache=True,
    )
    quality_audit = incremental["diagnostics"]["stage_audit"]["quality"]
    report["checks"]["C_incremental_recompute_and_reuse"] = {
        "ok": (
            quality_audit["status"] == "partially_recomputed"
            and len(quality_audit["recomputed_pair_ids"]) == 1
            and len(quality_audit["reused_pair_ids"]) == 119
            and not (
                set(quality_audit["recomputed_pair_ids"])
                & set(quality_audit["reused_pair_ids"])
            )
            and {"neardup", "consistency", "sampling", "manifest"}.issubset(
                set(incremental["diagnostics"]["incremental"]["recomputed_stages"])
            )
        ),
        "changed_input_pair_ids": incremental["diagnostics"]["incremental"][
            "changed_input_pair_ids"
        ],
        "quality_audit": quality_audit,
        "recomputed_stages": incremental["diagnostics"]["incremental"][
            "recomputed_stages"
        ],
        "reused_stages": incremental["diagnostics"]["incremental"]["reused_stages"],
    }
    report["checks"]["D_incremental_full_byte_identity"] = {
        "ok": (
            incremental["manifest_file_sha256"]
            == full_modified["manifest_file_sha256"]
            and (WORK / "runs" / "incremental" / "manifest.json").read_bytes()
            == (WORK / "runs" / "full_modified" / "manifest.json").read_bytes()
        ),
        "incremental": incremental["manifest_file_sha256"],
        "full_recompute": full_modified["manifest_file_sha256"],
    }

    threshold_config = json.loads(config_path.read_text(encoding="utf-8"))
    threshold_config["version"] = 2
    threshold_config["neardup"]["threshold"] = 0.70
    threshold_path = WORK / "config_threshold_v2.json"
    threshold_path.write_text(
        json.dumps(threshold_config, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    threshold_run = run_pipeline(
        input_path=modified_path,
        config_path=threshold_path,
        out_dir=WORK / "runs" / "threshold_v2",
        cache_root=WORK / "cache" / "baseline",
    )
    threshold_audit = threshold_run["diagnostics"]["stage_audit"]
    report["checks"]["E_threshold_change_suffix_recompute"] = {
        "ok": (
            threshold_audit["quality"]["status"] == "reused"
            and threshold_audit["neardup"]["status"] == "recomputed"
            and "quality" in threshold_run["diagnostics"]["incremental"]["reused_stages"]
            and "neardup"
            in threshold_run["diagnostics"]["incremental"]["recomputed_stages"]
        ),
        "statuses": {
            stage: threshold_audit[stage]["status"]
            for stage in ("quality", "neardup", "consistency", "sampling", "manifest")
        },
        "recomputed_stages": threshold_run["diagnostics"]["incremental"][
            "recomputed_stages"
        ],
        "reused_stages": threshold_run["diagnostics"]["incremental"]["reused_stages"],
    }

    config_v1 = load_config(config_path)
    config_v2 = load_config(threshold_path)
    manifest_payload = verify_sealed(
        read_json(WORK / "runs" / "baseline" / "manifest.json"),
        context="baseline manifest",
    )
    store = SnapshotStore(WORK / "cache" / "baseline" / "snapshots")
    stale = expect_error(E_STALE_RESULT, lambda: store.assert_fresh(manifest_payload, config_v2))

    changed_same_version = json.loads(json.dumps(config_v2.payload))
    changed_same_version["quality"]["weights"]["length"] = 0.29
    changed_same_version["quality"]["weights"]["repetition"] = 0.36
    checksum = expect_error(
        E_CHECKSUM_MISMATCH,
        lambda: store.publish(validate_config(changed_same_version)),
    )
    regression = expect_error(E_VERSION_REGRESSION, lambda: store.publish(config_v1))

    tampered = read_json(WORK / "runs" / "baseline" / "manifest.json")
    tampered["config"]["version"] = 999
    tamper = expect_error(
        E_CHECKSUM_MISMATCH,
        lambda: verify_sealed(tampered, context="tampered manifest"),
    )
    report["checks"]["F_stale_versioning_and_tamper"] = {
        "ok": stale["ok"] and checksum["ok"] and regression["ok"] and tamper["ok"],
        "stale": stale,
        "checksum_mismatch": checksum,
        "version_regression": regression,
        "tamper": tamper,
    }

    failure_rows = [
        json.loads(line)
        for line in data_path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    failing = dict(failure_rows[0])
    failing["pair_id"] = "P99999"
    failing["__fail_stage__"] = "quality"
    failure_rows.append(failing)
    failure_path = WORK / "sample_worker_failure.jsonl"
    write_jsonl(failure_path, failure_rows)
    failure_run = run_pipeline(
        input_path=failure_path,
        config_path=config_path,
        out_dir=WORK / "runs" / "worker_failure",
        cache_root=WORK / "cache" / "worker_failure",
    )
    failure_record = next(
        record
        for record in failure_run["manifest"]["records"]
        if record["pair_id"] == "P99999"
    )["admission"]
    report["checks"]["G_worker_failure_attribution"] = {
        "ok": (
            failure_record["code"] == E_WORKER_FAILURE
            and failure_record["stage"] == "quality"
            and failure_record["batch_id"] >= 0
            and failure_record["execution_error"]["pair_ids"] == ["P99999"]
        ),
        "error": failure_record,
    }

    report["checks"]["H_selfcheck_and_quota_recount"] = {
        "ok": (
            baseline["manifest"]["selfcheck"]["ok"]
            and incremental["manifest"]["selfcheck"]["ok"]
            and threshold_run["manifest"]["selfcheck"]["ok"]
            and sum(
                quota["selected_count"]
                for quota in baseline["manifest"]["quota_stats"]
            )
            == len(baseline["manifest"]["sampling_sequence"])
        ),
        "baseline_selfcheck": baseline["manifest"]["selfcheck"],
        "incremental_selfcheck": incremental["manifest"]["selfcheck"],
        "selected_count": len(baseline["manifest"]["sampling_sequence"]),
        "quota_selected_sum": sum(
            quota["selected_count"] for quota in baseline["manifest"]["quota_stats"]
        ),
    }

    report["ok"] = all(check["ok"] for check in report["checks"].values())
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
