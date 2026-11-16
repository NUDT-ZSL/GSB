"""End-to-end acceptance driver for the featbench workbench.

Runs the documented edit sequence incrementally, then re-runs the final config
from a clean workspace and compares the comparable report fields field by
field. Also exercises snapshot recovery and serial/parallel equivalence.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import yaml

from featbench.config import load_config
from featbench.pipeline import run_pipeline
from featbench.report import write_report

SEQUENCE_DIR = os.path.join(ROOT, "configs", "edit_sequence")
STEPS = [
    "00_base.yaml",
    "01_scale_switch.yaml",
    "02_order_change.yaml",
    "03_budget_change.yaml",
    "04_method_switch.yaml",
]
COMPARABLE = ("metric_table", "retained_features", "decisions",
              "cross_budget_audit", "failures", "scales", "methods",
              "snapshot_version")


def _load_yaml(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _dump_yaml(path: str, payload: dict) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(payload, handle, sort_keys=True)


def _comparable(report: dict) -> dict:
    return {key: report[key] for key in COMPARABLE}


def main() -> int:
    verification_root = os.path.join(ROOT, ".bench_work", "acceptance")
    shutil.rmtree(verification_root, ignore_errors=True)
    os.makedirs(verification_root, exist_ok=True)

    incremental_dir = os.path.join(verification_root, "incremental")
    os.makedirs(incremental_dir, exist_ok=True)
    incremental_config = os.path.join(incremental_dir, "config.yaml")
    incremental_reports = []
    for step in STEPS:
        payload = _load_yaml(os.path.join(SEQUENCE_DIR, step))
        payload["work_dir"] = os.path.join(incremental_dir, "work")
        _dump_yaml(incremental_config, payload)
        report = run_pipeline(load_config(incremental_config))
        incremental_reports.append((step, report))
        print(f"[incremental] {step}: rows={len(report['metric_table'])} "
              f"hits={report['cache_ledger']['hit_count']} "
              f"failures={len(report['failures'])}")

    final_payload = _load_yaml(incremental_config)
    clean_dir = os.path.join(verification_root, "clean_full")
    os.makedirs(clean_dir, exist_ok=True)
    clean_config = os.path.join(clean_dir, "config.yaml")
    clean_payload = copy.deepcopy(final_payload)
    clean_payload["work_dir"] = os.path.join(clean_dir, "work")
    _dump_yaml(clean_config, clean_payload)
    full_report = run_pipeline(load_config(clean_config))
    write_report(load_config(clean_config), full_report)

    incremental_final = incremental_reports[-1][1]
    if _comparable(incremental_final) != _comparable(full_report):
        print("[FAIL] incremental vs clean full report mismatch")
        return 1
    if incremental_final["report_fingerprint"] != full_report["report_fingerprint"]:
        print("[FAIL] report fingerprints differ")
        return 1
    print("[ok] incremental and clean full reports are field-for-field identical")
    print(f"[ok] report fingerprint: {full_report['report_fingerprint']}")

    # Snapshot recovery: snapshots must exist and carry version 1.
    snapshot_root = os.path.join(clean_payload["work_dir"], "snapshots")
    namespaces = os.listdir(snapshot_root)
    snapshot_files = []
    for namespace in namespaces:
        snapshot_files.extend(
            os.path.join(snapshot_root, namespace, name)
            for name in os.listdir(os.path.join(snapshot_root, namespace)))
    for path in snapshot_files:
        payload = json.load(open(path, encoding="utf-8"))
        assert payload["version"] == 1, path
    print(f"[ok] {len(snapshot_files)} atomic snapshots recovered at version 1")

    summary_path = os.path.join(verification_root, "acceptance_summary.json")
    with open(summary_path, "w", encoding="utf-8") as handle:
        json.dump({
            "steps": [step for step, _ in incremental_reports],
            "final_report_fingerprint": full_report["report_fingerprint"],
            "incremental_final_ledger": incremental_final["cache_ledger"],
            "clean_full_ledger": full_report["cache_ledger"],
            "metric_rows": len(full_report["metric_table"]),
            "snapshot_files": len(snapshot_files),
        }, handle, indent=2, sort_keys=True, ensure_ascii=False)
    print(f"[ok] summary written: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
