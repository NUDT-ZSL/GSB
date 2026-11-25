from __future__ import annotations

from pathlib import Path
from typing import Any

from .cache import build_cache_key
from .engine import evaluate_job
from .errors import OpVerifyError
from .service import VerificationService
from .snapshots import verify_envelope


def _inconsistent(message: str, first_difference: dict[str, Any]) -> OpVerifyError:
    return OpVerifyError(
        "E_INCONSISTENT",
        message,
        field=str(first_difference.get("path", "$")),
        details={"first_difference": first_difference},
    )


def _load_current_snapshot(service: VerificationService, snapshot_type: str) -> dict[str, Any]:
    try:
        current = service.snapshots.load_current(snapshot_type)
    except OpVerifyError as exc:
        if exc.code == "E_INCONSISTENT":
            raise _inconsistent(
                "snapshot checksum mismatch",
                {
                    "path": f"$.snapshots.{snapshot_type}.checksum",
                    "reason": "stored checksum does not match canonical content",
                    "details": exc.details,
                },
            ) from exc
        raise
    if current is None:
        raise _inconsistent(
            "current snapshot is missing",
            {"path": f"$.snapshots.{snapshot_type}", "expected": "object", "actual": None},
        )
    return current


def run_self_check(root: str | Path, base_dir: str | Path | None = None) -> dict[str, Any]:
    service = VerificationService(root)
    version = service.ledger.current_version()
    if version < 1:
        raise _inconsistent(
            "ledger has no committed version",
            {"path": "$.ledger.version", "expected": ">=1", "actual": version},
        )

    data_dir = base_dir or service.ledger.data_dir()
    record = service.ledger.version_record(version)

    for snapshot_type in ("ledger", "outputs", "parameters"):
        current = _load_current_snapshot(service, snapshot_type)
        if not verify_envelope(current):
            raise _inconsistent(
                "snapshot checksum mismatch",
                {"path": f"$.snapshots.{snapshot_type}.checksum"},
            )
        if int(current["version"]) != version:
            raise _inconsistent(
                "snapshot version does not match ledger version",
                {
                    "path": f"$.snapshots.{snapshot_type}.version",
                    "expected": version,
                    "actual": int(current["version"]),
                },
            )
        try:
            versioned = service.snapshots.load_version(snapshot_type, version)
        except OpVerifyError as exc:
            if exc.code == "E_INCONSISTENT":
                raise _inconsistent(
                    "versioned snapshot checksum mismatch",
                    {"path": f"$.snapshots.{snapshot_type}-v{version}.checksum"},
                ) from exc
            raise
        if versioned["checksum"] != current["checksum"]:
            raise _inconsistent(
                "current snapshot differs from atomic versioned snapshot",
                {"path": f"$.snapshots.{snapshot_type}.checksum"},
            )

    if record["ledger_checksum"] != service.snapshots.load_current("ledger")["checksum"]:
        raise _inconsistent(
            "ledger database checksum does not match ledger snapshot",
            {"path": "$.ledger_versions.ledger_checksum", "expected": record["ledger_checksum"]},
        )
    if record["outputs_checksum"] != service.snapshots.load_current("outputs")["checksum"]:
        raise _inconsistent(
            "outputs checksum does not match snapshot",
            {"path": "$.ledger_versions.outputs_checksum"},
        )
    if record["parameters_checksum"] != service.snapshots.load_current("parameters")["checksum"]:
        raise _inconsistent(
            "parameter checksum does not match snapshot",
            {"path": "$.ledger_versions.parameters_checksum"},
        )

    checkpoint = service.checkpoints.load_latest()
    if checkpoint is None:
        raise _inconsistent("commit checkpoint missing", {"path": "$.checkpoints.latest"})
    if int(checkpoint["target_version"]) != version or checkpoint["stage"] != "commit":
        raise _inconsistent(
            "stage version does not match ledger version",
            {
                "path": "$.checkpoints.latest",
                "expected": {"stage": "commit", "target_version": version},
                "actual": {
                    "stage": checkpoint.get("stage"),
                    "target_version": checkpoint.get("target_version"),
                },
            },
        )

    rows = service.ledger.rows_for_version(version)
    for row in rows:
        job_id = row["job_id"]
        if not service.ledger.verify_row_checksum(row):
            raise _inconsistent(
                "ledger row checksum mismatch",
                {"path": f"$.ledger.jobs.{job_id}.row_checksum", "job_id": job_id},
            )

        task = row["task"]
        try:
            result = evaluate_job(task, base_dir=data_dir)
        except OpVerifyError as exc:
            raise _inconsistent(
                "ledger job cannot be independently recomputed",
                {
                    "path": f"$.ledger.jobs.{job_id}.output_digest",
                    "job_id": job_id,
                    "cause_code": exc.code,
                    "cause_field": exc.field,
                    "cause_details": exc.details,
                },
            ) from exc
        output_digest, _ = service.cache.put_array_artifact(result["output"])
        if output_digest != row["output_digest"]:
            raise _inconsistent(
                "ledger output digest cannot be reproduced from recorded parameters",
                {
                    "path": f"$.ledger.jobs.{job_id}.output_digest",
                    "job_id": job_id,
                    "expected": row["output_digest"],
                    "actual": output_digest,
                    "first_numeric_difference": result["comparison"],
                },
            )
        reference_digest, _ = service.cache.put_array_artifact(result["reference"])
        if reference_digest != row["reference_digest"]:
            raise _inconsistent(
                "reference digest cannot be reproduced",
                {
                    "path": f"$.ledger.jobs.{job_id}.reference_digest",
                    "job_id": job_id,
                    "expected": row["reference_digest"],
                    "actual": reference_digest,
                },
            )

        upstream = row.get("upstream_artifacts", [])
        cache_key, _ = build_cache_key(
            task,
            input_hash=row["input_hash"],
            parameter_hash=row["parameter_digest"],
            upstream_artifacts=upstream,
            weight_hash=row.get("weight_hash"),
        )
        if cache_key != row["cache_key"]:
            raise _inconsistent(
                "cache key does not include the recorded content and revision fields",
                {
                    "path": f"$.ledger.jobs.{job_id}.cache_key",
                    "job_id": job_id,
                    "expected": row["cache_key"],
                    "actual": cache_key,
                },
            )
        entry = service.cache.get_entry(cache_key)
        if entry is None or entry.get("output_digest") != row["output_digest"]:
            raise _inconsistent(
                "content-addressed cache entry is missing or bound to another output",
                {"path": f"$.cas.keys.{cache_key}", "job_id": job_id},
            )

    plan = record["change_plan"]
    affected = set(plan.get("affected_jobs", []))
    recompute_jobs = set(plan.get("recompute_jobs", []))
    reused_jobs = set(plan.get("reused_jobs", []))
    uncovered = sorted(affected - (recompute_jobs | reused_jobs))
    if uncovered:
        raise _inconsistent(
            "recompute/reuse sets do not cover all affected jobs",
            {
                "path": "$.change_plan.affected_jobs",
                "uncovered_jobs": uncovered,
                "affected_jobs": sorted(affected),
                "recompute_jobs": sorted(recompute_jobs),
                "reused_jobs": sorted(reused_jobs),
            },
        )

    return {
        "ok": True,
        "code": "OK",
        "ledger_version": version,
        "checked_jobs": len(rows),
        "affected_jobs": sorted(affected),
        "recompute_jobs": sorted(recompute_jobs),
        "reused_jobs": sorted(reused_jobs),
        "first_difference": None,
        "checks": [
            "snapshot checksums and versions",
            "stage/ledger version alignment",
            "independent float64 recomputation",
            "cache key and CAS binding",
            "affected-set coverage",
        ],
    }


def self_check_response(root: str | Path, base_dir: str | Path | None = None) -> dict[str, Any]:
    try:
        return run_self_check(root, base_dir=base_dir)
    except OpVerifyError as exc:
        return exc.to_dict()
