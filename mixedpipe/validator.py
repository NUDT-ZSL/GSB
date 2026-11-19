"""Independent ledger, result and serial-baseline equivalence checks."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from .canonical import content_checksum
from .compute import run_job
from .errors import (
    E_INCONSISTENT,
    E_SNAPSHOT_MISSING,
    E_SNAPSHOT_VERSION_CONFLICT,
    BusinessError,
)
from .models import PipelineConfig, Task, load_config, load_manifest
from .snapshots import SnapshotEnvelope, SnapshotStore


def _numeric_mismatch(
    actual: Any,
    expected: Any,
    atol: float,
    rtol: float,
) -> float | None:
    if isinstance(actual, bool) or isinstance(expected, bool):
        return None if actual == expected else float("inf")
    if not isinstance(actual, (int, float)) or not isinstance(expected, (int, float)):
        return None if actual == expected else float("inf")
    deviation = abs(float(actual) - float(expected))
    if math.isclose(
        float(actual),
        float(expected),
        rel_tol=rtol,
        abs_tol=atol,
    ):
        return 0.0
    return deviation


def _first_summary_inconsistency(
    actual: dict[str, Any],
    expected: dict[str, Any],
    config: PipelineConfig,
) -> dict[str, Any] | None:
    if actual.get("status") != expected.get("status"):
        return {
            "job_id": actual["job_id"],
            "field": "status",
            "actual": actual.get("status"),
            "expected": expected.get("status"),
            "deviation": 1,
        }
    if actual.get("status") == "error":
        for field in ("business_code", "stage"):
            if actual.get(field) != expected.get(field):
                return {
                    "job_id": actual["job_id"],
                    "field": field,
                    "actual": actual.get(field),
                    "expected": expected.get(field),
                    "deviation": 1,
                }
        return None

    actual_summary = actual["summary"]
    expected_summary = expected["summary"]
    for field in (
        "job_id",
        "job_kind",
        "model_tag",
        "seed",
        "feature_dim",
        "batch_size",
        "seq_len",
        "iterations",
    ):
        if actual_summary.get(field) != expected_summary.get(field):
            return {
                "job_id": actual["job_id"],
                "field": field,
                "actual": actual_summary.get(field),
                "expected": expected_summary.get(field),
                "deviation": 1,
            }
    for field in ("loss_first", "loss_last", "grad_norm", "prediction_mean"):
        deviation = _numeric_mismatch(
            actual_summary.get(field),
            expected_summary.get(field),
            config.equivalence_atol,
            config.equivalence_rtol,
        )
        if deviation is not None and not math.isclose(
            deviation,
            0.0,
            rel_tol=0.0,
            abs_tol=0.0,
        ):
            return {
                "job_id": actual["job_id"],
                "field": field,
                "actual": actual_summary.get(field),
                "expected": expected_summary.get(field),
                "deviation": deviation,
            }
    for field in ("prediction_checksum", "summary_checksum"):
        if actual_summary.get(field) != expected_summary.get(field):
            return {
                "job_id": actual["job_id"],
                "field": field,
                "actual": actual_summary.get(field),
                "expected": expected_summary.get(field),
                "deviation": "checksum_mismatch",
            }

    actual_losses = actual_summary.get("loss_sequence", [])
    expected_losses = expected_summary.get("loss_sequence", [])
    if len(actual_losses) != len(expected_losses):
        return {
            "job_id": actual["job_id"],
            "field": "loss_sequence.length",
            "actual": len(actual_losses),
            "expected": len(expected_losses),
            "deviation": abs(len(actual_losses) - len(expected_losses)),
        }
    for index, (actual_loss, expected_loss) in enumerate(
        zip(actual_losses, expected_losses)
    ):
        deviation = _numeric_mismatch(
            actual_loss,
            expected_loss,
            config.equivalence_atol,
            config.equivalence_rtol,
        )
        if deviation is not None and not math.isclose(
            deviation,
            0.0,
            rel_tol=0.0,
            abs_tol=0.0,
        ):
            return {
                "job_id": actual["job_id"],
                "field": f"loss_sequence[{index}]",
                "actual": actual_loss,
                "expected": expected_loss,
                "deviation": deviation,
            }
    if actual.get("record_checksum") != expected.get("record_checksum"):
        return {
            "job_id": actual["job_id"],
            "field": "record_checksum",
            "actual": actual.get("record_checksum"),
            "expected": expected.get("record_checksum"),
            "deviation": "checksum_mismatch",
        }
    return None


class Validator:
    def __init__(
        self,
        root: str | Path,
        manifest_path: str | Path | None = None,
        config_path: str | Path | None = None,
        *,
        snapshot_dir: str | Path | None = None,
    ) -> None:
        self.root = Path(root)
        self.manifest_path = Path(
            manifest_path or self.root / "data" / "jobs.jsonl"
        )
        self.config_path = Path(
            config_path or self.root / "configs" / "demo.json"
        )
        self.store = SnapshotStore(
            snapshot_dir or self.root / "data" / "runtime" / "snapshots"
        )

    def _required(self, category: str) -> SnapshotEnvelope:
        envelope = self.store.load(category)
        if envelope is None:
            raise BusinessError(
                E_SNAPSHOT_MISSING,
                "required snapshot is missing",
                stage="validation_snapshot",
                details={"snapshot_category": category},
            )
        return envelope

    def validate(self) -> dict[str, Any]:
        envelope_map = {
            category: self._required(category)
            for category in ("plan", "ledger", "results")
        }
        plan_env = envelope_map["plan"]
        ledger_env = envelope_map["ledger"]
        results_env = envelope_map["results"]
        versions = {
            category: envelope.version
            for category, envelope in envelope_map.items()
        }
        checksums = {
            category: envelope.checksum
            for category, envelope in envelope_map.items()
        }
        checks: list[dict[str, Any]] = []
        checks.append(
            {
                "name": "snapshot_versions",
                "ok": len(set(versions.values())) == 1,
                "details": versions,
            }
        )
        if len(set(versions.values())) != 1:
            return {
                "ok": False,
                "business_code": E_SNAPSHOT_VERSION_CONFLICT,
                "message": "plan, ledger and results versions differ",
                "first_inconsistency": None,
                "checks": checks,
                "snapshot_versions": versions,
                "snapshot_checksums": checksums,
            }

        plan = plan_env.payload
        ledger = ledger_env.payload
        result_payload = results_env.payload
        stage_version = plan_env.version

        cross_checks = [
            {
                "name": "plan_stage_version",
                "ok": plan.get("stage_version") == stage_version,
            },
            {
                "name": "ledger_stage_version",
                "ok": ledger.get("stage_version") == stage_version,
            },
            {
                "name": "results_stage_version",
                "ok": result_payload.get("stage_version") == stage_version,
            },
            {
                "name": "ledger_plan_checksum",
                "ok": ledger.get("plan_checksum") == plan_env.checksum,
            },
            {
                "name": "results_plan_checksum",
                "ok": result_payload.get("plan_checksum") == plan_env.checksum,
            },
            {
                "name": "results_ledger_checksum",
                "ok": result_payload.get("ledger_checksum") == ledger_env.checksum,
            },
        ]
        checks.extend(cross_checks)
        if not all(check["ok"] for check in cross_checks):
            return {
                "ok": False,
                "business_code": E_SNAPSHOT_VERSION_CONFLICT,
                "message": "snapshot cross references are inconsistent",
                "first_inconsistency": None,
                "checks": checks,
                "snapshot_versions": versions,
                "snapshot_checksums": checksums,
            }

        admitted_decisions = {
            decision["job_id"]: decision
            for decision in plan["decisions"]
            if decision["decision"] == "admitted"
        }
        admitted_ids = set(admitted_decisions)
        ledger_entries = {
            entry["job_id"]: entry for entry in ledger.get("entries", [])
        }
        checks.append(
            {
                "name": "ledger_admission_set",
                "ok": set(ledger_entries) == admitted_ids,
                "details": {
                    "ledger": sorted(ledger_entries),
                    "admitted": sorted(admitted_ids),
                },
            }
        )

        allocations_match = set(ledger_entries) == admitted_ids
        if allocations_match:
            for job_id, decision in admitted_decisions.items():
                entry = ledger_entries[job_id]
                effective = decision["effective_estimate"]
                if (
                    entry["effective_compute_units"] != effective["compute_units"]
                    or entry["effective_memory_bytes"] != effective["memory_bytes"]
                ):
                    allocations_match = False
                    break
        checks.append(
            {
                "name": "ledger_allocations",
                "ok": allocations_match,
            }
        )

        expected_compute = sum(
            decision["effective_estimate"]["compute_units"]
            for decision in admitted_decisions.values()
        )
        expected_memory = sum(
            decision["effective_estimate"]["memory_bytes"]
            for decision in admitted_decisions.values()
        )
        totals = ledger.get("totals", {})
        budget_match = (
            totals.get("effective_compute_units") == expected_compute
            and totals.get("effective_memory_bytes") == expected_memory
            and plan["totals"].get("compute_units_used") == expected_compute
            and plan["totals"].get("memory_bytes_used") == expected_memory
        )
        checks.append(
            {
                "name": "ledger_budget_totals",
                "ok": budget_match,
                "details": {
                    "expected_compute": expected_compute,
                    "expected_memory": expected_memory,
                    "ledger_totals": totals,
                    "plan_totals": plan.get("totals"),
                },
            }
        )

        results = result_payload.get("results", [])
        result_ids = [result["job_id"] for result in results]
        results_one_to_one = (
            len(result_ids) == len(set(result_ids))
            and set(result_ids) == admitted_ids
        )
        checks.append(
            {
                "name": "results_admission_set",
                "ok": results_one_to_one,
                "details": {
                    "result_count": len(result_ids),
                    "unique_result_count": len(set(result_ids)),
                    "admitted_count": len(admitted_ids),
                },
            }
        )
        if not results_one_to_one:
            return {
                "ok": False,
                "business_code": E_INCONSISTENT,
                "message": "result set is not one-to-one with admission set",
                "first_inconsistency": {
                    "job_id": None,
                    "field": "result_set",
                    "actual": sorted(result_ids),
                    "expected": sorted(admitted_ids),
                    "deviation": len(set(result_ids) ^ admitted_ids),
                },
                "checks": checks,
                "snapshot_versions": versions,
                "snapshot_checksums": checksums,
            }

        config = load_config(self.config_path)
        manifest = load_manifest(self.manifest_path)
        manifest_ids = {task.job_id for task in manifest}
        checks.append(
            {
                "name": "manifest_membership",
                "ok": admitted_ids <= manifest_ids,
                "details": {
                    "manifest_count": len(manifest_ids),
                    "admitted_count": len(admitted_ids),
                },
            }
        )
        if not checks[-1]["ok"]:
            return {
                "ok": False,
                "business_code": E_INCONSISTENT,
                "message": "admitted jobs are not all present in the manifest",
                "first_inconsistency": {
                    "job_id": None,
                    "field": "manifest_membership",
                    "actual": sorted(admitted_ids - manifest_ids),
                    "expected": [],
                    "deviation": len(admitted_ids - manifest_ids),
                },
                "checks": checks,
                "snapshot_versions": versions,
                "snapshot_checksums": checksums,
            }

        result_by_id = {result["job_id"]: result for result in results}
        first_inconsistency: dict[str, Any] | None = None
        jobs_verified = 0
        degraded_jobs_verified = 0
        errors_attributed = 0
        degraded_checks: list[dict[str, Any]] = []

        for job_id in sorted(admitted_decisions):
            decision = admitted_decisions[job_id]
            effective_task = Task.from_dict(decision["effective_task"])
            baseline = run_job(effective_task, config)
            actual = result_by_id[job_id]
            mismatch = _first_summary_inconsistency(actual, baseline, config)
            if mismatch is not None and first_inconsistency is None:
                first_inconsistency = mismatch
            if actual["status"] == "ok":
                jobs_verified += 1
            else:
                errors_attributed += 1
            if decision["degraded"]:
                degraded_jobs_verified += 1
                degraded_checks.append(
                    {
                        "job_id": job_id,
                        "degraded_fields": decision["degraded_fields"],
                        "baseline_status": baseline["status"],
                        "matches": mismatch is None,
                    }
                )

        checks.append(
            {
                "name": "serial_baseline_equivalence",
                "ok": first_inconsistency is None,
                "details": {
                    "jobs_verified": jobs_verified,
                    "errors_attributed": errors_attributed,
                    "degraded_jobs_verified": degraded_jobs_verified,
                    "tolerance": {
                        "atol": config.equivalence_atol,
                        "rtol": config.equivalence_rtol,
                    },
                },
            }
        )
        failed_checks = [check for check in checks if not check["ok"]]
        if first_inconsistency is None and failed_checks:
            first_inconsistency = {
                "job_id": None,
                "field": failed_checks[0]["name"],
                "actual": failed_checks[0].get("details"),
                "expected": "ok",
                "deviation": 1,
            }
        output_ok = not failed_checks and first_inconsistency is None
        output = {
            "ok": output_ok,
            "business_code": E_INCONSISTENT if not output_ok else None,
            "message": (
                "serial baseline and snapshot checks passed"
                if output_ok
                else (
                    "parallel result differs from serial baseline"
                    if first_inconsistency and first_inconsistency.get("job_id")
                    else "one or more validation checks failed"
                )
            ),
            "first_inconsistency": first_inconsistency,
            "checks": checks,
            "snapshot_versions": versions,
            "snapshot_checksums": checksums,
            "snapshot_payload_checksums": {
                "plan": content_checksum(plan),
                "ledger": content_checksum(ledger),
                "results": content_checksum(result_payload),
            },
            "jobs_verified": jobs_verified,
            "degraded_jobs_verified": degraded_jobs_verified,
            "errors_attributed": errors_attributed,
            "degraded_checks": degraded_checks,
            "admitted_count": len(admitted_ids),
            "rejected_count": len(plan.get("rejected_job_ids", [])),
            "parallel_group_count": len(plan.get("parallel_groups", [])),
        }
        return output
