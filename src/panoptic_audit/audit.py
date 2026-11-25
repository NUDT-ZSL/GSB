from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .hashing import canonical_checksum
from .ledger import AdjudicatedLedger
from .groundtruth import ParsedTruth
from .probes import Probe
from .store import LedgerStore


def build_audit_report(
    *,
    config: dict[str, Any],
    manifest: dict[str, Any],
    accepted: list[Probe],
    rejected: list[dict[str, Any]],
    suppressed: list[dict[str, Any]],
    truths: dict[str, ParsedTruth],
    ledger: AdjudicatedLedger,
    conflict_summary: dict[str, Any],
    store: LedgerStore,
    degradation_history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    records = store.load_probe_records()
    records_by_uid = {record["uid"]: record for record in records}
    accepted_by_uid = {probe.uid: probe for probe in accepted}

    probe_reports: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for record in records:
        uid = record["uid"]
        probe = accepted_by_uid[uid]
        x, y = probe.x, probe.y
        raw_truth = {fmt: truth.raw_at(x, y) for fmt, truth in truths.items()}
        adjudication = ledger.pixel_at(x, y)
        entry = {
            **record,
            "raw_truth": raw_truth,
            "adjudication": adjudication,
            "replay": {
                "coordinate": {"x": x, "y": y},
                "ledger_version": config["ledger_version"],
                "ledger_revision": store.head()["revision"],
                "committed_revision": record.get("committed_revision"),
                "ledger_content_checksum": ledger.content_checksum,
                "record_checksum": record["record_checksum"],
                "replay_command": (
                    "panoptic-audit replay "
                    f"--x {x} --y {y} --ledger-revision {record.get('committed_revision')}"
                ),
            },
        }
        probe_reports.append(entry)
        if record["status"] == "MISMATCH":
            failures.append({
                "type": "PROBE_MISMATCH",
                "probe_id": record["probe_id"],
                "uid": uid,
                "coordinate": record["coordinate"],
                "business_code": record["business_code"],
                "detail": record["mismatch"],
            })
        if record["timing"]["budget_overrun"]:
            failures.append({
                "type": "PROBE_TIME_BUDGET_OVERRUN",
                "probe_id": record["probe_id"],
                "uid": uid,
                "elapsed_ns": record["timing"]["elapsed_ns"],
                "probe_budget_ns": record["timing"]["probe_budget_ns"],
            })

    for item in rejected:
        failures.append({"type": "PLAN_REJECTED", **item})

    progress = store.load_progress()
    head = store.head()
    report_body = {
        "report_version": "audit-report-v1",
        "sample_seed": manifest.get("seed"),
        "sample_manifest_version": manifest.get("version"),
        "config": {
            "probe_time_budget_ms": config["probe_time_budget_ms"],
            "conflict_coverage_threshold": config["conflict_coverage_threshold"],
            "format_priority": config["format_priority"],
            "ledger_version": config["ledger_version"],
            "probe_round_size": config.get("probe_round_size", 2),
            "slow_budget_share_threshold": config.get("slow_budget_share_threshold", 0.35),
        },
        "ledger": {
            "ledger_version": config["ledger_version"],
            "ledger_revision": head["revision"],
            "pixel_content_checksum": ledger.content_checksum,
            "probe_checksum": head["probe_checksum"],
            "progress_checksum": head["progress_checksum"],
            "combined_checksum": head["combined_checksum"],
            "progress": progress,
        },
        "conflict_summary": conflict_summary,
        "source_checksums": store.source_checksums(),
        "plan_summary": {
            "accepted_count": len(accepted),
            "committed_count": len(records),
            "rejected_count": len(rejected),
            "suppressed_count": len(suppressed),
        },
        "probes": probe_reports,
        "rejected_probes": rejected,
        "suppressed_probes": suppressed,
        "degradation_history": degradation_history or [],
        "failures": failures,
        "self_check": {"status": "PENDING"},
    }
    report_body["report_checksum"] = canonical_checksum(report_body)
    return report_body


def write_report_atomic(path: str | Path, report: dict[str, Any]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return path


def read_report(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))
