"""Threshold alignment, drift adjudication, hashes, and replay."""

from __future__ import annotations

from typing import Any

import numpy as np

from .codes import BusinessCode
from .errors import QCError
from .hashing import canonical_hash, hash_with_checksum, verify_checksum
from .thresholds import BAND_NAMES, find_window

FIXED_SCALE = 1_000_000


def block_hash(block: dict[str, Any]) -> str:
    return canonical_hash(block)


def window_hash(window: dict[str, Any]) -> str:
    return canonical_hash(window)


def align_blocks(blocks: list[dict[str, Any]], threshold_doc: dict[str, Any]) -> list[dict[str, Any]]:
    alignment: list[dict[str, Any]] = []
    for block in blocks:
        window = find_window(threshold_doc, block["start_sample"], block["end_sample"])
        overlap = max(
            0,
            min(block["end_sample"], window["end_sample"])
            - max(block["start_sample"], window["start_sample"]),
        )
        alignment.append(
            {
                "frame_index": block["frame_index"],
                "start_sample": block["start_sample"],
                "end_sample": block["end_sample"],
                "sample_interval": [block["start_sample"], block["end_sample"]],
                "block_hash": block_hash(block),
                "threshold_curve_id": threshold_doc["curve_id"],
                "threshold_version": threshold_doc["revision"],
                "threshold_source_version": threshold_doc["source_version"],
                "threshold_hash": threshold_doc["threshold_hash"],
                "segment_id": window["segment_id"],
                "window_hash": window_hash(window),
                "overlap_samples": int(overlap),
                "aligned_time_base": "sample_index@16000Hz",
            }
        )
    return alignment


def _observed_view(block: dict[str, Any]) -> dict[str, Any]:
    bands = {item["band"]: float(item["energy_fraction"]) for item in block["subbands"]}
    return {
        "rms_db": float(block["energy"]["rms_db"]),
        "energy": float(block["energy"]["energy"]),
        "rms": float(block["energy"]["rms"]),
        "band_fractions": {name: float(bands.get(name, 0.0)) for name in BAND_NAMES},
        "band_rms": {
            item["band"]: float(item["rms"])
            for item in block["subbands"]
            if item["band"] in BAND_NAMES
        },
    }


def _threshold_view(window: dict[str, Any]) -> dict[str, Any]:
    return {
        "energy_gate_db": float(window["energy_gate_db"]),
        "target_rms_db": float(window["target_rms_db"]),
        "energy_tolerance_db": float(window["energy_tolerance_db"]),
        "distribution_tolerance": float(window["distribution_tolerance"]),
        "subbands": {
            name: {
                "expected_fraction": float(window["subbands"][name]["expected_fraction"]),
                "fraction_tolerance": float(window["subbands"][name]["fraction_tolerance"]),
                "weight": float(window["subbands"][name].get("weight", 1.0)),
                **(
                    {"rms_limit": float(window["subbands"][name]["rms_limit"])}
                    if "rms_limit" in window["subbands"][name]
                    else {}
                ),
            }
            for name in BAND_NAMES
        },
    }


def _classify(energy_drift: bool, subband_drift: bool, distribution_drift: bool) -> str:
    if energy_drift:
        return BusinessCode.DRIFT_ENERGY
    if subband_drift:
        return BusinessCode.DRIFT_SUBBAND
    if distribution_drift:
        return BusinessCode.DRIFT_DISTRIBUTION
    return BusinessCode.NO_DRIFT


def adjudicate_float(observed: dict[str, Any], threshold: dict[str, Any]) -> dict[str, Any]:
    energy_error = abs(observed["rms_db"] - threshold["target_rms_db"])
    energy_drift = energy_error > threshold["energy_tolerance_db"]

    band_results: list[dict[str, Any]] = []
    distribution_distance = 0.0
    for name in BAND_NAMES:
        spec = threshold["subbands"][name]
        observed_fraction = observed["band_fractions"][name]
        delta = abs(observed_fraction - spec["expected_fraction"])
        distribution_distance += delta
        normalized_contribution = delta / spec["fraction_tolerance"] if spec["fraction_tolerance"] > 0 else float("inf")
        violation = delta > spec["fraction_tolerance"]
        rms_limit = spec.get("rms_limit")
        rms_violation = rms_limit is not None and observed["band_rms"].get(name, 0.0) > rms_limit
        band_results.append(
            {
                "band": name,
                "observed_fraction": observed_fraction,
                "expected_fraction": spec["expected_fraction"],
                "absolute_delta": delta,
                "normalized_contribution": normalized_contribution,
                "fraction_violation": bool(violation),
                "rms_violation": bool(rms_violation),
                "weight": spec["weight"],
                "weighted_contribution": normalized_contribution * spec["weight"],
            }
        )

    distribution_drift = distribution_distance > threshold["distribution_tolerance"]
    subband_drift = any(item["fraction_violation"] or item["rms_violation"] for item in band_results)
    dominant = max(band_results, key=lambda item: item["weighted_contribution"])
    dominant_excess = max(
        band_results,
        key=lambda item: max(
            0.0,
            observed["band_fractions"][item["band"]]
            - threshold["subbands"][item["band"]]["expected_fraction"],
        )
        / threshold["subbands"][item["band"]]["fraction_tolerance"],
    )
    code = _classify(energy_drift, subband_drift, distribution_drift)
    return {
        "mode": "float64",
        "business_code": code,
        "is_drift": code != BusinessCode.NO_DRIFT,
        "energy_error_db": energy_error,
        "energy_drift": bool(energy_drift),
        "distribution_distance": distribution_distance,
        "distribution_drift": bool(distribution_drift),
        "subband_drift": bool(subband_drift),
        "band_results": band_results,
        "dominant_subband": dominant["band"],
        "dominant_contribution": dominant["weighted_contribution"],
        "dominant_excess_subband": dominant_excess["band"],
    }


