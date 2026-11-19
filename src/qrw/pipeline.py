from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable

from .canonical import canonical_json, sha256_hex, walk_nodes, write_json
from .compiler import compile_bytes, compile_templates
from .equivalence import adjudicate
from .errors import BusinessError
from .expansion import expand_query
from .migration import (
    downgrade_ir_to_v1,
    migrate_v1_to_v2,
    migrate_v2_to_v1,
    migrate_v2_to_v3,
)
from .models import CompilerConfig
from .readers import (
    load_slot_types,
    read_queries,
    read_template_file,
    read_template_text,
)
from .snapshot import Snapshot, SnapshotManager, load_current_snapshot, load_snapshot
from .validator import verify


def compile_dialect_bundle(
    base_dir: Path,
    config: CompilerConfig,
) -> dict[str, dict[str, Any]]:
    slot_types = load_slot_types(base_dir / "slot_types.json")
    files = {"v1": "v1.txt", "v2": "v2.txt", "v3": "v3.json"}
    return {
        dialect: compile_templates(
            read_template_file(base_dir / filename, slot_types, config),
            config,
        )
        for dialect, filename in files.items()
    }


def compile_snapshot(
    snapshot: Snapshot,
    config: CompilerConfig,
) -> dict[str, dict[str, Any]]:
    return {
        dialect: compile_templates(
            read_template_text(
                snapshot.sources[dialect],
                f"{snapshot.manifest_path.name}#{dialect}",
                snapshot.slot_types,
                config,
            ),
            config,
        )
        for dialect in ("v1", "v2", "v3")
    }


def _capture(name: str, expected_code: str, operation: Callable[[], Any]) -> dict[str, Any]:
    try:
        operation()
    except BusinessError as exc:
        return {
            "name": name,
            "status": "EXPECTED_ERROR" if exc.code == expected_code else "UNEXPECTED_ERROR",
            "matched": exc.code == expected_code,
            "expected_code": expected_code,
            "error": exc.to_dict(),
        }
    return {
        "name": name,
        "status": "UNEXPECTED_SUCCESS",
        "matched": False,
        "expected_code": expected_code,
        "error": None,
    }


def _injections(samples: Path, config: CompilerConfig) -> list[dict[str, Any]]:
    anomalies = samples / "anomalies"
    slot_types = load_slot_types(anomalies / "slot_types.json")

    def bad_version() -> None:
        read_template_file(anomalies / "invalid_version.txt", slot_types, config)

    def bad_pipes() -> None:
        read_template_file(anomalies / "bad_pipes.txt", slot_types, config)

    def empty_pattern() -> None:
        read_template_file(anomalies / "empty_pattern.txt", slot_types, config)

    def duplicate_query() -> None:
        read_queries(anomalies / "duplicate_queries.jsonl")

    def bad_slot_type() -> None:
        parsed = read_template_file(anomalies / "bad_slot_type.json", slot_types, config)
        compile_templates(parsed, config)

    def over_depth() -> None:
        parsed = read_template_file(anomalies / "over_depth.json", slot_types, config)
        compiled = compile_templates(parsed, config)
        expand_query(
            compiled,
            {
                "query_id": "anomaly_depth",
                "query_text": "上海",
                "locale": "zh_CN",
                "template_set_version": "2",
            },
            config,
        )

    return [
        _capture("非法版本头", "E_VERSION_UNSUPPORTED", bad_version),
        _capture("竖线字段数不符", "E_FORMAT_CORRUPT", bad_pipes),
        _capture("空 pattern", "E_PATTERN_EMPTY", empty_pattern),
        _capture("重复 query_id", "E_DUP_QUERY", duplicate_query),
        _capture("槽位类型越界", "E_CONFIG_INVALID", bad_slot_type),
        _capture("展开深度超限", "E_CONFIG_INVALID", over_depth),
    ]


