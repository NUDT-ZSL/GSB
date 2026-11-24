"""Trace anomaly-injection manifest items into the final report."""

from __future__ import annotations

from typing import Any, Iterable

from .models import InputResult, UnifiedView


_KIND_ALIASES = {
    "invalid_bit_depth": "invalid_bit_depth",
    "bad_bit_depth": "invalid_bit_depth",
    "wrong_bit_depth": "invalid_bit_depth",
    "wrong_sample_rate_declaration": "wrong_sample_rate_declaration",
    "wrong_sample_rate": "wrong_sample_rate_declaration",
    "sample_rate_mismatch": "wrong_sample_rate_declaration",
    "corrupt_shard": "corrupt_shard",
    "corrupt_shard_snapshot": "corrupt_shard",
    "corrupt_fragment": "corrupt_shard",
}

_EXPECTED_CODE = {
    "invalid_bit_depth": "E_BIT_DEPTH",
    "wrong_sample_rate_declaration": "E_SR",
    "corrupt_shard": "E_DIGEST",
}


def normalize_anomalies(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        raw_kind = str(item.get("kind", "")).strip().lower()
        kind = _KIND_ALIASES.get(raw_kind, raw_kind)
        injection_id = str(
            item.get("injection_id", item.get("id", f"anomaly-{index + 1}"))
        )
        if kind == "corrupt_shard":
            target = item.get("target_input_id", item.get("input_id"))
        else:
            target = item.get(
                "target_input_id", item.get("input_id", item.get("target"))
            )
        shard_id = item.get("shard_id")
        if kind not in _EXPECTED_CODE:
            normalized.append({
                **item,
                "injection_id": injection_id,
                "kind": kind,
                "target_input_id": target,
                "shard_id": shard_id,
                "expected_code": None,
                "valid_kind": False,
            })
        else:
            normalized.append({
                **item,
                "injection_id": injection_id,
                "kind": kind,
                "target_input_id": target,
                "shard_id": shard_id,
                "expected_code": _EXPECTED_CODE[kind],
                "valid_kind": True,
            })
    return normalized


def trace_anomalies(
    anomalies: list[dict[str, Any]],
    results: list[InputResult],
    view: UnifiedView,
) -> list[dict[str, Any]]:
    by_input = {result.input_id: result for result in results}
    traced: list[dict[str, Any]] = []
    for item in anomalies:
        kind = item["kind"]
        target = item.get("target_input_id")
        expected = item.get("expected_code")
        observed: list[str] = []
        locations: list[dict[str, Any]] = []
        status = "not_detected"
        if kind in ("invalid_bit_depth", "wrong_sample_rate_declaration"):
            result = by_input.get(str(target))
            if result is not None:
                observed = list(result.codes)
                if expected in result.codes:
                    status = "detected"
                    locations.append({
                        "kind": "input",
                        "order": result.order,
                        "input_id": result.input_id,
                        "path": result.path,
                    })
                    if item["injection_id"] not in result.anomaly_ids:
                        result.anomaly_ids.append(item["injection_id"])
        elif kind == "corrupt_shard":
            shard_id = item.get("shard_id")
            target_text = str(item.get("target", ""))
            for issue in view.issues:
                if issue.get("code") != "E_DIGEST":
                    continue
                source = issue.get("source") or {}
                source_shard = source.get("shard_id")
                source_path = str(source.get("path", ""))
                matches = (
                    (shard_id is not None and str(source_shard) == str(shard_id))
                    or (not shard_id and target_text and target_text in source_path)
                )
                if matches:
                    status = "detected"
                    observed.append("E_DIGEST")
                    locations.append({
                        "kind": "snapshot-shard",
                        "shard_id": source_shard,
                        "path": source_path,
                    })
        traced.append({
            "injection_id": item["injection_id"],
            "kind": kind,
            "target_input_id": target,
            "shard_id": item.get("shard_id"),
            "expected_code": expected,
            "status": status,
            "observed_codes": sorted(set(observed)),
            "locations": locations,
            "valid_kind": item.get("valid_kind", True),
        })
    return traced
