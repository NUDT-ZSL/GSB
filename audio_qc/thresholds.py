"""Threshold readers, normalized timeline, and v1/v2 migration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .codes import BusinessCode
from .errors import QCError
from .hashing import canonical_hash

BAND_NAMES = ("d1", "d2", "d3", "a3")
DEFAULT_EXPECTED_FRACTIONS = {
    "d1": 0.16,
    "d2": 0.22,
    "d3": 0.28,
    "a3": 0.34,
}
SEGMENT_DEFAULT_FRACTIONS = {
    "silence": {"d1": 0.25, "d2": 0.25, "d3": 0.25, "a3": 0.25},
    "voiced": DEFAULT_EXPECTED_FRACTIONS,
    "default": DEFAULT_EXPECTED_FRACTIONS,
}


def _corrupt(message: str, **details: Any) -> QCError:
    return QCError(BusinessCode.E_FORMAT_CORRUPT, message, details)


def _as_float(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise _corrupt(f"{field} must be numeric", field=field, value=value)
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError as exc:
            raise _corrupt(f"{field} is not parseable as a number", field=field, value=value) from exc
    else:
        raise _corrupt(f"{field} must be numeric", field=field, value=value)
    if number != number or number in (float("inf"), float("-inf")):
        raise _corrupt(f"{field} must be finite", field=field, value=value)
    return number


def _as_int(value: Any, field: str) -> int:
    number = _as_float(value, field)
    if not number.is_integer() or number < 0:
        raise _corrupt(f"{field} must be a non-negative integer sample index", field=field, value=value)
    return int(number)


def _major_version(value: Any) -> int:
    if isinstance(value, bool):
        raise QCError(BusinessCode.E_VERSION_UNSUPPORTED, "threshold version is unsupported", {"version": value})
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip().lstrip("vV")
        try:
            return int(text.split(".", 1)[0])
        except ValueError:
            try:
                return int(float(text))
            except ValueError as exc:
                raise QCError(
                    BusinessCode.E_VERSION_UNSUPPORTED,
                    "threshold version is unsupported",
                    {"version": value},
                ) from exc
    raise QCError(BusinessCode.E_VERSION_UNSUPPORTED, "threshold version is unsupported", {"version": value})


def _interval(raw: dict[str, Any], sample_rate: int, index: int) -> tuple[int, int]:
    start = raw.get("start_sample", raw.get("start", raw.get("start_index")))
    end = raw.get("end_sample", raw.get("end", raw.get("end_index")))
    if start is None and "start_ms" in raw:
        start = _as_float(raw["start_ms"], f"rows[{index}].start_ms") * sample_rate / 1000.0
    if end is None and "end_ms" in raw:
        end = _as_float(raw["end_ms"], f"rows[{index}].end_ms") * sample_rate / 1000.0
    if start is None:
        raise _corrupt("threshold window is missing start_sample", index=index, field="start_sample")
    if end is None:
        if "duration_samples" in raw:
            end = _as_int(start, f"rows[{index}].start_sample") + _as_int(
                raw["duration_samples"], f"rows[{index}].duration_samples"
            )
        elif "duration_ms" in raw:
            start_int = _as_int(start, f"rows[{index}].start_sample")
            end = start_int + int(round(_as_float(raw["duration_ms"], f"rows[{index}].duration_ms") * sample_rate / 1000.0))
        else:
            raise _corrupt("threshold window is missing end_sample", index=index, field="end_sample")
    start_int = _as_int(start, f"rows[{index}].start_sample")
    end_int = _as_int(end, f"rows[{index}].end_sample")
    if end_int <= start_int:
        raise _corrupt("threshold window end must be greater than start", index=index, start=start_int, end=end_int)
    return start_int, end_int


def _check_timeline(windows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(windows, key=lambda item: (item["start_sample"], item["end_sample"], item["segment_id"]))
    previous: dict[str, Any] | None = None
    for window in ordered:
        if previous is not None and window["start_sample"] < previous["end_sample"]:
            raise QCError(
                BusinessCode.E_TIMELINE_OVERLAP,
                "threshold windows overlap on the sample timeline",
                {
                    "first_overlap": {
                        "left_segment_id": previous["segment_id"],
                        "right_segment_id": window["segment_id"],
                        "left_end_sample": previous["end_sample"],
                        "right_start_sample": window["start_sample"],
                    }
                },
            )
        previous = window
    return ordered


def _normalize_band(name: str, raw: dict[str, Any] | None, fallback_tolerance: float) -> dict[str, Any]:
    raw = raw or {}
    expected = raw.get("expected_fraction", raw.get("expected", DEFAULT_EXPECTED_FRACTIONS[name]))
    tolerance = raw.get("fraction_tolerance", raw.get("tolerance", fallback_tolerance))
    band = {
        "band": name,
        "expected_fraction": _as_float(expected, f"subbands.{name}.expected_fraction"),
        "fraction_tolerance": _as_float(tolerance, f"subbands.{name}.fraction_tolerance"),
        "weight": _as_float(raw.get("weight", 1.0), f"subbands.{name}.weight"),
    }
    if "rms_limit" in raw or "limit" in raw or "threshold" in raw:
        band["rms_limit"] = _as_float(
            raw.get("rms_limit", raw.get("limit", raw.get("threshold"))),
            f"subbands.{name}.rms_limit",
        )
    return band


def _bands_from_v1_row(row: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], list[str], list[str]]:
    diagnostics: list[str] = []
    explicit_expected: list[str] = []
    fallback_tolerance = _as_float(row.get("subband_tolerance", row.get("distribution_tolerance", 0.12)), "subband_tolerance")
    bands: dict[str, dict[str, Any]] = {}
    for name in BAND_NAMES:
        raw_band: dict[str, Any] = {}
        for source_key, target in (
            (f"{name}_expected", "expected_fraction"),
            (f"{name}_tolerance", "fraction_tolerance"),
            (f"{name}_weight", "weight"),
            (f"{name}_limit", "rms_limit"),
            (f"{name}_rms_limit", "rms_limit"),
            (f"{name}_threshold", "rms_limit"),
        ):
            if source_key in row:
                raw_band[target] = row[source_key]
                if target == "expected_fraction":
                    explicit_expected.append(name)
        if f"{name}_limit" not in row and f"{name}_rms_limit" not in row and f"{name}_threshold" not in row:
            if "subband_limit" in row:
                raw_band["rms_limit"] = row["subband_limit"]
                diagnostics.append(name)
        bands[name] = _normalize_band(name, raw_band, fallback_tolerance)
    return bands, diagnostics, explicit_expected


def _base_document(payload: dict[str, Any], source_version: str) -> dict[str, Any]:
    sample_rate = int(_as_float(payload.get("sample_rate", 16_000), "sample_rate"))
    return {
        "schema": "audio-qc-normalized-thresholds/v1",
        "source_version": source_version,
        "curve_id": str(payload.get("curve_id", payload.get("id", "default-thresholds"))),
        "revision": str(payload.get("revision", payload.get("curve_revision", source_version))),
        "sample_rate": sample_rate,
        "frame_length": int(_as_float(payload.get("frame_length", 320), "frame_length")),
        "frame_hop": int(_as_float(payload.get("frame_hop", 160), "frame_hop")),
    }


def parse_v1(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise _corrupt("v1 threshold document must be a JSON object")
    rows = payload.get("rows", payload.get("table", payload.get("flat_thresholds", payload.get("windows"))))
    if rows is None and isinstance(payload.get("thresholds"), list):
        rows = payload["thresholds"]
    if not isinstance(rows, list) or not rows:
        raise _corrupt("v1 flat threshold table must contain a non-empty rows/table list")

    document = _base_document(payload, "v1")
    windows: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise _corrupt("v1 threshold row must be an object", index=index)
        start, end = _interval(row, document["sample_rate"], index)
        if "energy_gate_db" not in row and "gate_db" not in row:
            raise _corrupt("threshold row is missing energy_gate_db", index=index, field="energy_gate_db")
        if "target_rms_db" not in row and "target_db" not in row:
            raise _corrupt("threshold row is missing target_rms_db", index=index, field="target_rms_db")
        if "energy_tolerance_db" not in row and "tolerance_db" not in row:
            raise _corrupt("threshold row is missing energy_tolerance_db", index=index, field="energy_tolerance_db")
        bands, inherited_flat_limit, explicit_expected = _bands_from_v1_row(row)
        segment_class = str(row.get("segment_class", row.get("kind", "default")))
        window = {
            "segment_id": str(row.get("segment_id", row.get("id", f"v1-row-{index:03d}"))),
            "segment_class": segment_class,
            "start_sample": start,
            "end_sample": end,
            "energy_gate_db": _as_float(row.get("energy_gate_db", row.get("gate_db")), f"rows[{index}].energy_gate_db"),
            "target_rms_db": _as_float(row.get("target_rms_db", row.get("target_db")), f"rows[{index}].target_rms_db"),
            "energy_tolerance_db": _as_float(
                row.get("energy_tolerance_db", row.get("tolerance_db")),
                f"rows[{index}].energy_tolerance_db",
            ),
            "distribution_tolerance": _as_float(
                row.get("distribution_tolerance", row.get("subband_distribution_tolerance", 0.24)),
                f"rows[{index}].distribution_tolerance",
            ),
            "subbands": bands,
            "flat_subband_limit_inherited": inherited_flat_limit,
            "explicit_expected_bands": explicit_expected,
        }
        windows.append(window)
    document["windows"] = _check_timeline(windows)
    document["threshold_hash"] = canonical_hash(_hashable_curve(document))
    return document


def parse_v2(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise _corrupt("v2 threshold document must be a JSON object")
    shards = payload.get("shards", payload.get("segments", payload.get("windows")))
    if not isinstance(shards, list) or not shards:
        raise _corrupt("v2 threshold document must contain a non-empty shards list")

    document = _base_document(payload, "v2")
    windows: list[dict[str, Any]] = []
    for index, shard in enumerate(shards):
        if not isinstance(shard, dict):
            raise _corrupt("v2 threshold shard must be an object", index=index)
        interval = shard.get("range", shard.get("timeline", shard))
        if not isinstance(interval, dict):
            interval = shard
        start, end = _interval(interval, document["sample_rate"], index)
        energy = shard.get("energy", shard.get("energy_thresholds", {}))
        if not isinstance(energy, dict):
            raise _corrupt("v2 shard energy must be an object", index=index)
        if "gate_db" not in energy and "energy_gate_db" not in energy:
            raise _corrupt("v2 shard is missing energy.gate_db", index=index, field="energy.gate_db")
        if "target_db" not in energy and "target_rms_db" not in energy:
            raise _corrupt("v2 shard is missing energy.target_db", index=index, field="energy.target_db")
        if "tolerance_db" not in energy and "energy_tolerance_db" not in energy:
            raise _corrupt("v2 shard is missing energy.tolerance_db", index=index, field="energy.tolerance_db")

        wavelet = shard.get("wavelet", shard.get("subbands", {}))
        if not isinstance(wavelet, dict):
            raise _corrupt("v2 shard wavelet must be an object", index=index)
        nested_bands = wavelet.get("bands", wavelet)
        if not isinstance(nested_bands, dict):
            raise _corrupt("v2 wavelet.bands must be an object", index=index)
        fallback_tolerance = _as_float(
            energy.get("distribution_tolerance", wavelet.get("distribution_tolerance", 0.24)),
            f"shards[{index}].distribution_tolerance",
        )
        bands = {
            name: _normalize_band(name, nested_bands.get(name, {}), fallback_tolerance)
            for name in BAND_NAMES
        }
        window = {
            "segment_id": str(shard.get("segment_id", shard.get("id", f"v2-shard-{index:03d}"))),
            "segment_class": str(shard.get("segment_class", shard.get("kind", "default"))),
            "start_sample": start,
            "end_sample": end,
            "energy_gate_db": _as_float(energy.get("gate_db", energy.get("energy_gate_db")), f"shards[{index}].energy.gate_db"),
            "target_rms_db": _as_float(energy.get("target_db", energy.get("target_rms_db")), f"shards[{index}].energy.target_db"),
            "energy_tolerance_db": _as_float(
                energy.get("tolerance_db", energy.get("energy_tolerance_db")),
                f"shards[{index}].energy.tolerance_db",
            ),
            "distribution_tolerance": fallback_tolerance,
            "subbands": bands,
        }
        if "adjudication" in shard:
            window["adjudication"] = shard["adjudication"]
        windows.append(window)
    document["windows"] = _check_timeline(windows)
    document["threshold_hash"] = canonical_hash(_hashable_curve(document))
    return document


def read_thresholds(payload: dict[str, Any] | str | Path) -> dict[str, Any]:
    if isinstance(payload, (str, Path)):
        path = Path(payload)
        try:
            import json

            payload = json.loads(path.read_text(encoding="utf-8"))
        except QCError:
            raise
        except Exception as exc:
            raise _corrupt("threshold JSON cannot be parsed", path=str(path)) from exc
    if not isinstance(payload, dict):
        raise _corrupt("threshold document must be a JSON object")

    version = payload.get("version", payload.get("format_version", payload.get("schema_version")))
    if version is not None:
        major = _major_version(version)
        if major == 1:
            return parse_v1(payload)
        if major == 2:
            return parse_v2(payload)
        raise QCError(
            BusinessCode.E_VERSION_UNSUPPORTED,
            "threshold major version is not supported",
            {"version": version, "supported": [1, 2]},
        )

    if "shards" in payload or ("schema" in payload and "v2" in str(payload.get("schema"))):
        return parse_v2(payload)
    if "rows" in payload or "table" in payload or "flat_thresholds" in payload:
        return parse_v1(payload)
    raise _corrupt("threshold document has neither a supported version nor a recognizable field set")


def _hashable_curve(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": document["schema"],
        "curve_id": document["curve_id"],
        "revision": document["revision"],
        "sample_rate": document["sample_rate"],
        "frame_length": document["frame_length"],
        "frame_hop": document["frame_hop"],
        "windows": document["windows"],
    }


def curve_hash(document: dict[str, Any]) -> str:
    return canonical_hash(_hashable_curve(document))


def find_window(document: dict[str, Any], start_sample: int, end_sample: int) -> dict[str, Any]:
    best: dict[str, Any] | None = None
    best_overlap = 0
    for window in document["windows"]:
        overlap = max(0, min(end_sample, window["end_sample"]) - max(start_sample, window["start_sample"]))
        if overlap > best_overlap:
            best = window
            best_overlap = overlap
    if best is None or best_overlap == 0:
        raise _corrupt(
            "envelope block is outside threshold timeline",
            start_sample=start_sample,
            end_sample=end_sample,
        )
    return best


def gate_active(block: dict[str, Any], window: dict[str, Any]) -> bool:
    return bool(block["energy"]["rms_db"] >= window["energy_gate_db"])


def migrate_v1_to_v2(document: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if document.get("source_version") != "v1":
        raise QCError(BusinessCode.E_FORMAT_CORRUPT, "migrate_v1_to_v2 requires a parsed v1 document")
    completed: list[dict[str, Any]] = []
    shards: list[dict[str, Any]] = []
    for window_index, window in enumerate(document["windows"]):
        class_defaults = SEGMENT_DEFAULT_FRACTIONS.get(window["segment_class"], SEGMENT_DEFAULT_FRACTIONS["default"])
        bands: dict[str, Any] = {}
        for band_index, name in enumerate(BAND_NAMES):
            source_band = window["subbands"][name]
            expected = source_band["expected_fraction"]
            if name not in window.get("explicit_expected_bands", []):
                expected = class_defaults[name]
                completed.append(
                    {
                        "path": f"shards[{window_index}].wavelet.bands.{name}.expected_fraction",
                        "source": f"segment_default:{window['segment_class']}",
                        "value": expected,
                    }
                )
            else:
                completed.append(
                    {
                        "path": f"shards[{window_index}].wavelet.bands.{name}.expected_fraction",
                        "source": f"v1:{name}_expected",
                        "value": expected,
                    }
                )
            tolerance = source_band["fraction_tolerance"]
            completed.append(
                {
                    "path": f"shards[{window_index}].wavelet.bands.{name}.fraction_tolerance",
                    "source": "v1:subband_tolerance" if tolerance == 0.12 else "v1:flat-column-or-global",
                    "value": tolerance,
                }
            )
            weight = source_band["weight"]
            if weight == 1.0:
                completed.append(
                    {
                        "path": f"shards[{window_index}].wavelet.bands.{name}.weight",
                        "source": "application_default:weight=1.0",
                        "value": 1.0,
                    }
                )
            band_payload: dict[str, Any] = {
                "expected_fraction": expected,
                "fraction_tolerance": tolerance,
                "weight": weight,
            }
            if "rms_limit" in source_band:
                band_payload["rms_limit"] = source_band["rms_limit"]
                completed.append(
                    {
                        "path": f"shards[{window_index}].wavelet.bands.{name}.rms_limit",
                        "source": f"v1:{name}_limit-or-subband_limit",
                        "value": source_band["rms_limit"],
                    }
                )
            bands[name] = band_payload
        shard = {
            "segment_id": window["segment_id"],
            "segment_class": window["segment_class"],
            "range": {
                "start_sample": window["start_sample"],
                "end_sample": window["end_sample"],
            },
            "energy": {
                "gate_db": window["energy_gate_db"],
                "target_db": window["target_rms_db"],
                "tolerance_db": window["energy_tolerance_db"],
                "distribution_tolerance": window["distribution_tolerance"],
            },
            "wavelet": {
                "family": "haar",
                "levels": 3,
                "bands": bands,
            },
        }
        shards.append(shard)
    payload = {
        "schema": "audio-qc-sharded-thresholds/v2",
        "version": 2,
        "curve_id": document["curve_id"],
        "revision": f"{document['revision']}-migrated-v2",
        "sample_rate": document["sample_rate"],
        "frame_length": document["frame_length"],
        "frame_hop": document["frame_hop"],
        "shards": shards,
    }
    report = {
        "direction": "v1_to_v2",
        "completed": completed,
        "discarded": [],
        "flattened": [],
        "output_hash": canonical_hash(payload),
    }
    return payload, report


def migrate_v2_to_v1(document: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if document.get("source_version") != "v2":
        raise QCError(BusinessCode.E_FORMAT_CORRUPT, "migrate_v2_to_v1 requires a parsed v2 document")
    rows: list[dict[str, Any]] = []
    flattened: list[dict[str, Any]] = []
    discarded: list[dict[str, Any]] = []
    for window_index, window in enumerate(document["windows"]):
        row: dict[str, Any] = {
            "segment_id": window["segment_id"],
            "segment_class": window["segment_class"],
            "start_sample": window["start_sample"],
            "end_sample": window["end_sample"],
            "energy_gate_db": window["energy_gate_db"],
            "target_rms_db": window["target_rms_db"],
            "energy_tolerance_db": window["energy_tolerance_db"],
            "distribution_tolerance": window["distribution_tolerance"],
        }
        absolute_limits: list[float] = []
        for name in BAND_NAMES:
            band = window["subbands"][name]
            row[f"{name}_expected"] = band["expected_fraction"]
            row[f"{name}_tolerance"] = band["fraction_tolerance"]
            row[f"{name}_weight"] = band["weight"]
            flattened.append(
                {
                    "path": f"shards[{window_index}].wavelet.bands.{name}",
                    "flat_columns": [
                        f"{name}_expected",
                        f"{name}_tolerance",
                        f"{name}_weight",
                        f"{name}_limit",
                    ],
                    "rule": "direct_column_flatten",
                }
            )
            if "rms_limit" in band:
                row[f"{name}_limit"] = band["rms_limit"]
                absolute_limits.append(band["rms_limit"])
        if absolute_limits:
            row["subband_limit"] = max(absolute_limits)
            flattened.append(
                {
                    "path": f"shards[{window_index}].wavelet.bands.*.rms_limit",
                    "flat_column": "subband_limit",
                    "rule": "max_absolute_rms_across_bands",
                    "value": row["subband_limit"],
                }
            )
        if "adjudication" in window:
            discarded.append(
                {
                    "path": f"shards[{window_index}].adjudication",
                    "rule": "discard_nested_adjudication_profile",
                    "reason": "v1 flat table has no nested adjudication representation",
                    "value": window["adjudication"],
                }
            )
        rows.append(row)
    payload = {
        "schema": "audio-qc-flat-thresholds/v1",
        "version": 1,
        "curve_id": document["curve_id"],
        "revision": f"{document['revision']}-downgraded-v1",
        "sample_rate": document["sample_rate"],
        "frame_length": document["frame_length"],
        "frame_hop": document["frame_hop"],
        "table": rows,
    }
    report = {
        "direction": "v2_to_v1",
        "completed": [],
        "flattened": flattened,
        "discarded": discarded,
        "output_hash": canonical_hash(payload),
    }
    return payload, report


def gate_decisions_match(
    blocks: list[dict[str, Any]],
    left: dict[str, Any],
    right: dict[str, Any],
) -> dict[str, Any]:
    mismatches: list[dict[str, Any]] = []
    compared = 0
    for block in blocks:
        left_window = find_window(left, block["start_sample"], block["end_sample"])
        right_window = find_window(right, block["start_sample"], block["end_sample"])
        left_value = gate_active(block, left_window)
        right_value = gate_active(block, right_window)
        compared += 1
        if left_value != right_value:
            mismatches.append(
                {
                    "frame_index": block["frame_index"],
                    "start_sample": block["start_sample"],
                    "end_sample": block["end_sample"],
                    "left": left_value,
                    "right": right_value,
                }
            )
            break
    return {"consistent": not mismatches, "compared_windows": compared, "first_mismatch": mismatches[0] if mismatches else None}
