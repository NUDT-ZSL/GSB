"""Strict, lossless parsing and normalization for all offline inputs."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .constants import (
    CANONICAL_MAP_KINDS,
    CODE_ADDR_OVERFLOW,
    CODE_ENDPOINT,
    CODE_FIELD_TYPE,
    CODE_FLAG,
    CODE_JSON,
    CODE_MAP_KIND,
    CODE_POLICY,
    CODE_PROT,
    CODE_REGION_DUPLICATE,
    CODE_SEQ,
    CODE_SEQ_DUPLICATE,
    CODE_TIME,
    CODE_ADDR_RANGE,
)
from .precision import ProtectionParseError, parse_protection


DEFAULT_ADDRESS_LIMIT = 1 << 64
ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:{}\-]+$")


def rejection(
    source_category: str,
    business_code: str,
    message: str,
    *,
    record_ref: Any = None,
    record_index: Optional[int] = None,
    event_seq: Optional[int] = None,
    raw_excerpt: Any = None,
) -> Dict[str, Any]:
    item: Dict[str, Any] = {
        "source_category": source_category,
        "business_code": business_code,
        "message": message,
    }
    if record_ref is not None:
        item["record_ref"] = record_ref
    if record_index is not None:
        item["record_index"] = record_index
        item["record_seq"] = record_index
    if event_seq is not None:
        item["event_seq"] = event_seq
    if raw_excerpt is not None:
        item["raw_excerpt"] = raw_excerpt
    return item


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def require_int(
    value: Any,
    field: str,
    rejections: List[Dict[str, Any]],
    source_category: str,
    record_ref: Any,
    *,
    minimum: Optional[int] = None,
) -> Optional[int]:
    if not is_int(value):
        rejections.append(
            rejection(
                source_category,
                CODE_FIELD_TYPE,
                f"{field} must be an integer",
                record_ref=record_ref,
                raw_excerpt=value,
            )
        )
        return None
    if minimum is not None and value < minimum:
        rejections.append(
            rejection(
                source_category,
                CODE_FIELD_TYPE,
                f"{field} must be >= {minimum}",
                record_ref=record_ref,
                raw_excerpt=value,
            )
        )
        return None
    return value


def parse_bool_flag(value: Any, field_name: str = "flag") -> bool:
    if isinstance(value, bool):
        return value
    if is_int(value) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in {
        "0",
        "1",
        "true",
        "false",
        "yes",
        "no",
        "y",
        "n",
    }:
        return value.strip().lower() in {"1", "true", "yes", "y"}
    raise ValueError(f"{field_name} must be a boolean or 0/1 marker")


def valid_identifier(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 160
        and not any(ch.isspace() for ch in value)
        and bool(ID_PATTERN.fullmatch(value))
    )


def load_json(path: Path, source_category: str, rejections: List[Dict[str, Any]]):
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        rejections.append(
            rejection(
                source_category,
                CODE_JSON,
                f"file not found: {path}",
                record_ref=str(path),
            )
        )
    except json.JSONDecodeError as exc:
        rejections.append(
            rejection(
                source_category,
                CODE_JSON,
                f"JSON parse failed: {exc.msg}",
                record_ref=str(path),
                record_index=exc.lineno,
            )
        )
    except OSError as exc:
        rejections.append(
            rejection(
                source_category,
                CODE_JSON,
                f"file read failed: {exc}",
                record_ref=str(path),
            )
        )
    return None


def parse_observation_clock(
    manifest: Optional[Dict[str, Any]],
    rejections: List[Dict[str, Any]],
) -> Dict[str, Any]:
    if not manifest:
        raise ValueError("sample manifest with observation_clock is required")
    clock = manifest.get("observation_clock", manifest)
    boot_ms = clock.get("boot_time_ms", manifest.get("boot_time_ms"))
    if not is_int(boot_ms) or boot_ms < 0:
        rejections.append(
            rejection(
                "sample_manifest",
                CODE_TIME,
                "observation_clock.boot_time_ms must be a non-negative integer",
                record_ref="manifest.json",
                raw_excerpt=boot_ms,
            )
        )
        raise ValueError("invalid observation clock")
    limit = manifest.get("address_space_limit", DEFAULT_ADDRESS_LIMIT)
    if not is_int(limit) or limit <= 0:
        limit = DEFAULT_ADDRESS_LIMIT
    return {
        "clock_id": clock.get("clock_id", "declared-sample-clock"),
        "boot_time_ms": boot_ms,
        "time_source": "sample_declaration",
        "uses_system_clock": False,
        "address_space_limit": limit,
    }


def parse_event_stream(
    path: Path,
    clock: Dict[str, Any],
    rejections: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    seen_seq = set()
    address_limit = int(clock.get("address_space_limit", DEFAULT_ADDRESS_LIMIT))

    try:
        handle = path.open("r", encoding="utf-8")
    except OSError as exc:
        rejections.append(
            rejection(
                "event_stream",
                CODE_JSON,
                f"cannot open event stream: {exc}",
                record_ref=str(path),
            )
        )
        return events

    with handle:
        for lineno, line in enumerate(handle, start=1):
            record_ref = f"{path.name}:{lineno}"
            text = line.strip()
            if not text:
                rejections.append(
                    rejection(
                        "event_stream",
                        CODE_JSON,
                        "empty JSONL record",
                        record_ref=record_ref,
                        record_index=lineno,
                    )
                )
                continue
            try:
                raw = json.loads(text)
            except json.JSONDecodeError as exc:
                rejections.append(
                    rejection(
                        "event_stream",
                        CODE_JSON,
                        f"JSON parse failed: {exc.msg}",
                        record_ref=record_ref,
                        record_index=lineno,
                        raw_excerpt=text[:200],
                    )
                )
                continue

            if not isinstance(raw, dict):
                rejections.append(
                    rejection(
                        "event_stream",
                        CODE_FIELD_TYPE,
                        "event record must be a JSON object",
                        record_ref=record_ref,
                        record_index=lineno,
                        raw_excerpt=text[:200],
                    )
                )
                continue

            seq = raw.get("event_seq")
            event_rejections_before = len(rejections)
            if not is_int(seq) or seq < 1:
                rejections.append(
                    rejection(
                        "event_stream",
                        CODE_SEQ,
                        "event_seq must be a positive integer",
                        record_ref=record_ref,
                        record_index=lineno,
                        raw_excerpt=seq,
                    )
                )
                seq = None
            elif seq in seen_seq:
                rejections.append(
                    rejection(
                        "event_stream",
                        CODE_SEQ_DUPLICATE,
                        "event_seq must be unique",
                        record_ref=record_ref,
                        record_index=lineno,
                        event_seq=seq,
                    )
                )

            endpoint_id = raw.get("endpoint_id")
            if not valid_identifier(endpoint_id):
                rejections.append(
                    rejection(
                        "event_stream",
                        CODE_ENDPOINT,
                        "endpoint_id must be a non-empty stable identifier",
                        record_ref=record_ref,
                        record_index=lineno,
                        event_seq=seq,
                        raw_excerpt=endpoint_id,
                    )
                )

            process_guid = raw.get("process_guid")
            region_id = raw.get("region_id")
            if not valid_identifier(process_guid):
                rejections.append(
                    rejection(
                        "event_stream",
                        CODE_FIELD_TYPE,
                        "process_guid must be a non-empty stable identifier",
                        record_ref=record_ref,
                        record_index=lineno,
                        event_seq=seq,
                    )
                )
            if not valid_identifier(region_id):
                rejections.append(
                    rejection(
                        "event_stream",
                        CODE_FIELD_TYPE,
                        "region_id must be a non-empty stable identifier",
                        record_ref=record_ref,
                        record_index=lineno,
                        event_seq=seq,
                    )
                )

            map_kind = raw.get("map_kind")
            if map_kind not in CANONICAL_MAP_KINDS:
                rejections.append(
                    rejection(
                        "event_stream",
                        CODE_MAP_KIND,
                        f"map_kind must be one of {CANONICAL_MAP_KINDS}",
                        record_ref=record_ref,
                        record_index=lineno,
                        event_seq=seq,
                        raw_excerpt=map_kind,
                    )
                )
                map_kind = None

            base_offset = raw.get("base_offset")
            region_size = raw.get("region_size")
            base_ok = is_int(base_offset) and base_offset >= 0
            size_ok = is_int(region_size) and region_size >= 0
            if not base_ok:
                rejections.append(
                    rejection(
                        "event_stream",
                        CODE_ADDR_RANGE,
                        "base_offset must be a non-negative integer",
                        record_ref=record_ref,
                        record_index=lineno,
                        event_seq=seq,
                        raw_excerpt=base_offset,
                    )
                )
            if not size_ok:
                rejections.append(
                    rejection(
                        "event_stream",
                        CODE_ADDR_RANGE,
                        "region_size must be a non-negative integer",
                        record_ref=record_ref,
                        record_index=lineno,
                        event_seq=seq,
                        raw_excerpt=region_size,
                    )
                )
            end_offset = None
            if base_ok and size_ok:
                end_offset = base_offset + region_size
                if base_offset > address_limit or end_offset > address_limit:
                    rejections.append(
                        rejection(
                            "event_stream",
                            CODE_ADDR_OVERFLOW,
                            "base_offset + region_size exceeds declared address space",
                            record_ref=record_ref,
                            record_index=lineno,
                            event_seq=seq,
                            raw_excerpt={
                                "base_offset": base_offset,
                                "region_size": region_size,
                                "end_offset": end_offset,
                                "address_limit": address_limit,
                            },
                        )
                    )

            prot_before = None
            prot_after = None
            try:
                prot_before = parse_protection(raw.get("prot_before"))
            except ProtectionParseError as exc:
                rejections.append(
                    rejection(
                        "event_stream",
                        CODE_PROT,
                        str(exc),
                        record_ref=record_ref,
                        record_index=lineno,
                        event_seq=seq,
                        raw_excerpt=raw.get("prot_before"),
                    )
                )
            try:
                prot_after = parse_protection(raw.get("prot_after"))
            except ProtectionParseError as exc:
                rejections.append(
                    rejection(
                        "event_stream",
                        CODE_PROT,
                        str(exc),
                        record_ref=record_ref,
                        record_index=lineno,
                        event_seq=seq,
                        raw_excerpt=raw.get("prot_after"),
                    )
                )

            observed_ms = raw.get("observed_ms")
            if not is_int(observed_ms) or observed_ms < 0:
                rejections.append(
                    rejection(
                        "event_stream",
                        CODE_TIME,
                        "observed_ms must be a non-negative integer",
                        record_ref=record_ref,
                        record_index=lineno,
                        event_seq=seq,
                        raw_excerpt=observed_ms,
                    )
                )
                observed_ms = None

            write_source = raw.get("write_source")
            if not isinstance(write_source, str) or not write_source.strip():
                rejections.append(
                    rejection(
                        "event_stream",
                        CODE_FIELD_TYPE,
                        "write_source must be a non-empty string",
                        record_ref=record_ref,
                        record_index=lineno,
                        event_seq=seq,
                        raw_excerpt=write_source,
                    )
                )
                write_source = None

            if len(rejections) != event_rejections_before or seq is None:
                continue

            seen_seq.add(seq)
            uncertainty = raw.get("precision_error_bound", 0)
            if not is_int(uncertainty) or uncertainty < 0:
                uncertainty = 0
            events.append(
                {
                    "event_seq": seq,
                    "endpoint_id": endpoint_id,
                    "process_guid": process_guid,
                    "region_id": region_id,
                    "map_kind": map_kind,
                    "base_offset": base_offset,
                    "region_size": region_size,
                    "end_offset": end_offset,
                    "prot_before": prot_before,
                    "prot_after": prot_after,
                    "write_source": write_source.strip(),
                    "observed_ms": observed_ms,
                    "sync_marker": raw.get("sync_marker"),
                    "precision_error_bound": uncertainty,
                    "source_line": lineno,
                    "record_ref": record_ref,
                }
            )

    events.sort(key=lambda item: item["event_seq"])
    return events


def parse_regions(
    path: Path,
    clock: Dict[str, Any],
    rejections: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    data = load_json(path, "region_declaration", rejections)
    if data is None:
        return []
    if isinstance(data, dict):
        raw_regions = data.get("regions", [])
        default_endpoint = data.get("endpoint_id")
    elif isinstance(data, list):
        raw_regions = data
        default_endpoint = None
    else:
        rejections.append(
            rejection(
                "region_declaration",
                CODE_FIELD_TYPE,
                "region declaration must be an object or array",
                record_ref=str(path),
            )
        )
        return []

    if not isinstance(raw_regions, list):
        rejections.append(
            rejection(
                "region_declaration",
                CODE_FIELD_TYPE,
                "regions must be an array",
                record_ref=str(path),
            )
        )
        return []

    regions: List[Dict[str, Any]] = []
    seen = set()
    address_limit = int(clock.get("address_space_limit", DEFAULT_ADDRESS_LIMIT))

    for index, raw in enumerate(raw_regions, start=1):
        ref = f"{path.name}#{index}"
        if not isinstance(raw, dict):
            rejections.append(
                rejection(
                    "region_declaration",
                    CODE_FIELD_TYPE,
                    "region record must be an object",
                    record_ref=ref,
                    record_index=index,
                )
            )
            continue

        endpoint_id = raw.get("endpoint_id", default_endpoint)
        process_guid = raw.get("process_guid")
        region_id = raw.get("region_id")
        generation = raw.get("region_generation", raw.get("generation", 0))
        module = raw.get("module", raw.get("module_name"))
        map_kind = raw.get("map_kind")
        local_errors = len(rejections)

        if not valid_identifier(endpoint_id):
            rejections.append(
                rejection(
                    "region_declaration",
                    CODE_ENDPOINT,
                    "endpoint_id must be a non-empty stable identifier",
                    record_ref=ref,
                    record_index=index,
                    raw_excerpt=endpoint_id,
                )
            )
        if not valid_identifier(process_guid):
            rejections.append(
                rejection(
                    "region_declaration",
                    CODE_FIELD_TYPE,
                    "process_guid must be a non-empty stable identifier",
                    record_ref=ref,
                    record_index=index,
                )
            )
        if not valid_identifier(region_id):
            rejections.append(
                rejection(
                    "region_declaration",
                    CODE_FIELD_TYPE,
                    "region_id must be a non-empty stable identifier",
                    record_ref=ref,
                    record_index=index,
                )
            )
        if not isinstance(module, str) or not module.strip():
            rejections.append(
                rejection(
                    "region_declaration",
                    CODE_FIELD_TYPE,
                    "module must be a non-empty string",
                    record_ref=ref,
                    record_index=index,
                )
            )
        if map_kind not in CANONICAL_MAP_KINDS:
            rejections.append(
                rejection(
                    "region_declaration",
                    CODE_MAP_KIND,
                    f"map_kind must be one of {CANONICAL_MAP_KINDS}",
                    record_ref=ref,
                    record_index=index,
                    raw_excerpt=map_kind,
                )
            )
            map_kind = None
        if not is_int(generation) or generation < 0:
            rejections.append(
                rejection(
                    "region_declaration",
                    CODE_FIELD_TYPE,
                    "region_generation must be a non-negative integer",
                    record_ref=ref,
                    record_index=index,
                    raw_excerpt=generation,
                )
            )
            generation = None

        base = raw.get("base_offset", raw.get("base"))
        size = raw.get("region_size", raw.get("size"))
        base_ok = is_int(base) and base >= 0
        size_ok = is_int(size) and size >= 0
        if not base_ok or not size_ok:
            rejections.append(
                rejection(
                    "region_declaration",
                    CODE_ADDR_RANGE,
                    "base_offset and region_size must be non-negative integers",
                    record_ref=ref,
                    record_index=index,
                )
            )
        end = base + size if base_ok and size_ok else None
        if end is not None and (base > address_limit or end > address_limit):
            rejections.append(
                rejection(
                    "region_declaration",
                    CODE_ADDR_OVERFLOW,
                    "region range exceeds declared address space",
                    record_ref=ref,
                    record_index=index,
                    raw_excerpt={"base_offset": base, "region_size": size},
                )
            )

        legal_range = raw.get("legal_range")
        legal_start = raw.get("legal_start")
        legal_end = raw.get("legal_end")
        if isinstance(legal_range, list) and len(legal_range) == 2:
            legal_start, legal_end = legal_range
        if legal_start is None:
            legal_start = base
        if legal_end is None:
            legal_end = end
        legal_ok = (
            is_int(legal_start)
            and is_int(legal_end)
            and legal_start >= 0
            and legal_end >= legal_start
        )
        if not legal_ok:
            rejections.append(
                rejection(
                    "region_declaration",
                    CODE_ADDR_RANGE,
                    "legal interval must be [non-negative start, end>=start]",
                    record_ref=ref,
                    record_index=index,
                )
            )
        elif end is not None and not (
            legal_start <= base and end <= legal_end
        ):
            rejections.append(
                rejection(
                    "region_declaration",
                    CODE_ADDR_RANGE,
                    "mapping range is outside its declared legal interval",
                    record_ref=ref,
                    record_index=index,
                )
            )

        initial_prot = None
        try:
            initial_prot = parse_protection(raw.get("initial_prot"))
        except ProtectionParseError as exc:
            rejections.append(
                rejection(
                    "region_declaration",
                    CODE_PROT,
                    str(exc),
                    record_ref=ref,
                    record_index=index,
                    raw_excerpt=raw.get("initial_prot"),
                )
            )

        if len(rejections) != local_errors:
            continue

        key = (process_guid, region_id)
        if key in seen:
            rejections.append(
                rejection(
                    "region_declaration",
                    CODE_REGION_DUPLICATE,
                    "region_id must be unique within endpoint and process",
                    record_ref=ref,
                    record_index=index,
                    raw_excerpt={"key": key},
                )
            )
            continue
        seen.add(key)

        depends = raw.get("rollback_depends_on_regions", [])
        if not isinstance(depends, list) or not all(
            isinstance(item, str) and item for item in depends
        ):
            depends = []
        regions.append(
            {
                "endpoint_id": endpoint_id,
                "process_guid": process_guid,
                "region_id": region_id,
                "region_generation": generation,
                "module": module.strip(),
                "map_kind": map_kind,
                "base_offset": base,
                "region_size": size,
                "end_offset": end,
                "legal_start": legal_start,
                "legal_end": legal_end,
                "initial_prot": initial_prot,
                "policy_key": raw.get("policy_key"),
                "rollback_depends_on_regions": depends,
                "record_ref": ref,
            }
        )

    return regions


def _policy_record_collection(data: Any) -> List[Dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("policies", "records", "items"):
            if isinstance(data.get(key), list):
                return [item for item in data[key] if isinstance(item, dict)]
        if "policy_key" in data or "key" in data:
            return [data]
    return []


def parse_map_kind_list(value: Any) -> Optional[List[str]]:
    if value is None:
        return None
    if isinstance(value, str):
        items = [part.strip() for part in re.split(r"[\s,|]+", value) if part.strip()]
    elif isinstance(value, list):
        items = value
    else:
        raise ValueError("map_kinds must be a string or array")
    invalid = [item for item in items if item not in CANONICAL_MAP_KINDS]
    if invalid:
        raise ValueError(f"unsupported map_kind values: {invalid}")
    return sorted(set(items))


def parse_policy_record(
    raw: Dict[str, Any],
    *,
    snapshot: str,
    source_ref: str,
    clock: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Return normalized candidate or a rejection dictionary."""

    try:
        policy_key = raw.get("policy_key", raw.get("key", raw.get("id")))
        if not valid_identifier(policy_key):
            raise ValueError("policy_key must be a non-empty stable identifier")

        generation = raw.get("generation", raw.get("policy_generation", 0))
        if not is_int(generation) or generation < 0:
            raise ValueError("policy generation must be a non-negative integer")

        tombstone = parse_bool_flag(raw.get("tombstone", False), "tombstone")
        disabled = parse_bool_flag(raw.get("disabled", raw.get("disabled_flag", False)), "disabled")
        map_kinds = parse_map_kind_list(
            raw.get("map_kinds", raw.get("applies_to_map_kinds"))
        )

        effective_interval: Optional[List[int]] = None
        time_bound_ms = 0
        if snapshot == "v1":
            if clock is None:
                raise ValueError("clock is required for v1 conversion")
            seconds = raw.get(
                "effective_after_boot_seconds",
                raw.get("relative_seconds", raw.get("effective_seconds")),
            )
            if not is_int(seconds) or seconds < 0:
                raise ValueError("v1 relative boot seconds must be a non-negative integer")
            boot_ms = int(clock["boot_time_ms"])
            effective_at_ms = boot_ms + seconds * 1000
            effective_interval = [effective_at_ms, effective_at_ms + 1000]
            time_bound_ms = 1000
            window_seconds = raw.get("effective_window_seconds")
            if window_seconds is not None:
                if (
                    not isinstance(window_seconds, list)
                    or len(window_seconds) != 2
                    or not all(is_int(v) and v >= 0 for v in window_seconds)
                    or window_seconds[0] > window_seconds[1]
                ):
                    raise ValueError("effective_window_seconds must be [start,end]")
                effective_window_ms = [
                    boot_ms + window_seconds[0] * 1000,
                    boot_ms + window_seconds[1] * 1000,
                ]
            else:
                effective_window_ms = None
        else:
            effective_at_ms = raw.get("effective_at_ms", raw.get("effective_ms"))
            if not is_int(effective_at_ms) or effective_at_ms < 0:
                raise ValueError("v2 effective_at_ms must be a non-negative integer")
            effective_interval = [effective_at_ms, effective_at_ms]
            window = raw.get("effective_window_ms")
            if window is not None:
                if (
                    not isinstance(window, list)
                    or len(window) != 2
                    or not all(is_int(v) and v >= 0 for v in window)
                    or window[0] > window[1]
                ):
                    raise ValueError("effective_window_ms must be [start,end]")
                effective_window_ms = [int(window[0]), int(window[1])]
            else:
                effective_window_ms = None

        max_prot = None
        protection_value = raw.get(
            "max_prot_mask",
            raw.get("protection", raw.get("flat_protection", raw.get("prot"))),
        )
        if protection_value is not None:
            parsed = parse_protection(protection_value)
            max_prot = parsed["mask"]
        elif not tombstone:
            raise ValueError("non-tombstone policy requires a protection mask/string")

        allowed_masks = None
        if raw.get("allowed_masks") is not None:
            allowed_masks = []
            for item in raw["allowed_masks"]:
                allowed_masks.append(parse_protection(item)["mask"])

        required_mask = None
        if raw.get("required_mask") is not None:
            required_mask = parse_protection(raw["required_mask"])["mask"]

        target = raw.get("target") if isinstance(raw.get("target"), dict) else {}
        candidate = {
            "policy_key": policy_key,
            "generation": generation,
            "effective_at_ms": effective_at_ms,
            "effective_interval_ms": effective_interval,
            "time_quantization_error_bound_ms": time_bound_ms,
            "effective_window_ms": effective_window_ms,
            "max_mask": max_prot,
            "allowed_masks": sorted(set(allowed_masks)) if allowed_masks is not None else None,
            "required_mask": required_mask,
            "map_kinds": map_kinds,
            "tombstone": tombstone,
            "disabled": disabled,
            "target": target,
            "source": {
                "snapshot": snapshot,
                "ref": source_ref,
            },
        }
        return candidate, None
    except (ValueError, ProtectionParseError) as exc:
        return None, rejection(
            f"policy_{snapshot}",
            CODE_POLICY,
            str(exc),
            record_ref=source_ref,
            raw_excerpt=raw if isinstance(raw, dict) else None,
        )


