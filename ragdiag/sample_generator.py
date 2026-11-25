"""Generate the fixed-seed local sample used by the workbench and tests."""

from __future__ import annotations

import hashlib
import json
import random
from copy import deepcopy
from pathlib import Path
from typing import Any

from .canonical import checksum_of, content_hash


SEED = 20261126


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _entry(
    result_ref: str,
    version: int,
    format_version: str,
    content: Any,
    **metadata: Any,
) -> dict[str, Any]:
    return {
        "result_ref": result_ref,
        "version": version,
        "format_version": format_version,
        "checksum": checksum_of(content),
        "content_hash": content_hash(content),
        "content": content,
        **metadata,
    }


def _base_entries() -> list[dict[str, Any]]:
    weather_v1 = (
        "v1|weather|Shanghai|18.5|62|clear|2026-11-26T09:00:00Z"
    )
    weather_v2 = "\n".join(
        [
            "format_version=v2",
            "kind=weather",
            "city=Shanghai",
            "temperature=19.0",
            "humidity=58",
            "condition=cloudy",
            "observed_at=2026-11-26T09:05:00Z",
        ]
    )
    weather_v3 = {
        "format_version": "v3",
        "kind": "weather",
        "location": {"city": "Suzhou", "district": "Industrial Park"},
        "metrics": {"temperature": 20.25, "humidity": 55, "pressure": 1012.5},
        "condition": "light-rain",
        "observed_at": "2026-11-26T09:10:00Z",
    }
    inventory_v1 = (
        "v1|inventory|warehouse-7|SKU-42|31|20|restock|urgent"
    )
    inventory_v2 = "\n".join(
        [
            "format_version=v2",
            "kind=inventory",
            "warehouse=warehouse-2",
            "sku=SKU-17",
            "available=8",
            "reorder_point=15",
            "supplier=acme-components",
        ]
    )
    build_v1 = (
        "v1|build|python|3.12.13|numpy|2.5.5|green|42"
    )
    build_v3 = {
        "format_version": "v3",
        "kind": "build",
        "toolchain": {"python": "3.12.13", "numpy": "2.5.5"},
        "dependencies": [
            {"name": "setuptools", "version": "68.2.2"},
            {"name": "pip", "version": "26.3"},
        ],
        "status": "green",
        "metrics": {"duration_s": 37.5, "warnings": 0},
    }
    policy_v1 = (
        "v1|policy|retrieval|quality-first|16384|strict|on|half-even"
    )
    archive_records = [
        {
            "row": index,
            "sku": f"ARCHIVE-{index:04d}",
            "quantity": 100 + index,
            "note": "cold-storage diagnostic archive record " * 4,
        }
        for index in range(100)
    ]
    archive_v3 = {
        "format_version": "v3",
        "kind": "catalog",
        "dataset": "diagnostic-archive",
        "records": archive_records,
        "summary": {"rows": len(archive_records), "complete": True},
    }
    entries = [
        _entry(
            "res/weather/001",
            1,
            "v1",
            weather_v1,
            columns=[
                "kind",
                "city",
                "temperature",
                "humidity",
                "condition",
                "observed_at",
            ],
            numeric_fields=["temperature", "humidity"],
        ),
        _entry(
            "res/weather/002",
            1,
            "v2",
            weather_v2,
            fields=[
                "format_version",
                "kind",
                "city",
                "temperature",
                "humidity",
                "condition",
                "observed_at",
            ],
            field_count=7,
            numeric_fields=["temperature", "humidity"],
        ),
        _entry(
            "res/weather/003",
            1,
            "v3",
            weather_v3,
            required_paths=[
                "kind",
                "location.city",
                "metrics.temperature",
                "metrics.humidity",
                "condition",
            ],
            numeric_paths=[
                "metrics.temperature",
                "metrics.humidity",
                "metrics.pressure",
            ],
        ),
        _entry(
            "res/inventory/001",
            1,
            "v1",
            inventory_v1,
            columns=[
                "kind",
                "warehouse",
                "sku",
                "available",
                "reorder_point",
                "action",
                "priority",
            ],
            numeric_fields=["available", "reorder_point"],
        ),
        _entry(
            "res/inventory/002",
            1,
            "v2",
            inventory_v2,
            fields=[
                "format_version",
                "kind",
                "warehouse",
                "sku",
                "available",
                "reorder_point",
                "supplier",
            ],
            field_count=7,
            numeric_fields=["available", "reorder_point"],
        ),
        _entry(
            "res/build/001",
            1,
            "v1",
            build_v1,
            columns=[
                "kind",
                "language",
                "language_version",
                "library",
                "library_version",
                "status",
                "duration_s",
            ],
            numeric_fields=["duration_s"],
        ),
        _entry(
            "res/build/002",
            1,
            "v3",
            build_v3,
            required_paths=[
                "kind",
                "toolchain.python",
                "toolchain.numpy",
                "status",
                "metrics.duration_s",
            ],
            numeric_paths=["metrics.duration_s", "metrics.warnings"],
        ),
        _entry(
            "res/policy/001",
            1,
            "v1",
            policy_v1,
            columns=[
                "kind",
                "domain",
                "strategy",
                "memory",
                "mode",
                "subnormal_protection",
                "rounding",
            ],
            numeric_fields=["memory"],
        ),
        _entry(
            "res/catalog/archive",
            1,
            "v3",
            archive_v3,
            required_paths=[
                "kind",
                "dataset",
                "summary.rows",
                "summary.complete",
            ],
            numeric_paths=["summary.rows"],
        ),
    ]
    return entries


