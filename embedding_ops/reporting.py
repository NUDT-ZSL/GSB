"""Deterministic report assembly for the local CLI demo."""

from __future__ import annotations

import platform
import traceback
from dataclasses import replace
from typing import Any, Iterable

import numpy as np

import embedding_ops
from .config import NumericsConfig
from .diagnostics import run_error_injection_suite
from .errors import BusinessError
from .models import JobSpec
from .pipeline import run_job
from .snapshots import SnapshotStore, create_snapshot


def _error_job_record(
    *,
    job_id: str,
    precision_tier: str,
    error: BusinessError,
) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "status": "error",
        "precision_tier": precision_tier,
        "business_codes": [error.code],
        "degraded": True,
        "error": error.to_dict(),
    }


def _capture_job(
    spec: JobSpec,
    config: NumericsConfig,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        return run_job(spec, config), None
    except BusinessError as exc:
        return (
            _error_job_record(
                job_id=spec.job_id,
                precision_tier=spec.table_dtype,
                error=exc,
            ),
            None,
        )
    except Exception as exc:  # noqa: BLE001 - explicitly reported, not hidden
        return (
            {
                "job_id": spec.job_id,
                "status": "unattributed_error",
                "precision_tier": spec.table_dtype,
                "business_codes": [],
                "degraded": True,
                "error": {
                    "code": None,
                    "stage": "unattributed",
                    "job_id": spec.job_id,
                    "message": str(exc),
                },
            },
            {
                "job_id": spec.job_id,
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(),
            },
        )


def _precision_switches(
    base_spec: JobSpec,
    config: NumericsConfig,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    unattributed: list[dict[str, Any]] = []
    for tier in ("float64", "float32", "qint16", "qint8"):
        quant_scale = 0.001 if tier == "qint16" else 0.03
        switch_spec = replace(
            base_spec,
            job_id=f"switch-{tier}",
            table_dtype=tier,
            quant_scale=quant_scale,
        )
        record, unexpected = _capture_job(switch_spec, config)
        if record is not None:
            if record["status"] == "ok":
                records.append(
                    {
                        "job_id": switch_spec.job_id,
                        "precision_tier": tier,
                        "source_job_id": base_spec.job_id,
                        "status": "ok",
                        "final_max_abs_deviation": record["spectrum"][
                            "final_max_abs_deviation"
                        ],
                        "dominant_source": record["spectrum"][
                            "dominant_source"
                        ],
                        "within_tolerance": record["spectrum"][
                            "within_tolerance"
                        ],
                        "degraded": record["degraded"],
                        "self_check_passed": record["self_check"]["passed"],
                        "output_digest": record["output_digest"],
                        "tolerance": record["spectrum"]["tolerance"],
                    }
                )
            else:
                records.append(record)
        if unexpected is not None:
            unattributed.append(unexpected)
    return records, unattributed


def _spectrum_stats(
    jobs: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    by_tier: dict[str, dict[str, Any]] = {}
    job_stats: list[dict[str, Any]] = []
    for job in jobs:
        if job.get("status") != "ok":
            continue
        tier = job["precision_tier"]
        spectrum = job["spectrum"]
        stage_max_abs = {
            stage["stage"]: stage["max_abs_deviation"]
            for stage in spectrum["stages"]
        }
        job_stats.append(
            {
                "job_id": job["job_id"],
                "precision_tier": tier,
                "stage_max_abs_deviation": stage_max_abs,
                "final_max_abs_deviation": spectrum[
                    "final_max_abs_deviation"
                ],
                "dominant_source": spectrum["dominant_source"],
                "within_tolerance": spectrum["within_tolerance"],
                "tolerance": spectrum["tolerance"],
            }
        )
        current = by_tier.get(tier)
        value = float(spectrum["final_max_abs_deviation"])
        if current is None or value > current["max_final_abs_deviation"]:
            by_tier[tier] = {
                "max_final_abs_deviation": value,
                "representative_job_id": job["job_id"],
                "dominant_source": spectrum["dominant_source"],
            }
    return {
        "metric": "final_max_abs_deviation",
        "by_tier": by_tier,
        "jobs": job_stats,
        "job_count": sum(1 for job in jobs if job.get("status") == "ok"),
    }


def _snapshot_demo(
    config: NumericsConfig,
    spectra: dict[str, Any],
    specs: list[JobSpec],
) -> dict[str, Any]:
    store = SnapshotStore()
    attempts: list[dict[str, Any]] = []
    q8 = next((spec for spec in specs if spec.table_dtype == "qint8"), None)
    quant_v1 = {
        "qint8": {
            "quant_scale": q8.quant_scale if q8 else 0.03,
            "quant_zero_point": q8.quant_zero_point if q8 else 0,
        },
        "legacy_tier": "qint4",
    }
    quant_v2 = {
        "qint8": {"quant_scale": 0.03, "quant_zero_point": 0},
        "qint16": {"quant_scale": 0.001, "quant_zero_point": 0},
    }
    config_v1 = create_snapshot(
        "config",
        1,
        {
            "numerics": config.to_dict(),
            "label": "v1",
            "legacy_only": True,
        },
    )
    quant_snapshot_v1 = create_snapshot("quant_params", 1, quant_v1)
    spectrum_v1 = create_snapshot(
        "spectrum_stats",
        1,
        {**spectra, "stats_label": "v1"},
    )
    for snapshot in (config_v1, quant_snapshot_v1, spectrum_v1):
        outcome = store.load(snapshot).to_dict()
        attempts.append({**outcome, "result": "accepted"})

    config_v2 = create_snapshot(
        "config",
        2,
        {
            "numerics": replace(
                config,
                accum_rounding_enabled=True,
                accum_rounding_step=1e-4,
            ).to_dict(),
            "label": "v2",
        },
    )
    quant_snapshot_v2 = create_snapshot("quant_params", 2, quant_v2)
    spectrum_v2 = create_snapshot(
        "spectrum_stats",
        2,
        {**spectra, "stats_label": "v2"},
    )
    for snapshot in (config_v2, quant_snapshot_v2, spectrum_v2):
        outcome = store.load(snapshot).to_dict()
        attempts.append({**outcome, "result": "accepted"})

    def capture_error(action: Any, label: str, version: int) -> None:
        try:
            action()
        except BusinessError as exc:
            attempts.append(
                {
                    "kind": label,
                    "version": version,
                    "result": "rejected",
                    **exc.to_dict(),
                }
            )

    capture_error(lambda: store.load(config_v1), "config", 1)
    corrupt = create_snapshot(
        "quant_params",
        3,
        {
            "qint8": {"quant_scale": 0.03, "quant_zero_point": 0},
            "qint16": {"quant_scale": 0.001, "quant_zero_point": 0},
        },
    ).to_dict()
    corrupt["content"]["qint8"]["quant_scale"] = 0.5
    capture_error(lambda: store.load(corrupt), "quant_params", 3)

    state = store.state()
    current_config = state["current"]["config"]
    current_quant = state["current"]["quant_params"]
    replacement_checks = {
        "config_legacy_key_removed": (
            "legacy_only" not in current_config["content"]
        ),
        "quant_legacy_key_removed": (
            "legacy_tier" not in current_quant["content"]
        ),
        "config_whole_content_replaced": (
            current_config["content"].get("label") == "v2"
        ),
        "rollback_preserved_version": (
            current_config["version"] == 2
        ),
        "corruption_preserved_version": (
            current_quant["version"] == 2
        ),
    }
    return {
        "current": state["current"],
        "successful_outcomes": state["outcomes"],
        "load_attempts": attempts,
        "replacement_checks": replacement_checks,
    }


def build_report(
    specs: list[JobSpec],
    config: NumericsConfig,
    *,
    duplicate_errors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not specs:
        raise ValueError("report requires at least one job")
    jobs: list[dict[str, Any]] = []
    unattributed: list[dict[str, Any]] = []
    for spec in specs:
        record, unexpected = _capture_job(spec, config)
        if record is not None:
            jobs.append(record)
        if unexpected is not None:
            unattributed.append(unexpected)
    for duplicate in duplicate_errors or []:
        jobs.append(
            {
                "job_id": duplicate["job_id"],
                "status": "error",
                "precision_tier": "unknown",
                "business_codes": [duplicate["code"]],
                "degraded": True,
                "error": duplicate,
            }
        )
    base_spec = next(
        (spec for spec in specs if spec.job_id == "emb-f64"),
        specs[0],
    )
    switches, switch_unattributed = _precision_switches(base_spec, config)
    unattributed.extend(switch_unattributed)
    injection_suite = run_error_injection_suite(base_spec, config)
    unattributed.extend(injection_suite["unattributed_exceptions"])
    spectra = _spectrum_stats(jobs)
    snapshots = _snapshot_demo(config, spectra, specs)

    code_counts: dict[str, int] = {}
    for job in jobs:
        for code in job.get("business_codes", []):
            code_counts[code] = code_counts.get(code, 0) + 1
    for record in injection_suite["records"]:
        for code in record.get("business_codes", []):
            code_counts[code] = code_counts.get(code, 0) + 1
    for attempt in snapshots["load_attempts"]:
        if attempt.get("result") == "rejected" and attempt.get("code"):
            code = attempt["code"]
            code_counts[code] = code_counts.get(code, 0) + 1
    successful_jobs = [job for job in jobs if job["status"] == "ok"]
    self_checks = [
        job["self_check"] for job in successful_jobs if "self_check" in job
    ]
    report = {
        "report_version": 1,
        "implementation": {
            "name": "embedding_ops",
            "version": embedding_ops.__version__,
            "python": platform.python_version(),
            "numpy": np.__version__,
        },
        "config": config.to_dict(),
        "jobs": jobs,
        "precision_switches": switches,
        "injections": injection_suite["records"],
        "snapshots": snapshots,
        "unattributed_exceptions": unattributed,
        "summary": {
            "total_jobs": len(jobs),
            "succeeded": len(successful_jobs),
            "errors": len(jobs) - len(successful_jobs),
            "degraded": sum(
                1 for job in successful_jobs if job["degraded"]
            ),
            "injections_raised": sum(
                1
                for record in injection_suite["records"]
                if record["result"] == "error_raised"
            ),
            "injections_total": len(injection_suite["records"]),
            "snapshot_load_attempts": len(snapshots["load_attempts"]),
            "snapshot_rejections": sum(
                1
                for attempt in snapshots["load_attempts"]
                if attempt.get("result") == "rejected"
            ),
            "self_checks_passed": sum(
                1 for check in self_checks if check["passed"]
            ),
            "self_checks_total": len(self_checks),
            "business_codes": dict(sorted(code_counts.items())),
            "unattributed_exceptions": len(unattributed),
        },
    }
    return report
