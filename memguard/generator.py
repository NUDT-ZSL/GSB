"""Deterministic local fixture generator.

Expected values are a hand-authored truth table. They are serialized into the
trace as synchronization metadata and into a separate marker file, but the
detector is never allowed to consume them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .codes import (
    E_ACCESS_SEQ_DUPLICATE,
    E_PARENT_CYCLE,
    E_PARENT_MISSING,
    OK,
    OOB_CROSS_ANCESTOR,
    OOB_ENDPOINT_TOUCH,
    OOB_OUTSIDE_ANCESTORS,
    OOB_ZERO_LENGTH,
    OOB_ANCESTOR_CONTAINED,
)
from .models import Region
from .util import atomic_write_json, canonical_json_bytes


@dataclass(frozen=True)
class Scenario:
    access_seq: int
    region_id: str
    base_offset: int
    access_size: int
    access_kind: str
    expected_code: str
    expected_interval: tuple[int, int] | None
    timestamp_ms: int = 1000
    duplicate_of: int | None = None


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(1, "region-root", 0, 64, "read", OK, (0, 64)),
    Scenario(2, "region-leaf", 1100, 100, "write", OK, (1100, 1200)),
    Scenario(
        3,
        "region-leaf",
        3000,
        200,
        "read",
        OOB_ANCESTOR_CONTAINED,
        (3000, 3072),
    ),
    Scenario(
        4,
        "region-leaf",
        4000,
        1000,
        "read",
        OOB_CROSS_ANCESTOR,
        None,
    ),
    Scenario(
        5,
        "region-leaf",
        3072,
        8,
        "write",
        OOB_ENDPOINT_TOUCH,
        (3072, 3072),
    ),
    Scenario(
        6,
        "region-leaf",
        1016,
        8,
        "read",
        OOB_ENDPOINT_TOUCH,
        (1024, 1024),
    ),
    Scenario(
        7,
        "region-leaf",
        2000,
        0,
        "write",
        OOB_ZERO_LENGTH,
        (2000, 2000),
    ),
    Scenario(8, "region-cyc-a", 10000, 10, "read", E_PARENT_CYCLE, (10000, 10010)),
    Scenario(
        9,
        "region-orphan",
        12000,
        10,
        "write",
        E_PARENT_MISSING,
        (12000, 12010),
    ),
    Scenario(10, "region-touch", 14000, 1000, "read", OK, (14000, 15000)),
    Scenario(
        11,
        "region-touch",
        15000,
        10,
        "read",
        OOB_ENDPOINT_TOUCH,
        (15000, 15000),
    ),
    Scenario(
        12,
        "region-touch",
        14990,
        20,
        "write",
        OOB_OUTSIDE_ANCESTORS,
        (14990, 15000),
    ),
    Scenario(
        13,
        "region-root",
        65500,
        36,
        "read",
        OOB_OUTSIDE_ANCESTORS,
        None,
    ),
    Scenario(20, "region-beta", 100, 20, "write", OK, (100, 120)),
    Scenario(
        21,
        "region-beta",
        8000,
        300,
        "read",
        OOB_OUTSIDE_ANCESTORS,
        (8000, 8192),
    ),
)


REGIONS: tuple[Region, ...] = (
    Region("region-root", "arena-alpha", 65536, 0, 4096, 0, 100000, 1, None, "<generated>", 1),
    Region(
        "region-child",
        "arena-alpha",
        65536,
        512,
        3584,
        0,
        100000,
        2,
        "region-root",
        "<generated>",
        2,
    ),
    Region(
        "region-leaf",
        "arena-alpha",
        65536,
        1024,
        3072,
        0,
        100000,
        3,
        "region-child",
        "<generated>",
        3,
    ),
    Region(
        "region-touch",
        "arena-alpha",
        65536,
        14000,
        15000,
        0,
        100000,
        1,
        None,
        "<generated>",
        4,
    ),
    Region("region-beta", "arena-beta", 16384, 0, 8192, 0, 100000, 1, None, "<generated>", 5),
    Region(
        "region-cyc-a",
        "arena-beta",
        16384,
        10000,
        11000,
        0,
        100000,
        1,
        "region-cyc-b",
        "<generated>",
        6,
    ),
    Region(
        "region-cyc-b",
        "arena-beta",
        16384,
        9000,
        11500,
        0,
        100000,
        1,
        "region-cyc-a",
        "<generated>",
        7,
    ),
    Region(
        "region-orphan",
        "arena-beta",
        16384,
        12000,
        13000,
        0,
        100000,
        1,
        "region-missing",
        "<generated>",
        8,
    ),
)


def _region_document() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "regions": [region.canonical_dict() for region in REGIONS],
    }


def _trace_record(seed: int, scenario: Scenario, duplicate_index: int | None = None) -> dict[str, Any]:
    arena_size = 65536 if scenario.region_id in {
        "region-root",
        "region-child",
        "region-leaf",
        "region-touch",
    } else 16384
    access_end = scenario.base_offset + scenario.access_size
    suffix = "dup" if duplicate_index is not None else "ok"
    marker = "seed-%s-sync-%03d-%s" % (seed, scenario.access_seq, suffix)
    trace_id = "seed-%s-trace-%03d-%s" % (seed, scenario.access_seq, suffix)
    if duplicate_index is not None:
        marker = "%s-%d" % (marker, duplicate_index)
        trace_id = "%s-%d" % (trace_id, duplicate_index)
    expected_code = (
        E_ACCESS_SEQ_DUPLICATE if duplicate_index is not None else scenario.expected_code
    )
    expected_interval = (
        None if duplicate_index is not None else scenario.expected_interval
    )
    return {
        "trace_id": trace_id,
        "access_seq": scenario.access_seq,
        "region_id": scenario.region_id,
        "base_offset": scenario.base_offset,
        "access_size": scenario.access_size,
        "access_kind": scenario.access_kind,
        "arena_size": arena_size,
        "timestamp_ms": scenario.timestamp_ms,
        "sender_interval": [scenario.base_offset, access_end],
        "sync_marker": marker,
        "expected_interval": (
            None if expected_interval is None else [expected_interval[0], expected_interval[1]]
        ),
        "expected_business_code": expected_code,
    }


def _write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / (".%s.%s.tmp" % (path.name, "generator"))
    with temp.open("wb") as handle:
        for record in records:
            handle.write(canonical_json_bytes(record))
            handle.write(b"\n")
        handle.flush()
    json.loads(temp.read_text(encoding="utf-8").splitlines()[0])
    temp.replace(path)


def task_ids(seed: int) -> list[str]:
    dup = _trace_record(seed, SCENARIOS[2], 1)
    return [item["trace_id"] for item in _trace_records(seed)] + [dup["trace_id"]]


def _trace_records(seed: int) -> list[dict[str, Any]]:
    return [_trace_record(seed, scenario) for scenario in SCENARIOS]


def marker_document(seed: int) -> dict[str, Any]:
    markers: list[dict[str, Any]] = []
    for scenario in SCENARIOS:
        record = _trace_record(seed, scenario)
        markers.append(
            {
                "sync_marker": record["sync_marker"],
                "trace_id": record["trace_id"],
                "access_seq": record["access_seq"],
                "sender_interval": record["sender_interval"],
                "expected_interval": record["expected_interval"],
                "expected_business_code": record["expected_business_code"],
            }
        )
    duplicate = _trace_record(seed, SCENARIOS[2], 1)
    markers.append(
        {
            "sync_marker": duplicate["sync_marker"],
            "trace_id": duplicate["trace_id"],
            "access_seq": duplicate["access_seq"],
            "sender_interval": duplicate["sender_interval"],
            "expected_interval": None,
            "expected_business_code": E_ACCESS_SEQ_DUPLICATE,
        }
    )
    return {"schema_version": 1, "seed": seed, "markers": markers}


def invalid_trace_lines(seed: int) -> list[str]:
    return [
        "{",
        json.dumps(
            {
                "trace_id": "seed-%s-invalid-unknown" % seed,
                "access_seq": 30,
                "region_id": "region-not-declared",
                "base_offset": 0,
                "access_size": 1,
                "access_kind": "read",
                "arena_size": 65536,
                "timestamp_ms": 1,
            },
            sort_keys=True,
        ),
        json.dumps(
            {
                "trace_id": "seed-%s-invalid-kind" % seed,
                "access_seq": 31,
                "region_id": "region-root",
                "base_offset": 0,
                "access_size": 1,
                "access_kind": "execute",
                "arena_size": 65536,
                "timestamp_ms": 1,
            },
            sort_keys=True,
        ),
        json.dumps(
            {
                "trace_id": "seed-%s-invalid-offset" % seed,
                "access_seq": 32,
                "region_id": "region-root",
                "base_offset": -1,
                "access_size": 1,
                "access_kind": "read",
                "arena_size": 65536,
                "timestamp_ms": 1,
            },
            sort_keys=True,
        ),
        json.dumps(
            {
                "trace_id": "seed-%s-invalid-overflow" % seed,
                "access_seq": 33,
                "region_id": "region-root",
                "base_offset": (1 << 64) - 1,
                "access_size": 2,
                "access_kind": "read",
                "arena_size": 65536,
                "timestamp_ms": 1,
            },
            sort_keys=True,
        ),
        json.dumps(
            {
                "trace_id": "seed-%s-invalid-time" % seed,
                "access_seq": 34,
                "region_id": "region-root",
                "base_offset": 0,
                "access_size": 1,
                "access_kind": "read",
                "arena_size": 65536,
                "timestamp_ms": -1,
            },
            sort_keys=True,
        ),
    ]


def generate_fixture(out_dir: Path, seed: int = 20261125) -> dict[str, str]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    regions_path = out_dir / "regions.json"
    traces_path = out_dir / "traces.jsonl"
    invalid_path = out_dir / "zz_invalid_traces.jsonl"
    markers_path = out_dir / "sync_markers.json"
    state_dir = out_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    atomic_write_json(regions_path, _region_document())
    records = _trace_records(seed)
    duplicate = _trace_record(seed, SCENARIOS[2], 1)
    _write_jsonl(traces_path, records + [duplicate])
    # The first line is intentionally malformed so parser continuation is
    # exercised against the remaining valid but field-invalid records.
    with invalid_path.open("wb") as handle:
        handle.write(b"{\n")
        for record in [json.loads(line) for line in invalid_trace_lines(seed)[1:]]:
            handle.write(canonical_json_bytes(record))
            handle.write(b"\n")
    atomic_write_json(markers_path, marker_document(seed))
    (state_dir / ".keep").write_text("", encoding="utf-8")

    manifest = {
        "seed": seed,
        "regions": str(regions_path),
        "traces": str(traces_path),
        "invalid_traces": str(invalid_path),
        "sync_markers": str(markers_path),
        "state_dir": str(state_dir),
    }
    manifest_path = out_dir / "sample_manifest.json"
    atomic_write_json(manifest_path, manifest)
    return {key: str(value) for key, value in manifest.items() if key != "seed"}