def _fixed(value: float) -> int:
    return int(np.round(np.float64(value) * FIXED_SCALE).astype(np.int64))


def adjudicate_fixed(observed: dict[str, Any], threshold: dict[str, Any]) -> dict[str, Any]:
    rms_db_q = _fixed(observed["rms_db"])
    target_q = _fixed(threshold["target_rms_db"])
    energy_tol_q = _fixed(threshold["energy_tolerance_db"])
    energy_error_q = abs(rms_db_q - target_q)
    energy_drift = energy_error_q > energy_tol_q

    dist_tol_q = _fixed(threshold["distribution_tolerance"])
    distance_q = 0
    band_results: list[dict[str, Any]] = []
    for name in BAND_NAMES:
        spec = threshold["subbands"][name]
        observed_q = _fixed(observed["band_fractions"][name])
        expected_q = _fixed(spec["expected_fraction"])
        tolerance_q = max(1, _fixed(spec["fraction_tolerance"]))
        delta_q = abs(observed_q - expected_q)
        distance_q += delta_q
        violation = delta_q > tolerance_q
        rms_limit = spec.get("rms_limit")
        rms_violation = rms_limit is not None and _fixed(observed["band_rms"].get(name, 0.0)) > _fixed(rms_limit)
        weight_q = max(1, _fixed(spec["weight"]))
        band_results.append(
            {
                "band": name,
                "absolute_delta_q": delta_q,
                "tolerance_q": tolerance_q,
                "normalized_contribution_q_x_scale": int(delta_q * FIXED_SCALE // tolerance_q),
                "weighted_contribution_q_x_scale": int(delta_q * weight_q * FIXED_SCALE // tolerance_q),
                "fraction_violation": bool(violation),
                "rms_violation": bool(rms_violation),
            }
        )

    distribution_drift = distance_q > dist_tol_q
    subband_drift = any(item["fraction_violation"] or item["rms_violation"] for item in band_results)
    dominant = max(band_results, key=lambda item: item["weighted_contribution_q_x_scale"])
    code = _classify(energy_drift, subband_drift, distribution_drift)
    return {
        "mode": "fixed_i64_s1e6",
        "business_code": code,
        "is_drift": code != BusinessCode.NO_DRIFT,
        "energy_error_q": energy_error_q,
        "energy_tolerance_q": energy_tol_q,
        "energy_drift": bool(energy_drift),
        "distribution_distance_q": distance_q,
        "distribution_tolerance_q": dist_tol_q,
        "distribution_drift": bool(distribution_drift),
        "subband_drift": bool(subband_drift),
        "band_results": band_results,
        "dominant_subband": dominant["band"],
    }


def _dual_consistency(float_verdict: dict[str, Any], fixed_verdict: dict[str, Any]) -> dict[str, Any]:
    consistent = float_verdict["business_code"] == fixed_verdict["business_code"]
    return {
        "consistent": consistent,
        "float_code": float_verdict["business_code"],
        "fixed_code": fixed_verdict["business_code"],
        "fixed_scale": FIXED_SCALE,
        "first_mismatch": None
        if consistent
        else {
            "field": "business_code",
            "float64": float_verdict["business_code"],
            "fixed_i64": fixed_verdict["business_code"],
        },
    }


def build_drift_record(
    block: dict[str, Any],
    alignment: dict[str, Any],
    window: dict[str, Any],
    *,
    enforce_dual: bool = True,
) -> dict[str, Any]:
    observed = _observed_view(block)
    threshold = _threshold_view(window)
    float_verdict = adjudicate_float(observed, threshold)
    fixed_verdict = adjudicate_fixed(observed, threshold)
    dual = _dual_consistency(float_verdict, fixed_verdict)
    if enforce_dual and not dual["consistent"]:
        raise QCError(
            BusinessCode.E_INCONSISTENT,
            "float64 and fixed-point drift classifications differ",
            {"frame_index": block["frame_index"], "dual": dual},
        )

    replay_input = {
        "frame_index": block["frame_index"],
        "start_sample": block["start_sample"],
        "end_sample": block["end_sample"],
        "observed": observed,
        "threshold": threshold,
    }
    precomputed_hash = canonical_hash(replay_input)
    record = {
        "frame_index": block["frame_index"],
        "envelope_block_id": f"block-{int(block['frame_index']):06d}",
        "start_sample": block["start_sample"],
        "end_sample": block["end_sample"],
        "sample_interval": [block["start_sample"], block["end_sample"]],
        "pcm_sample_range": {"start": block["start_sample"], "end": block["end_sample"]},
        "segment_id": alignment["segment_id"],
        "energy_segment_id": alignment["segment_id"],
        "threshold_segment_id": alignment["segment_id"],
        "threshold_version": alignment["threshold_version"],
        "threshold_source_version": alignment["threshold_source_version"],
        "threshold_hash": alignment["threshold_hash"],
        "window_hash": alignment["window_hash"],
        "block_hash": alignment["block_hash"],
        "alignment_hash": canonical_hash(alignment),
        "precomputed_hash": precomputed_hash,
        "replay_input": replay_input,
        "dual_numeric_consistency": dual,
        "float_verdict": float_verdict,
        "fixed_verdict": fixed_verdict,
        "business_code": float_verdict["business_code"],
        "is_drift": float_verdict["is_drift"],
        "dominant_subband": float_verdict["dominant_subband"],
        "dominant_excess_subband": float_verdict["dominant_excess_subband"],
        "trace": {
            "pcm_samples": f"{block['start_sample']}:{block['end_sample']}",
            "segment_id": alignment["segment_id"],
            "threshold_version": alignment["threshold_version"],
            "block_hash12": alignment["block_hash"][:12],
        },
    }
    return hash_with_checksum(record)


def detect_drift(
    blocks: list[dict[str, Any]],
    threshold_doc: dict[str, Any],
    alignment: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    alignment = alignment or align_blocks(blocks, threshold_doc)
    by_index = {item["frame_index"]: item for item in alignment}
    records: list[dict[str, Any]] = []
    drift_events: list[dict[str, Any]] = []
    dual_failures: list[dict[str, Any]] = []
    for block in blocks:
        aligned = by_index[block["frame_index"]]
        window = find_window(threshold_doc, block["start_sample"], block["end_sample"])
        record = build_drift_record(block, aligned, window, enforce_dual=False)
        if not record["dual_numeric_consistency"]["consistent"]:
            dual_failures.append(
                {
                    "frame_index": block["frame_index"],
                    "start_sample": block["start_sample"],
                    "end_sample": block["end_sample"],
                    "first_mismatch": record["dual_numeric_consistency"]["first_mismatch"],
                }
            )
        records.append(record)
        if record["is_drift"]:
            drift_events.append(record)

    dual_consistent = not dual_failures
    code = BusinessCode.OK if dual_consistent else BusinessCode.E_INCONSISTENT
    return {
        "code": code,
        "dual_consistent": dual_consistent,
        "dual_failures": dual_failures,
        "records": records,
        "events": drift_events,
        "event_count": len(drift_events),
        "block_count": len(records),
        "drift_block_count": len(drift_events),
        "drift_sample_ranges": [event["sample_interval"] for event in drift_events],
    }


def replay_event(record: dict[str, Any]) -> dict[str, Any]:
    if not verify_checksum(record):
        raise QCError(BusinessCode.E_HASH_MISMATCH, "drift record checksum verification failed")
    replay_input = record["replay_input"]
    expected_hash = record["precomputed_hash"]
    actual_hash = canonical_hash(replay_input)
    if actual_hash != expected_hash:
        raise QCError(
            BusinessCode.E_HASH_MISMATCH,
            "precomputed replay hash does not match record input",
            {"expected": expected_hash, "actual": actual_hash},
        )
    float_verdict = adjudicate_float(replay_input["observed"], replay_input["threshold"])
    fixed_verdict = adjudicate_fixed(replay_input["observed"], replay_input["threshold"])
    dual = _dual_consistency(float_verdict, fixed_verdict)
    interval = [replay_input["start_sample"], replay_input["end_sample"]]
    ok = (
        float_verdict["business_code"] == record["business_code"]
        and interval == record["sample_interval"]
        and dual["consistent"]
    )
    if not ok:
        raise QCError(
            BusinessCode.E_INCONSISTENT,
            "event replay does not reproduce the recorded category or interval",
            {
                "frame_index": record.get("frame_index"),
                "recorded_code": record.get("business_code"),
                "replayed_code": float_verdict["business_code"],
                "recorded_interval": record.get("sample_interval"),
                "replayed_interval": interval,
            },
        )
    return {
        "ok": True,
        "business_code": float_verdict["business_code"],
        "sample_interval": interval,
        "dominant_subband": float_verdict["dominant_subband"],
        "dual": dual,
    }


def replay_report(report: dict[str, Any]) -> dict[str, Any]:
    results = [replay_event(event) for event in report.get("events", [])]
    return {"replayed_events": len(results), "results": results, "ok": True}
