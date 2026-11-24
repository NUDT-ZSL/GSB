"""Deterministic sample and injection-marker generator."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .canonical import UINT64_MAX, segment_key
from .parser import parse_osl
from .policy import LITERAL_RESERVED16_LAYOUT, OSL40_LAYOUT, Policy
from .revisions import RevisionBatch, compute_invalidation
from .storage import atomic_write_json
from .writer import BuiltOsl, SegmentSpec, build_osl


DEFAULT_SEED = 20261125


def deterministic_bytes(seed: int, size: int) -> bytes:
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, size=size, dtype=np.uint8).tobytes()


def default_anomaly_plan() -> list[dict[str, Any]]:
    return [
        {"case_id": "valid_basic", "kind": "valid", "seed": DEFAULT_SEED},
        {"case_id": "bad_magic", "kind": "magic", "magic_hex": "58534c31"},
        {"case_id": "bad_version", "kind": "version", "version": 99},
        {"case_id": "bad_total_length", "kind": "total_length", "offset": 18, "delta": 17},
        {"case_id": "oob_offset", "kind": "bounds", "target": "data", "offset_beyond_end": 4096},
        {"case_id": "overflow_offset", "kind": "bounds", "target": "data", "offset": UINT64_MAX - 7, "length": 64},
        {"case_id": "invalid_utf8_name", "kind": "utf8", "target": "bãd"},
        {"case_id": "bad_segment_type", "kind": "segment_type", "target": "data", "value": 99},
        {"case_id": "bad_compression", "kind": "compression", "target": "data", "value": 7},
        {"case_id": "dup_name", "kind": "duplicate_name", "name": "dup"},
        {"case_id": "selfref_table", "kind": "self_reference", "target": "data"},
        {"case_id": "overlap_pair", "kind": "overlap", "target": "beta", "other": "alpha", "shift": 8, "length": 16},
        {"case_id": "partial_overlap", "kind": "overlap", "target": "beta", "other": "alpha", "shift": 16, "length": 48},
        {"case_id": "checksum_bad", "kind": "checksum", "target": "sha"},
        {"case_id": "check_algo_width", "kind": "check_algorithm", "algorithm": 1, "declared_bits": 128},
        {"case_id": "unknown_algo", "kind": "unknown_algorithm", "algorithm": 99},
        {"case_id": "nested_valid", "kind": "nested", "depth": 2},
        {"case_id": "depth_exceeded", "kind": "depth", "links": 5, "max_depth": 4},
        {"case_id": "cycle_ancestor", "kind": "cycle", "ancestor": "cycle_root", "reference": "cycle_ref"},
        {"case_id": "quota_expanded_bytes", "kind": "quota", "quota_mode": "exact_top_level_size"},
        {"case_id": "timeout_zero_budget", "kind": "timeout", "timeout_seconds": 0.0},
        {"case_id": "literal_reserved16_layout", "kind": "header_layout", "layout": "literal42"},
    ]


def generate_all(
    output_dir: str | Path,
    seed: int = DEFAULT_SEED,
    policy: Policy | None = None,
) -> dict[str, Any]:
    root = Path(output_dir)
    injection_root = root / "injections"
    injection_root.mkdir(parents=True, exist_ok=True)
    used_policy = policy or Policy()

    markers: list[dict[str, Any]] = []
    for plan in default_anomaly_plan():
        marker = _build_case(plan, seed + _stable_case_seed(plan["case_id"]), used_policy, injection_root)
        markers.append(marker)

    revision_fixture = _write_revision_fixture(root, used_policy)
    plan_path = root / "anomaly_injections.json"
    atomic_write_json(
        plan_path,
        {
            "manifest": "OSL anomaly injection parameter list",
            "fixed_seed": seed,
            "marker_name": "OSL_SYNC_MARKER_V1",
            "cases": default_anomaly_plan(),
        },
    )
    manifest = {
        "fixed_seed": seed,
        "sample_count": len(markers),
        "injection_plan": str(plan_path),
        "revision_fixture": revision_fixture,
        "markers": markers,
    }
    atomic_write_json(root / "manifest.json", manifest)
    return manifest


def write_root_injection_plan(path: str | Path = "anomaly_injections.json") -> Path:
    target = Path(path)
    atomic_write_json(
        target,
        {
            "manifest": "OSL anomaly injection parameter list",
            "fixed_seed": DEFAULT_SEED,
            "marker_name": "OSL_SYNC_MARKER_V1",
            "cases": default_anomaly_plan(),
        },
    )
    return target


def _build_case(
    plan: dict[str, Any],
    seed: int,
    policy: Policy,
    injection_root: Path,
) -> dict[str, Any]:
    case_id = plan["case_id"]
    kind = plan["kind"]
    builder = _BUILDERS[kind]
    artifact = builder(plan, seed, policy)
    data = artifact["data"]
    expect_code = artifact["expect_code"]
    expect_offset = artifact["expect_offset"]
    parser_config = artifact.get("parser_config", {})
    policy_overrides = artifact.get("policy_overrides", {})

    case_dir = injection_root / case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    sample_path = case_dir / "sample.osl"
    sample_path.write_bytes(data)

    marker = {
        "marker": "OSL_SYNC_MARKER_V1",
        "case_id": case_id,
        "seed": seed,
        "sample_file": sample_path.name,
        "expect_code": expect_code,
        "expect_offset": expect_offset,
        "parser_config": parser_config,
        "policy_overrides": policy_overrides,
        "injection": plan,
        "binary_sync_marker": "OSLSYNC1@header_reserved_offset_26",
        "sync_marker_file": "sync_marker.json",
    }
    atomic_write_json(case_dir / "sync_marker.json", marker)
    return marker


def _valid_basic(plan: dict[str, Any], seed: int, policy: Policy) -> dict[str, Any]:
    payload = deterministic_bytes(seed, 96)
    built = build_osl(
        [
            SegmentSpec("meta", b"fixed-seed-meta", segment_type=2),
            SegmentSpec("data", payload, segment_type=1, compression=1, algo=1),
        ],
        policy,
        format_version=2,
    )
    return _artifact(built.data, "OK", None)


def _magic(plan: dict[str, Any], seed: int, policy: Policy) -> dict[str, Any]:
    built = build_osl([SegmentSpec("data", deterministic_bytes(seed, 32), compression=1, algo=1)], policy)
    data = bytearray(built.data)
    data[0:4] = bytes.fromhex(plan["magic_hex"])
    return _artifact(bytes(data), "E_HEADER", 0)


def _version(plan: dict[str, Any], seed: int, policy: Policy) -> dict[str, Any]:
    built = build_osl([SegmentSpec("data", b"version")], policy)
    data = bytearray(built.data)
    data[4:6] = int(plan["version"]).to_bytes(2, "little")
    return _artifact(bytes(data), "E_HEADER", 4)


def _total_length(plan: dict[str, Any], seed: int, policy: Policy) -> dict[str, Any]:
    built = build_osl([SegmentSpec("data", b"total-length")], policy)
    data = bytearray(built.data)
    value = int.from_bytes(data[18:26], "little") + int(plan["delta"])
    data[18:26] = value.to_bytes(8, "little")
    return _artifact(bytes(data), "E_HEADER", 18)


def _bounds(plan: dict[str, Any], seed: int, policy: Policy) -> dict[str, Any]:
    built = build_osl([SegmentSpec("data", deterministic_bytes(seed, 32))], policy)
    record = built.record("data")
    data = bytearray(built.data)
    field_offset = int(record["payload_offset_field_offset"])
    if "offset_beyond_end" in plan:
        value = len(data) + int(plan["offset_beyond_end"])
        length = 16
    else:
        value = int(plan["offset"])
        length = int(plan["length"])
    data[field_offset : field_offset + 8] = value.to_bytes(8, "little")
    length_offset = int(record["declared_length_field_offset"])
    data[length_offset : length_offset + 8] = length.to_bytes(8, "little")
    return _artifact(bytes(data), "E_BOUNDS", field_offset)


def _utf8(plan: dict[str, Any], seed: int, policy: Policy) -> dict[str, Any]:
    built = build_osl([SegmentSpec(plan["target"], b"utf8")], policy)
    record = built.record(plan["target"])
    data = bytearray(built.data)
    name_offset = int(record["name_offset"])
    data[name_offset] = 0xFF
    return _artifact(bytes(data), "E_HEADER", name_offset)


def _segment_type(plan: dict[str, Any], seed: int, policy: Policy) -> dict[str, Any]:
    built = build_osl([SegmentSpec("data", b"type")], policy)
    offset = int(built.record("data")["type_offset"])
    data = bytearray(built.data)
    data[offset] = int(plan["value"])
    return _artifact(bytes(data), "E_HEADER", offset)


def _compression(plan: dict[str, Any], seed: int, policy: Policy) -> dict[str, Any]:
    built = build_osl([SegmentSpec("data", b"comp")], policy)
    offset = int(built.record("data")["compression_offset"])
    data = bytearray(built.data)
    data[offset] = int(plan["value"])
    return _artifact(bytes(data), "E_HEADER", offset)


def _duplicate(plan: dict[str, Any], seed: int, policy: Policy) -> dict[str, Any]:
    name = plan["name"]
    built = build_osl(
        [SegmentSpec(name, b"first"), SegmentSpec(name, b"second")],
        policy,
    )
    second_offset = int(built.records[1]["name_offset"])
    return _artifact(built.data, "E_DUP_NAME", second_offset)


def _selfref(plan: dict[str, Any], seed: int, policy: Policy) -> dict[str, Any]:
    built = build_osl([SegmentSpec("data", deterministic_bytes(seed, 32))], policy)
    record = built.record("data")
    offset = int(record["payload_offset_field_offset"])
    length_offset = int(record["declared_length_field_offset"])
    data = bytearray(built.data)
    data[offset : offset + 8] = policy.header_layout.size.to_bytes(8, "little")
    data[length_offset : length_offset + 8] = (8).to_bytes(8, "little")
    return _artifact(bytes(data), "E_SELFREF", offset)


def _overlap(plan: dict[str, Any], seed: int, policy: Policy) -> dict[str, Any]:
    alpha_payload = deterministic_bytes(seed, 64)
    beta_payload = deterministic_bytes(seed + 1, 64)
    built = build_osl(
        [SegmentSpec("alpha", alpha_payload), SegmentSpec("beta", beta_payload)],
        policy,
    )
    target = built.record(plan["target"])
    other = built.record(plan["other"])
    other_payload = int(other["physical_payload_offset"])
    shifted = other_payload + int(plan["shift"])
    data = bytearray(built.data)
    offset_field = int(target["payload_offset_field_offset"])
    length_field = int(target["declared_length_field_offset"])
    data[offset_field : offset_field + 8] = shifted.to_bytes(8, "little")
    data[length_field : length_field + 8] = int(plan["length"]).to_bytes(8, "little")
    expected_offset = min(
        int(target["payload_offset_field_offset"]),
        int(other["payload_offset_field_offset"]),
    )
    return _artifact(bytes(data), "E_OVERLAP", expected_offset)


def _checksum(plan: dict[str, Any], seed: int, policy: Policy) -> dict[str, Any]:
    built = build_osl(
        [SegmentSpec("sha", deterministic_bytes(seed, 48), compression=1, algo=1)],
        policy,
    )
    offset = int(built.record("sha")["digest_offset"])
    data = bytearray(built.data)
    data[offset] ^= 0xFF
    return _artifact(bytes(data), "E_CHECKSUM", offset)


def _check_algorithm(plan: dict[str, Any], seed: int, policy: Policy) -> dict[str, Any]:
    declared_bytes = int(plan["declared_bits"]) // 8
    built = build_osl(
        [
            SegmentSpec(
                "width",
                b"algorithm-width",
                compression=1,
                algo=int(plan["algorithm"]),
                digest=b"\x11" * declared_bytes,
                digest_declared_length=declared_bytes,
            )
        ],
        policy,
    )
    offset = int(built.record("width")["digest_length_offset"])
    return _artifact(built.data, "E_CHECK_ALGO", offset)


def _unknown_algorithm(plan: dict[str, Any], seed: int, policy: Policy) -> dict[str, Any]:
    built = build_osl(
        [SegmentSpec("unknown", b"x", algo=int(plan["algorithm"]), digest=b"")],
        policy,
    )
    offset = int(built.record("unknown")["algo_offset"])
    return _artifact(built.data, "E_CHECK_ALGO", offset)


def _nested_valid(plan: dict[str, Any], seed: int, policy: Policy) -> dict[str, Any]:
    child = build_osl(
        [
            SegmentSpec("inner-meta", b"inner"),
            SegmentSpec("inner-sha", deterministic_bytes(seed, 32), compression=1, algo=1),
        ],
        policy,
    )
    if int(plan["depth"]) == 2:
        middle = build_osl([SegmentSpec("middle", child.data, compression=0x10, segment_type=4)], policy)
        top = build_osl([SegmentSpec("outer", middle.data, compression=0x10, segment_type=4)], policy)
    else:
        top = build_osl([SegmentSpec("outer", child.data, compression=0x10, segment_type=4)], policy)
    return _artifact(top.data, "OK", None)


def _depth(plan: dict[str, Any], seed: int, policy: Policy) -> dict[str, Any]:
    links = int(plan["links"])
    leaf = build_osl([SegmentSpec("leaf", b"leaf-payload")], policy).data
    current = build_osl(
        [SegmentSpec(f"n{links - 1}", leaf, compression=0x10, segment_type=4)],
        policy,
    )
    target_offset = int(current.record(f"n{links - 1}")["compression_offset"])
    current_bytes = current.data
    for level in range(links - 2, -1, -1):
        parent = build_osl(
            [SegmentSpec(f"n{level}", current_bytes, compression=0x10, segment_type=4)],
            policy,
        )
        parent_record = parent.record(f"n{level}")
        target_offset = int(parent_record["physical_payload_offset"]) + target_offset
        current_bytes = parent.data
    marker_policy = Policy(max_depth=int(plan["max_depth"]))
    return _artifact(
        current_bytes,
        "E_DEPTH",
        target_offset,
        parser_config={"max_depth": int(plan["max_depth"])},
        policy_overrides={"max_depth": int(plan["max_depth"])},
    )


def _cycle(plan: dict[str, Any], seed: int, policy: Policy) -> dict[str, Any]:
    ancestor = plan["ancestor"]
    reference = plan["reference"]
    target_key = segment_key(ancestor)
    child = build_osl(
        [
            SegmentSpec(
                reference,
                segment_type=3,
                refs=[target_key],
            )
        ],
        policy,
    )
    parent = build_osl(
        [SegmentSpec(ancestor, child.data, compression=0x10, segment_type=4)],
        policy,
    )
    offset = int(parent.record(ancestor)["physical_payload_offset"]) + int(
        child.record(reference)["physical_payload_offset"]
    )
    return _artifact(parent.data, "E_CYCLE", offset)


def _quota(plan: dict[str, Any], seed: int, policy: Policy) -> dict[str, Any]:
    child = build_osl([SegmentSpec("child", deterministic_bytes(seed, 128))], policy)
    parent = build_osl(
        [SegmentSpec("outer", child.data, compression=0x10, segment_type=4)],
        policy,
    )
    offset = int(parent.record("outer")["physical_payload_offset"])
    quota = len(parent.data)
    return _artifact(
        parent.data,
        "E_QUOTA",
        offset,
        parser_config={"max_expanded_bytes": quota},
        policy_overrides={"max_expanded_bytes": quota},
    )


def _timeout(plan: dict[str, Any], seed: int, policy: Policy) -> dict[str, Any]:
    built = build_osl([SegmentSpec("slow", deterministic_bytes(seed, 64), compression=1, algo=1)], policy)
    return _artifact(
        built.data,
        "E_TIMEOUT",
        0,
        parser_config={"timeout_seconds": float(plan["timeout_seconds"])},
    )


def _literal16(plan: dict[str, Any], seed: int, policy: Policy) -> dict[str, Any]:
    literal_policy = Policy(header_layout=LITERAL_RESERVED16_LAYOUT)
    built = build_osl(
        [SegmentSpec("literal", deterministic_bytes(seed, 32), compression=1, algo=1)],
        literal_policy,
    )
    return _artifact(
        built.data,
        "OK",
        None,
        policy_overrides={"header_layout": "literal42"},
    )


def _write_revision_fixture(root: Path, policy: Policy) -> str:
    old_raw = b"raw-content-v1"
    new_raw = b"raw-content-v2-deterministic"
    old_built = build_osl(
        [
            SegmentSpec("raw", old_raw, segment_type=1),
            SegmentSpec("agg", segment_type=3, refs=[segment_key("raw")]),
            SegmentSpec("report", segment_type=3, refs=[segment_key("agg")]),
        ],
        policy,
    )
    new_built = build_osl(
        [
            SegmentSpec("raw", new_raw, segment_type=1),
            SegmentSpec("agg", segment_type=3, refs=[segment_key("raw")]),
            SegmentSpec("report", segment_type=3, refs=[segment_key("agg")]),
        ],
        policy,
    )
    old_result = parse_osl(old_built.data, "revision-demo", policy)
    new_result = parse_osl(new_built.data, "revision-demo", policy)
    old_batch = RevisionBatch.from_result(old_result, "r1")
    new_batch = RevisionBatch.from_result(new_result, "r2", "r1")
    closure = compute_invalidation(old_batch, new_batch)
    path = root / "revision_fixture.json"
    atomic_write_json(
        path,
        {
            "old": old_batch.to_dict(),
            "new": new_batch.to_dict(),
            "expected_closure": closure,
        },
    )
    return str(path)


def _artifact(
    data: bytes,
    code: str,
    offset: int | None,
    *,
    parser_config: dict[str, Any] | None = None,
    policy_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "data": data,
        "expect_code": code,
        "expect_offset": offset,
        "parser_config": parser_config or {},
        "policy_overrides": policy_overrides or {},
    }


def _stable_case_seed(case_id: str) -> int:
    value = 0
    for byte in case_id.encode("utf-8"):
        value = (value * 131 + byte) % 1_000_003
    return value


_BUILDERS: dict[str, Callable[..., dict[str, Any]]] = {
    "valid": _valid_basic,
    "magic": _magic,
    "version": _version,
    "total_length": _total_length,
    "bounds": _bounds,
    "utf8": _utf8,
    "segment_type": _segment_type,
    "compression": _compression,
    "duplicate_name": _duplicate,
    "self_reference": _selfref,
    "overlap": _overlap,
    "checksum": _checksum,
    "check_algorithm": _check_algorithm,
    "unknown_algorithm": _unknown_algorithm,
    "nested": _nested_valid,
    "depth": _depth,
    "cycle": _cycle,
    "quota": _quota,
    "timeout": _timeout,
    "header_layout": _literal16,
}