def _call(tool_id: str, result_ref: str, version: str, seed: int) -> dict[str, Any]:
    digest = hashlib.sha256(f"{seed}:{tool_id}:{result_ref}".encode()).hexdigest()
    return {
        "tool_id": tool_id,
        "arg_digest": f"sha256:{digest[:32]}",
        "result_ref": result_ref,
        "result_format_version": version,
    }


def _requests() -> list[dict[str, Any]]:
    rng = random.Random(SEED)
    requests = [
        {
            "request_id": "conv-001",
            "turn_no": 1,
            "query_text": "Shanghai weather temperature humidity forecast",
            "tool_calls": [
                _call("weather.v1", "res/weather/001", "v1", SEED),
                _call("weather.v2", "res/weather/002", "v2", SEED),
                _call("weather.v3", "res/weather/003", "v3", SEED),
            ],
        },
        {
            "request_id": "conv-001",
            "turn_no": 2,
            "query_text": "warehouse inventory shortage restock supplier weather risk",
            "tool_calls": [
                _call("weather.followup", "res/weather/002", "v2", SEED + 1),
                _call("inventory.v1", "res/inventory/001", "v1", SEED + 1),
            ],
        },
        {
            "request_id": "conv-002",
            "turn_no": 1,
            "query_text": "python build dependency version error toolchain",
            "tool_calls": [
                _call("build.v1", "res/build/001", "v1", SEED + 2),
                _call("build.v3", "res/build/002", "v3", SEED + 2),
            ],
        },
        {
            "request_id": "conv-003",
            "turn_no": 1,
            "query_text": "Suzhou rain pressure humidity warehouse policy",
            "tool_calls": [
                _call("weather.audit", "res/weather/003", "v3", SEED + 3),
                _call("inventory.audit", "res/inventory/002", "v2", SEED + 3),
            ],
        },
        {
            "request_id": "conv-004",
            "turn_no": 1,
            "query_text": "parallel fanout weather inventory build policy",
            "required_lanes": 4,
            "tool_calls": [
                _call("weather.fanout", "res/weather/001", "v1", SEED + 4),
                _call("inventory.fanout", "res/inventory/001", "v1", SEED + 4),
                _call("build.fanout", "res/build/001", "v1", SEED + 4),
                _call("policy.fanout", "res/policy/001", "v1", SEED + 4),
            ],
        },
        {
            "request_id": "conv-005",
            "turn_no": 1,
            "query_text": "expanded weather history repeated evidence tool latency",
            "tool_calls": [
                _call(
                    f"weather.history.{index:02d}",
                    "res/weather/002",
                    "v2",
                    SEED + 5,
                )
                for index in range(30)
            ],
        },
        {
            "request_id": "conv-006",
            "turn_no": 1,
            "query_text": "diagnostic archive catalog cold storage rows",
            "tool_calls": [
                _call("catalog.archive", "res/catalog/archive", "v3", SEED + 6),
            ],
        },
        {
            "request_id": "conv-007",
            "turn_no": 1,
            "query_text": "retrieval quality memory strict subnormal rounding policy",
            "tool_calls": [
                _call("policy.read", "res/policy/001", "v1", SEED + 7),
                _call("weather.policy", "res/weather/002", "v2", SEED + 7),
            ],
        },
        {
            "request_id": "conv-008",
            "turn_no": 1,
            "query_text": "build green duration forecast Shanghai",
            "tool_calls": [
                _call("build.check", "res/build/001", "v1", SEED + 8),
                _call("weather.check", "res/weather/001", "v1", SEED + 8),
            ],
        },
    ]
    for request in requests:
        request["seed_offset"] = rng.randrange(1000)
    return requests