def _candidate_comparison(
    left: dict[str, dict[str, Any]],
    right: dict[str, dict[str, Any]],
    queries: list[dict[str, Any]],
    config: CompilerConfig,
) -> dict[str, Any]:
    first_difference = None
    equal = True
    for query in queries:
        left_candidates = expand_query(left, query, config)
        right_candidates = expand_query(right, query, config)
        left_normalized = [
            {
                "candidate": item["candidate"],
                "score_detail": item["score_detail"],
                "offset_map": item["offset_map"],
            }
            for item in left_candidates
        ]
        right_normalized = [
            {
                "candidate": item["candidate"],
                "score_detail": item["score_detail"],
                "offset_map": item["offset_map"],
            }
            for item in right_candidates
        ]
        if left_normalized != right_normalized:
            equal = False
            if first_difference is None:
                first_difference = {
                    "query_id": query["query_id"],
                    "left": left_normalized,
                    "right": right_normalized,
                }
    return {"equal": equal, "first_difference": first_difference}


def _migration_checks(
    samples: Path,
    compiled: dict[str, dict[str, Any]],
    queries: list[dict[str, Any]],
    config: CompilerConfig,
) -> dict[str, Any]:
    slot_types = load_slot_types(samples / "slot_types.json")
    parsed_v1 = read_template_file(samples / "v1.txt", slot_types, config)
    parsed_v2 = read_template_file(samples / "v2.txt", slot_types, config)
    v1_to_v2 = migrate_v1_to_v2(parsed_v1, config)
    v2_roundtrip = read_template_text(v1_to_v2["artifact"], "<migration:v1_to_v2>", slot_types, config)
    v1_to_v2_compiled = compile_templates(v2_roundtrip, config)
    v2_to_v1 = migrate_v2_to_v1(parsed_v2)
    v1_roundtrip = read_template_text(v2_to_v1["artifact"], "<migration:v2_to_v1>", slot_types, config)
    v2_to_v1_compiled = compile_templates(v1_roundtrip, config)
    v2_to_v3 = migrate_v2_to_v3(parsed_v2)
    v3_roundtrip = read_template_text(v2_to_v3["artifact"], "<migration:v2_to_v3>", slot_types, config)
    v2_to_v3_compiled = compile_templates(v3_roundtrip, config)
    advanced_ir = compile_dialect_bundle(samples / "advanced", config)["v3"]
    downgrade = downgrade_ir_to_v1(advanced_ir)
    return {
        "v1_to_v2": {
            "artifact": v1_to_v2["artifact"],
            "fill_ins": v1_to_v2["fill_ins"],
            "discards": v1_to_v2["discards"],
            "candidate_equivalence": _candidate_comparison(
                compiled["v1"], v1_to_v2_compiled, queries, config
            ),
        },
        "v2_to_v1": {
            "artifact": v2_to_v1["artifact"],
            "fill_ins": v2_to_v1["fill_ins"],
            "discards": v2_to_v1["discards"],
            "candidate_equivalence": _candidate_comparison(
                compiled["v2"], v2_to_v1_compiled, queries, config
            ),
        },
        "v2_to_v3": {
            "artifact": v2_to_v3["artifact"],
            "fill_ins": v2_to_v3["fill_ins"],
            "discards": v2_to_v3["discards"],
            "candidate_equivalence": _candidate_comparison(
                compiled["v2"], v2_to_v3_compiled, queries, config
            ),
        },
        "v3_to_v1_with_discard": {
            "artifact": downgrade["artifact"],
            "fill_ins": downgrade["fill_ins"],
            "discards": downgrade["discards"],
        },
    }


