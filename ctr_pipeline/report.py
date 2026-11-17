"""Deterministic JSON report construction."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from .config import PipelineConfig
from .errors import FailureRecord
from .io_utils import clean_float, content_digest, write_json_atomic
from .models import InjectionSpec
from .snapshot import SnapshotStore


def _request_error_trace_summary(request_reports: Iterable[dict[str, Any]]) -> dict[str, Any]:
    stages = ("normalization", "quantization", "fusion", "truncation")
    accum: dict[str, list[float]] = {stage: [] for stage in stages}
    boundary_samples = 0
    rank_changed_requests = 0
    for request in request_reports:
        trace = request["error_trace"]
        for stage in stages:
            accum[stage].append(float(trace["stage_summary"][stage]["max_abs_delta"]))
        boundary_samples += len(trace["boundary_sensitive_samples"])
        if trace["ranking_changed"]:
            rank_changed_requests += 1
    summary: dict[str, Any] = {}
    for stage in stages:
        values = accum[stage]
        summary[stage] = {
            "max_abs_delta": clean_float(max(values, default=0.0)),
            "mean_request_max_abs_delta": (
                clean_float(sum(values) / len(values)) if values else 0.0
            ),
            "request_count": len(values),
        }
    summary["boundary_sensitive_sample_count"] = boundary_samples
    summary["rank_changed_request_count"] = rank_changed_requests
    return summary


def _consistency_payload(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "config_hash": report["config_hash"],
        "params_hash": report["params_hash"],
        "candidates_hash": report["candidates_hash"],
        "snapshot_version": report["snapshot_version"],
        "stage_versions": report["stage_versions"],
        "requests": report["requests"],
        "failure_list": report["failure_list"],
        "budget": report["budget"],
        "injections": report["injections"],
        "widths": report["widths"],
        "error_trace_summary": report["error_trace_summary"],
        "error_summary": report["error_summary"],
    }


def build_report(
    *,
    cfg: PipelineConfig,
    store: SnapshotStore,
    manifest: dict[str, Any],
    injections: list[InjectionSpec],
    status: str,
    effective_concurrency: int,
    input_paths: dict[str, str],
) -> dict[str, Any]:
    request_reports: list[dict[str, Any]] = []
    for request_id in sorted(manifest["request_records"]):
        request_report = store.load_request_report(manifest, request_id)
        if request_report is not None:
            request_reports.append(request_report)

    failure_list = [FailureRecord.from_dict(item) for item in manifest["failure_list"]]
    failure_dicts = [failure.to_dict() for failure in failure_list]
    by_code = Counter(item["code"] for item in failure_dicts)
    by_stage = Counter(item["stage"] for item in failure_dicts)

    injection_report = []
    for spec in sorted(injections, key=lambda item: item.injection_id):
        hit_data = manifest["injection_hits"].get(spec.injection_id, {})
        related_failures = [
            item
            for item in failure_dicts
            if spec.injection_id in item.get("injection_ids", [])
        ]
        injection_report.append(
            {
                "id": spec.injection_id,
                "request_id": spec.request_id,
                "stage": spec.stage,
                "fault_type": spec.fault_type,
                "candidate_id": spec.candidate_id,
                "hit": bool(hit_data.get("hit", False)),
                "hit_request_id": hit_data.get("request_id"),
                "error_codes": sorted({item["code"] for item in related_failures}),
                "exception_types": sorted(
                    {item["exception_type"] for item in related_failures}
                ),
            }
        )

    curve = [dict(item) for item in manifest["budget_curve"]]
    consumed = len(curve)
    remaining = max(0, cfg.global_budget - consumed)
    report: dict[str, Any] = {
        "schema_version": 1,
        "run_id": manifest["run_id"],
        "status": status,
        "config_hash": manifest["config_hash"],
        "params_hash": manifest["params_hash"],
        "candidates_hash": manifest["candidates_hash"],
        "snapshot_version": manifest["global_version"],
        "stage_versions": manifest["stage_versions"],
        "effective_concurrency": effective_concurrency,
        "widths": {"configured": cfg.width, "high": cfg.high_width},
        "config": {
            "normalization": cfg.normalization,
            "quantization_base": cfg.quantization_base,
            "quantization_levels": cfg.quantization_levels,
            "fusion_temperature": cfg.temperature,
            "score_clip_interval": [cfg.clip_min, cfg.clip_max],
            "score_decimals": cfg.score_decimals,
            "snapshot_commit_interval": cfg.snapshot_commit_interval,
        },
        "input_paths": dict(sorted(input_paths.items())),
        "requests": request_reports,
        "request_count": len(request_reports),
        "candidate_count": sum(int(item["candidate_count"]) for item in request_reports),
        "failure_list": failure_dicts,
        "failure_count": len(failure_dicts),
        "budget": {
            "global_budget": cfg.global_budget,
            "per_request_quota": cfg.per_request_quota,
            "consumed": consumed,
            "remaining": remaining,
            "exhausted": remaining == 0,
            "consumption_curve": curve,
        },
        "injections": injection_report,
        "error_summary": {
            "by_code": {key: by_code[key] for key in sorted(by_code)},
            "by_stage": {key: by_stage[key] for key in sorted(by_stage)},
            "failed_request_count": len(failure_dicts),
        },
        "error_trace_summary": _request_error_trace_summary(request_reports),
    }
    report["determinism_digest"] = content_digest(_consistency_payload(report))
    return report


def write_report(path: str | Path, report: dict[str, Any]) -> str:
    return write_json_atomic(Path(path), report)


def build_fatal_report(
    *,
    status: str,
    code: str,
    stage: str,
    exception_type: str,
    message: str,
    config_hash: str | None = None,
    extra_failures: list[FailureRecord] | None = None,
) -> dict[str, Any]:
    failure = FailureRecord(
        request_id="__global__",
        stage=stage,
        code=code,
        exception_type=exception_type,
        message=message,
        injection_ids=[],
    )
    failures = [failure] + (extra_failures or [])
    failure_dicts = [item.to_dict() for item in failures]
    by_code = Counter(item["code"] for item in failure_dicts)
    by_stage = Counter(item["stage"] for item in failure_dicts)
    report = {
        "schema_version": 1,
        "status": status,
        "config_hash": config_hash,
        "snapshot_version": None,
        "stage_versions": {},
        "requests": [],
        "failure_list": failure_dicts,
        "failure_count": len(failure_dicts),
        "budget": {
            "global_budget": None,
            "per_request_quota": None,
            "consumed": 0,
            "remaining": None,
            "exhausted": False,
            "consumption_curve": [],
        },
        "injections": [],
        "error_summary": {
            "by_code": {key: by_code[key] for key in sorted(by_code)},
            "by_stage": {key: by_stage[key] for key in sorted(by_stage)},
            "failed_request_count": len(failure_dicts),
        },
        "error_trace_summary": {},
    }
    report["determinism_digest"] = content_digest(
        {
            "status": status,
            "failure_list": failure_dicts,
            "config_hash": config_hash,
        }
    )
    return report