def _corpus() -> list[dict[str, Any]]:
    texts = [
        (
            "doc-001",
            "Shanghai weather forecast clear temperature 18 humidity 62 coastal breeze",
            ["weather", "shanghai"],
        ),
        (
            "doc-002",
            "Suzhou industrial park light rain pressure humidity warehouse routing",
            ["weather", "suzhou", "warehouse"],
        ),
        (
            "doc-003",
            "warehouse seven inventory SKU shortage restock supplier urgent",
            ["inventory", "warehouse"],
        ),
        (
            "doc-004",
            "warehouse two inventory reorder point supplier acme components",
            ["inventory", "supplier"],
        ),
        (
            "doc-005",
            "python build dependency version error numpy setuptools toolchain",
            ["build", "python"],
        ),
        (
            "doc-006",
            "green build duration seconds warnings package cache",
            ["build", "latency"],
        ),
        (
            "doc-007",
            "retrieval quality first memory budget strict policy subnormal protection",
            ["policy", "retrieval"],
        ),
        (
            "doc-008",
            "rounding half even float64 deterministic vector similarity",
            ["precision", "retrieval"],
        ),
        (
            "doc-009",
            "parallel fanout concurrency limit logical time budget memory admission",
            ["scheduling", "budget"],
        ),
        (
            "doc-010",
            "cold storage diagnostic archive catalog row completeness",
            ["catalog", "archive"],
        ),
        (
            "doc-011",
            "weather risk forecast warehouse inventory restock planning",
            ["weather", "inventory"],
        ),
        (
            "doc-012",
            "tool result version checksum content addressed cache invalidation",
            ["cache", "tool"],
        ),
    ]
    return [
        {"doc_id": doc_id, "text": text, "tags": tags}
        for doc_id, text, tags in texts
    ]