def _determinism_check(
    compiled: dict[str, dict[str, Any]],
    queries: list[dict[str, Any]],
    config: CompilerConfig,
) -> dict[str, Any]:
    per_dialect: dict[str, Any] = {}
    for dialect, ir in compiled.items():
        first = compile_bytes(ir)
        second = compile_bytes(ir)
        expansions_one = {
            query["query_id"]: expand_query(ir, query, config) for query in queries
        }
        expansions_two = {
            query["query_id"]: expand_query(ir, query, config) for query in queries
        }
        per_dialect[dialect] = {
            "ir_byte_identical": first == second,
            "expansion_byte_identical": canonical_json(expansions_one)
            == canonical_json(expansions_two),
            "ir_sha256": sha256_hex(first),
            "ir_bytes": len(first),
        }
    return {
        "ok": all(
            item["ir_byte_identical"] and item["expansion_byte_identical"]
            for item in per_dialect.values()
        ),
        "dialects": per_dialect,
    }


def _snapshot_operations(
    samples: Path,
    config: CompilerConfig,
    compiled_current: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    snapshot_1 = load_snapshot(samples / "snapshots" / "snapshot_1.json")
    snapshot_2 = load_snapshot(samples / "snapshots" / "snapshot_2.json")
    snapshot_retired = load_snapshot(samples / "snapshots" / "snapshot_retired.json")
    manager = SnapshotManager()
    first_install = manager.install(snapshot_1)
    compiled_1 = compile_snapshot(snapshot_1, config)
    second_install = manager.install(snapshot_2, active_ir=compiled_1["v2"])
    idempotent = manager.install(snapshot_2, active_ir=compiled_1["v2"])
    rollback = _capture(
        "快照回退拒绝",
        "E_SNAPSHOT_ROLLBACK",
        lambda: manager.install(snapshot_1),
    )
    retired = _capture(
        "槽位类型退休拒绝",
        "E_SLOT_TYPE_RETIRED",
        lambda: manager.install(snapshot_retired, active_ir=compiled_current["v2"]),
    )
    checksum = _checksum_corruption_check(samples)
    return {
        "install_v1": first_install,
        "install_v2": second_install,
        "idempotent_v2": idempotent,
        "rollback_attempt": rollback,
        "retired_slot_type": retired,
        "checksum_mismatch": checksum,
        "current_version_after_attempts": manager.current.version if manager.current else None,
    }


def _checksum_corruption_check(samples: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="qrw-snapshot-") as temp_name:
        temp = Path(temp_name)
        (temp / "snapshots").mkdir(parents=True)
        shutil.copytree(samples / "snapshot_sources", temp / "snapshot_sources")
        manifest = json.loads(
            (samples / "snapshots" / "snapshot_2.json").read_text(encoding="utf-8")
        )
        tampered = temp / "snapshot_sources" / "v2" / "v1.txt"
        tampered.write_text(tampered.read_text(encoding="utf-8") + "# tampered\n", encoding="utf-8")
        manifest_path = temp / "snapshots" / "snapshot_bad.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        try:
            load_snapshot(manifest_path)
        except BusinessError as exc:
            return {
                "name": "快照校验和不符",
                "status": "EXPECTED_ERROR" if exc.code == "E_SNAPSHOT_CORRUPT" else "UNEXPECTED_ERROR",
                "matched": exc.code == "E_SNAPSHOT_CORRUPT",
                "expected_code": "E_SNAPSHOT_CORRUPT",
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "line": exc.line,
                    "field": exc.field,
                    "column": exc.column,
                },
            }
    return {
        "name": "快照校验和不符",
        "status": "UNEXPECTED_SUCCESS",
        "matched": False,
        "expected_code": "E_SNAPSHOT_CORRUPT",
        "error": None,
    }


def _equivalence_summary(report: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in report.items() if key != "expansion"}


