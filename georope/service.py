"""Business orchestration for encoding, checking, and incremental recomputation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .compare import cross_combination_comparison, cross_version_comparison
from .continuity import continuity_report
from .errors import BusinessError, invalid_input
from .incremental import incremental_recompute
from .manual import run_manual_cases
from .normalize import normalize_request
from .reference import verify_against_reference
from .rope import encode_request_fields


def _public_field(field: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in field.items()
        if not key.startswith("_")
    }


def encode_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    request = normalize_request(payload)
    field_result = encode_request_fields(request, include_tensors=True)
    production_encodings = field_result.pop("_block_encodings")
    fields = field_result["fields"]
    records = field_result["rotation_angle_records"]
    boundary_reports = [
        continuity_report(request, combination)
        for combination in request.combinations
    ]
    cross_version = cross_version_comparison(request)
    cross_combination = cross_combination_comparison(request)
    reference_result = verify_against_reference(
        request, production_encodings, request.tolerance
    )
    global_boundary_continuous = all(
        report["seams_with_global_discontinuity"] == 0
        for report in boundary_reports
    )
    passed = (
        reference_result["consistent"]
        and cross_version["consistent"]
        and cross_combination["consistent"]
        and global_boundary_continuous
    )
    return {
        "ok": True,
        "request_id": request.request_id,
        "normalized_summary": request.summary(),
        "band_combinations": [
            combination.summary() for combination in request.combinations
        ],
        "encoded_fields": fields,
        "rotation_angle_records": records,
        "boundary_continuity_report": boundary_reports,
        "boundary_continuity_summary": {
            "combination_count": len(boundary_reports),
            "seams_checked": sum(
                report["seams_checked"] for report in boundary_reports
            ),
            "seams_with_local_phase_breaks": sum(
                report["seams_with_local_phase_breaks"]
                for report in boundary_reports
            ),
            "seams_with_global_discontinuity": sum(
                report["seams_with_global_discontinuity"]
                for report in boundary_reports
            ),
            "global_continuous": global_boundary_continuous,
        },
        "cross_combination_differences": cross_combination,
        "cross_version_differences": cross_version,
        "reference_verification": reference_result,
        "verification_summary": {
            "passed": passed,
            "reference_consistent": reference_result["consistent"],
            "cross_version_consistent": cross_version["consistent"],
            "cross_combination_consistent": cross_combination["consistent"],
            "global_boundary_continuous": global_boundary_continuous,
            "local_block_phase_breaks_detected_by_reference": sum(
                report["seams_with_local_phase_breaks"]
                for report in boundary_reports
            ),
        },
    }


def check_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    encoding_result = encode_request(payload)
    manual_cases = run_manual_cases()
    boundary_passed = all(
        report["seams_with_global_discontinuity"] == 0
        for report in encoding_result["boundary_continuity_report"]
    )
    reference_passed = encoding_result["reference_verification"]["consistent"]
    cross_version_passed = encoding_result["cross_version_differences"]["consistent"]
    cross_combination_passed = encoding_result[
        "cross_combination_differences"
    ]["consistent"]
    passed = bool(
        boundary_passed
        and reference_passed
        and cross_version_passed
        and cross_combination_passed
        and manual_cases["passed"]
    )
    return {
        "ok": True,
        "request_id": encoding_result["request_id"],
        "passed": passed,
        "tolerance": encoding_result["normalized_summary"]["tolerance"],
        "reference_verification": encoding_result["reference_verification"],
        "boundary_verification": {
            "report": encoding_result["boundary_continuity_report"],
            "summary": encoding_result["boundary_continuity_summary"],
            "passed": boundary_passed,
        },
        "cross_version_verification": encoding_result[
            "cross_version_differences"
        ],
        "cross_combination_verification": encoding_result[
            "cross_combination_differences"
        ],
        "manual_cases": manual_cases,
        "normalized_summary": encoding_result["normalized_summary"],
        "checks": {
            "reference_consistency": reference_passed,
            "cross_version_consistency": cross_version_passed,
            "cross_combination_consistency": cross_combination_passed,
            "global_seam_continuity": boundary_passed,
            "manual_cases": manual_cases["passed"],
        },
    }


def incremental_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise invalid_input("request body must be a JSON object")
    old_payload = payload.get("old") or payload.get("previous") or payload.get("before")
    new_payload = payload.get("new") or payload.get("current") or payload.get("after")
    if not isinstance(old_payload, Mapping) or not isinstance(new_payload, Mapping):
        incremental_payload = payload.get("incremental")
        if isinstance(incremental_payload, Mapping):
            old_payload = (
                incremental_payload.get("old")
                or incremental_payload.get("previous")
                or incremental_payload.get("before")
            )
            new_payload = (
                incremental_payload.get("new")
                or incremental_payload.get("current")
                or incremental_payload.get("after")
            )
    if not isinstance(old_payload, Mapping) or not isinstance(new_payload, Mapping):
        raise BusinessError(
            "INCREMENTAL_PAYLOAD_INVALID",
            "incremental request must contain old/previous and new/current objects",
            [{"fields": ["old|previous", "new|current"]}],
            400,
        )
    old_request = normalize_request(old_payload)
    new_request = normalize_request(new_payload)
    report = incremental_recompute(old_request, new_request)
    return {
        "ok": True,
        "old_normalized_summary": old_request.summary(),
        "new_normalized_summary": new_request.summary(),
        "incremental_recomputation": report,
    }