def parse_v1_file(
    path: Path,
    clock: Dict[str, Any],
    rejections: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    data = load_json(path, "policy_v1_snapshot", rejections)
    if data is None:
        return []
    records = _policy_record_collection(data)
    candidates = []
    for index, raw in enumerate(records, start=1):
        candidate, item = parse_policy_record(
            raw,
            snapshot="v1",
            source_ref=f"{path.name}#{index}",
            clock=clock,
        )
        if item:
            rejections.append(item)
        elif candidate:
            candidate["source"]["path"] = str(path)
            candidates.append(candidate)
    return candidates


def parse_v2_manifest(
    path: Path,
    rejections: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Optional[Path]]:
    data = load_json(path, "policy_v2_manifest", rejections)
    if data is None:
        return [], None
    base = path.parent
    shard_names = data.get("shards", []) if isinstance(data, dict) else []
    candidates: List[Dict[str, Any]] = []
    if not isinstance(shard_names, list):
        rejections.append(
            rejection(
                "policy_v2_manifest",
                CODE_FIELD_TYPE,
                "shards must be an array",
                record_ref=str(path),
            )
        )
        shard_names = []
    for shard_name in shard_names:
        shard_path = base / str(shard_name)
        shard_data = load_json(shard_path, "policy_v2_shard", rejections)
        if shard_data is None:
            continue
        records = _policy_record_collection(shard_data)
        for index, raw in enumerate(records, start=1):
            candidate, item = parse_policy_record(
                raw,
                snapshot="v2",
                source_ref=f"{shard_path.name}#{index}",
            )
            if item:
                item["source_category"] = "policy_v2_shard"
                rejections.append(item)
            elif candidate:
                candidate["source"]["path"] = str(shard_path)
                candidates.append(candidate)

    sqlite_name = data.get("sqlite", data.get("metadata_db", "meta.sqlite"))
    sqlite_path = base / str(sqlite_name)
    db_candidates = parse_v2_sqlite(sqlite_path, rejections)
    return candidates + db_candidates, sqlite_path


def parse_v2_sqlite(
    path: Path,
    rejections: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not path.exists():
        rejections.append(
            rejection(
                "policy_v2_sqlite",
                CODE_JSON,
                "v2 metadata SQLite database not found",
                record_ref=str(path),
            )
        )
        return []
    candidates: List[Dict[str, Any]] = []
    try:
        sqlite_uri = Path(path).resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(sqlite_uri, uri=True) as conn:
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    """
                    SELECT policy_key, generation, effective_at_ms,
                           max_prot_mask, map_kinds_json, effective_window_json,
                           tombstone, disabled, target_json, required_mask,
                           allowed_masks_json, source_shard
                    FROM policy_meta
                    """
                ).fetchall()
            except sqlite3.DatabaseError as exc:
                rejections.append(
                    rejection(
                        "policy_v2_sqlite",
                        CODE_JSON,
                        f"cannot query policy_meta: {exc}",
                        record_ref=str(path),
                    )
                )
                return []
            for row_index, row in enumerate(rows, start=1):
                raw = {
                    "policy_key": row["policy_key"],
                    "generation": row["generation"],
                    "effective_at_ms": row["effective_at_ms"],
                    "max_prot_mask": row["max_prot_mask"],
                    "tombstone": row["tombstone"],
                    "disabled": row["disabled"],
                    "required_mask": row["required_mask"],
                }
                for field, column in (
                    ("map_kinds", "map_kinds_json"),
                    ("effective_window_ms", "effective_window_json"),
                    ("target", "target_json"),
                    ("allowed_masks", "allowed_masks_json"),
                ):
                    value = row[column]
                    if value is not None:
                        try:
                            raw[field] = json.loads(value)
                        except json.JSONDecodeError:
                            raw[field] = value
                candidate, item = parse_policy_record(
                    raw,
                    snapshot="v2_sqlite",
                    source_ref=f"{path.name}:policy_meta:{row_index}",
                )
                if item:
                    item["source_category"] = "policy_v2_sqlite"
                    rejections.append(item)
                elif candidate:
                    candidate["source"]["source_shard"] = row["source_shard"]
                    candidates.append(candidate)
    except sqlite3.DatabaseError as exc:
        rejections.append(
            rejection(
                "policy_v2_sqlite",
                CODE_JSON,
                f"cannot open SQLite metadata database: {exc}",
                record_ref=str(path),
            )
        )
    return candidates