def _build_dialect_report(
    compiled: dict[str, dict[str, Any]],
    snapshot: Snapshot,
    determinism: dict[str, Any],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for dialect, ir in compiled.items():
        result[dialect] = {
            "source_name": ir["source"]["name"],
            "source_text": snapshot.sources[dialect],
            "checksum": ir["checksum"],
            "rule_count": len(ir["rules"]),
            "node_count": sum(1 for _ in walk_nodes(ir)),
            "determinism": determinism["dialects"][dialect],
            "ir": ir,
        }
    return result


def run_demo(root: Path, config: CompilerConfig | None = None) -> dict[str, Any]:
    config = config or CompilerConfig()
    samples = root / "samples"
    snapshot, version_index = load_current_snapshot(samples / "versions.json")
    allowed_versions = {str(item["version"]) for item in version_index["snapshots"]}
    queries = read_queries(samples / "queries.jsonl", allowed_versions)
    compiled = compile_snapshot(snapshot, config)
    determinism = _determinism_check(compiled, queries, config)
    core_equivalence = adjudicate(compiled, queries, config)
    validation = verify(compiled, queries, config)
    advanced = compile_dialect_bundle(samples / "advanced", config)
    advanced_equivalence = adjudicate(advanced, queries, config)
    injections = _injections(samples, config)
    migrations = _migration_checks(samples, compiled, queries, config)
    snapshots = _snapshot_operations(samples, config, compiled)
    results = []
    for query in queries:
        results.append(
            {
                "query": query,
                "dialects": {
                    dialect: expand_query(compiled[dialect], query, config)
                    for dialect in ("v1", "v2", "v3")
                },
            }
        )
    injections_ok = all(item["matched"] for item in injections)
    snapshots_ok = (
        snapshots["install_v1"]["ok"]
        and snapshots["install_v2"]["ok"]
        and snapshots["rollback_attempt"]["matched"]
        and snapshots["retired_slot_type"]["matched"]
        and snapshots["checksum_mismatch"]["matched"]
    )
    migrations_ok = all(
        item["candidate_equivalence"]["equal"]
        for item in migrations.values()
        if "candidate_equivalence" in item
    )
    report_ok = (
        determinism["ok"]
        and core_equivalence["ok"]
        and validation["ok"]
        and advanced_equivalence["code"] == "E_SEMANTIC_DRIFT"
        and injections_ok
        and snapshots_ok
        and migrations_ok
    )
    recent_errors: list[dict[str, Any]] = []
    seen_codes: set[str] = set()
    for item in injections:
        if item["error"]:
            code = item["error"]["code"]
            if code not in seen_codes:
                seen_codes.add(code)
                recent_errors.append(item["error"])
    if advanced_equivalence["code"]:
        recent_errors.append(advanced_equivalence["error"])
    for item in (
        snapshots["rollback_attempt"],
        snapshots["retired_slot_type"],
        snapshots["checksum_mismatch"],
    ):
        if item["error"]:
            recent_errors.append(item["error"])
    return {
        "ok": report_ok,
        "mode": "local_deterministic_demo",
        "snapshot": snapshot.summary(),
        "queries": queries,
        "dialects": _build_dialect_report(compiled, snapshot, determinism),
        "results": results,
        "equivalence": _equivalence_summary(core_equivalence),
        "advanced_equivalence": _equivalence_summary(advanced_equivalence),
        "validation": validation,
        "determinism": determinism,
        "migrations": migrations,
        "injections": injections,
        "snapshot_operations": snapshots,
        "recent_business_errors": recent_errors,
    }


def write_report(report: dict[str, Any], output: Path) -> None:
    write_json(output, report)


def report_summary(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": report["ok"],
        "snapshot": report["snapshot"],
        "queries": len(report["queries"]),
        "dialects": sorted(report["dialects"]),
        "equivalence": report["equivalence"]["status"],
        "advanced_equivalence": report["advanced_equivalence"]["status"],
        "validation": report["validation"]["ok"],
        "deterministic": report["determinism"]["ok"],
        "injections": [
            {
                "name": item["name"],
                "status": item["status"],
                "code": item["error"]["code"] if item["error"] else None,
            }
            for item in report["injections"]
        ],
        "recent_business_error_codes": [
            item["code"] for item in report["recent_business_errors"]
        ],
    }