def _schedule() -> dict[str, Any]:
    return {
        "schema": "ragdiag-schedule/v1",
        "seed": SEED,
        "precision": {
            "mode": "float64",
            "score_decimals": 8,
            "rounding_mode": "half_even",
            "subnormal_protection": True,
            "block_size": 3,
        },
        "retrieval": {
            "weights": {
                "inverted": 0.55,
                "prefix": 0.2,
                "vector": 0.25,
            },
            "top_k": 8,
            "candidate_limit": 7,
            "prefix_expansion": 3,
            "vector_dim": 24,
            "vector_lsh_bits": 8,
            "vector_probe_buckets": 2,
        },
        "scheduling": {
            "concurrency_limit": 3,
            "time_budget_ms": 110.0,
            "memory_budget_bytes": 16384,
            "budget_precedence": ["concurrency", "time", "memory"],
            "cost_model": {
                "read_ms_per_call": 4.0,
                "retrieve_ms": 2.0,
                "merge_ms": 2.0,
                "candidate_compare_ms": 0.5,
                "kib_read_ms": 0.25,
            },
        },
        "arbitration": {
            "priority_order": ["quality", "memory", "latency"],
            "memory_pressure_ratio": 0.8,
            "time_pressure_ratio": 0.85,
            "quality_gain_threshold": 0.02,
            "profiles": [
                {
                    "name": "compact",
                    "candidate_limit": 3,
                    "tool_expansion": 0,
                    "concurrency": 1,
                },
                {
                    "name": "balanced",
                    "candidate_limit": 5,
                    "tool_expansion": 1,
                    "concurrency": 2,
                },
                {
                    "name": "expanded",
                    "candidate_limit": 7,
                    "tool_expansion": 2,
                    "concurrency": 3,
                },
            ],
            "diagnostic_profiles": [
                {
                    "name": "max-recall",
                    "candidate_limit": 12,
                    "tool_expansion": 4,
                    "concurrency": 3,
                }
            ],
        },
        "merge": {
            "evidence_per_fragment": 2,
            "tool_expansion": 2,
        },
    }


def generate(out_dir: str | Path = "data") -> dict[str, Any]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    entries = _base_entries()
    repository = {
        "schema": "ragdiag-tool-results/v1",
        "snapshot_version": 7,
        "seed": SEED,
        "entries": entries,
    }
    updated_repository = deepcopy(repository)
    updated_repository["snapshot_version"] = 8
    for entry in updated_repository["entries"]:
        if entry["result_ref"] == "res/weather/002":
            entry["version"] = 2
            entry["content"] = "\n".join(
                [
                    "format_version=v2",
                    "kind=weather",
                    "city=Shanghai",
                    "temperature=21.0",
                    "humidity=61",
                    "condition=clear",
                    "observed_at=2026-11-26T10:05:00Z",
                ]
            )
            entry["checksum"] = checksum_of(entry["content"])
            entry["content_hash"] = content_hash(entry["content"])
    requests = _requests()
    schedule = _schedule()
    tuned_schedule = deepcopy(schedule)
    tuned_schedule["scheduling"]["memory_budget_bytes"] = 20000
    corpus = _corpus()
    repository["evidence_corpus"] = corpus
    updated_repository["evidence_corpus"] = corpus

    _write_json(out / "tool_results.json", repository)
    _write_json(out / "tool_results.next.json", updated_repository)
    _write_json(out / "schedule.json", schedule)
    _write_json(out / "schedule.tuned.json", tuned_schedule)
    with (out / "requests.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for request in requests:
            handle.write(
                json.dumps(
                    request,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
    with (out / "evidence_corpus.jsonl").open(
        "w", encoding="utf-8", newline="\n"
    ) as handle:
        for document in corpus:
            handle.write(
                json.dumps(
                    document,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
    files = [
        "requests.jsonl",
        "tool_results.json",
        "tool_results.next.json",
        "schedule.json",
        "schedule.tuned.json",
        "evidence_corpus.jsonl",
    ]
    manifest = {
        "schema": "ragdiag-sample/v1",
        "seed": SEED,
        "snapshot_version": 7,
        "updated_snapshot_version": 8,
        "files": {
            name: hashlib.sha256((out / name).read_bytes()).hexdigest()
            for name in files
        },
        "expected_request_codes": {
            "conv-004@1": "E_CONCURRENCY_BUDGET",
            "conv-005@1": "E_TIME_BUDGET",
            "conv-006@1": "E_MEMORY_BUDGET",
        },
    }
    _write_json(out / "manifest.json", manifest)
    return manifest

