"""Diagnostic report assembly and atomic JSON persistence."""
from __future__ import annotations

import os

from .atomicio import SNAPSHOT_VERSION, atomic_write_json
from .cache import CacheLedger
from .config import Config
from .hashing import digest_iterable
from .snapshot import SnapshotStore


def assemble_report(config: Config, scale_results: list[dict],
                    failures: list[dict], ledger: CacheLedger,
                    snapshots: SnapshotStore) -> dict:
    ordered = sorted(scale_results, key=lambda item: item["task"]["sort_key"])
    ordered_failures = sorted(
        failures,
        key=lambda item: f"{item.get('scale') or ''}|{item.get('stage') or ''}|"
                         f"{item.get('code') or ''}",
    )
    cross_audits = []
    seen_audits: set[str] = set()
    decisions = []
    metric_table = []
    retained = []
    for result in ordered:
        audit_key = result["scale_id"]
        for entry in result["cross_audit"]:
            dedupe = f"{audit_key}|{entry['cross']}"
            if dedupe not in seen_audits:
                seen_audits.add(dedupe)
                cross_audits.append({"scale_id": audit_key, **entry})
        decisions.append(result["decision"])
        metric_table.append({
            "sort_key": result["task"]["sort_key"],
            "scale_id": result["scale_id"],
            "method": result["method"],
            **result["metrics"],
        })
        retained.append({
            "sort_key": result["task"]["sort_key"],
            "scale_id": result["scale_id"],
            "method": result["method"],
            "retained_dim": result["retained_dim"],
            "retained_features": result["retained_features"],
        })
    report = {
        "schema_version": 1,
        "experiment": config.experiment,
        "config_fingerprint": config.config_fingerprint,
        "snapshot_version": SNAPSHOT_VERSION,
        "snapshot_versions": snapshots.list_versions(),
        "scales": [scale.id for scale in config.scales],
        "methods": list(config.reduction.methods),
        "metric_table": metric_table,
        "retained_features": retained,
        "decisions": decisions,
        "cross_budget_audit": cross_audits,
        "cache_ledger": ledger.as_dict(),
        "failures": ordered_failures,
    }
    report["report_fingerprint"] = digest_iterable([
        report["config_fingerprint"], report["metric_table"],
        report["retained_features"], report["decisions"],
        report["cross_budget_audit"], report["failures"],
    ])
    return report


def write_report(config: Config, report: dict) -> str:
    path = os.path.join(config.work_dir, "diagnostic_report.json")
    atomic_write_json(path, report)
    return path
