"""End-to-end reproducible self-check and report assembly."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any, Callable

from .annotations import (
    AnnotationDocument,
    AnnotationRecord,
    arbitrate_annotations,
    default_role,
    load_annotations,
    migrate_annotations,
    parse_annotations,
    semantic_signature,
    write_annotations,
)
from .cache import ContentAddressedCache
from .canonical import (
    atomic_write_json,
    canonical_json,
    read_json,
    report_hash,
    sha256_obj,
)
from .costs import normalize_cost_config, score_edges
from .errors import LayoutError
from .geometry import (
    build_candidate_edges,
    normalize_page,
    true_order_fingerprint,
    validate_candidate_coverage,
)
from .pipeline import (
    LayoutRunner,
    STAGES,
    build_pipeline_graph,
    report_content_fingerprint,
)
from .samples import (
    DEFAULT_SEED,
    generate_dataset,
    read_samples,
)
from .snapshot import (
    LayoutSnapshot,
    SnapshotStore,
    build_snapshot,
    replace_snapshot_section,
)
from .solver import deviation_spectrum, validate_order


def _capture_error(operation: Callable[[], Any]) -> dict[str, Any]:
    try:
        operation()
    except LayoutError as exc:
        return {"raised": True, "error": exc.to_dict()}
    except Exception as exc:  # pragma: no cover - defensive report path
        return {
            "raised": True,
            "error": {
                "code": "E_UNEXPECTED",
                "message": str(exc),
                "type": type(exc).__name__,
            },
        }
    return {"raised": False, "error": None}


def _check(
    checks: list[dict[str, Any]],
    name: str,
    condition: bool,
    details: dict[str, Any] | None = None,
) -> None:
    checks.append(
        {
            "name": name,
            "passed": bool(condition),
            **({"details": details} if details else {}),
        }
    )


def _validate_algorithm_report(report: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    graph = report["graph"]
    _check(
        checks,
        "graph_is_acyclic",
        graph["acyclic"] and len(graph["topological_order"]) == len(graph["nodes"]),
        {
            "node_count": len(graph["nodes"]),
            "topological_count": len(graph["topological_order"]),
        },
    )
    execution = report["execution"]
    recomputed = execution["recomputed_nodes"]
    reused = execution["reused_nodes"]
    _check(
        checks,
        "recompute_reuse_disjoint",
        not (set(recomputed) & set(reused)),
    )
    _check(
        checks,
        "node_partition_complete",
        sorted(set(recomputed) | set(reused)) == sorted(graph["nodes"]),
    )
    affected_from_nodes = sorted(
        {
            execution["node_status"][node]["page_id"]
            for node in recomputed
        }
    )
    _check(
        checks,
        "affected_pages_cover_recomputed_nodes",
        affected_from_nodes == execution["affected_pages"],
        {"affected_pages": affected_from_nodes},
    )
    for page in report["pages"]:
        page_id = page["page_id"]
        block_ids = {block["block_id"] for block in page["blocks"]}
        sequence = list(page["reading_order"])
        _check(
            checks,
            f"order_once:{page_id}",
            len(sequence) == len(block_ids) and set(sequence) == block_ids,
            {"block_count": len(block_ids)},
        )
        true_sequence = list(page["reading_order_true"])
        recomputed_spectrum = deviation_spectrum(sequence, true_sequence)
        _check(
            checks,
            f"deviation_recomputed:{page_id}",
            recomputed_spectrum == page["metrics"]["position_deviation"],
        )
        chain_cost = sum(
            float(edge["total_cost"]) for edge in page["edge_cost_chain"]
        )
        _check(
            checks,
            f"chain_cost_recomputed:{page_id}",
            abs(chain_cost - float(page["total_cost"])) <= 1e-8,
            {"chain_cost": chain_cost, "reported": page["total_cost"]},
        )
        component_ok = all(
            abs(
                sum(float(value) for value in edge["components"].values())
                - float(edge["total_cost"])
            )
            <= 1e-8
            for edge in page["costed_edges"]
        )
        _check(checks, f"cost_components_recomputed:{page_id}", component_ok)
        normalized_page = {
            "page_id": page_id,
            "canvas_shape": page["canvas_shape"],
            "blocks": page["blocks"],
        }
        coverage_result = _capture_error(
            lambda normalized_page=normalized_page, edges=page[
                "candidate_edges"
            ]: validate_candidate_coverage(normalized_page, edges)
        )
        _check(
            checks,
            f"candidate_coverage:{page_id}",
            not coverage_result["raised"],
            coverage_result.get("error"),
        )
        views = page["annotation_views"]
        view_ok = True
        view_details: dict[str, Any] = {}
        for version in ("v1", "v2", "v3"):
            try:
                document = parse_annotations(
                    views[version], source=f"{page_id}:{version}"
                )
                signature = semantic_signature(document)
                view_ok = view_ok and signature["sequence"] == sequence
                view_ok = view_ok and set(signature["block_ids"]) == block_ids
                view_details[version] = {
                    "sequence": signature["sequence"],
                    "block_count": len(signature["block_ids"]),
                }
            except LayoutError as exc:
                view_ok = False
                view_details[version] = exc.to_dict()
        _check(
            checks,
            f"three_format_views:{page_id}",
            view_ok,
            view_details,
        )
    _, _, _, _ = _annotation_documents_from_report(report)
    return checks


def _annotation_documents_from_report(
    report: dict[str, Any],
) -> tuple[dict[str, AnnotationDocument], dict[str, AnnotationDocument], dict[str, AnnotationDocument], dict[str, AnnotationDocument]]:
    canonical: dict[str, AnnotationDocument] = {}
    v1: dict[str, AnnotationDocument] = {}
    v2: dict[str, AnnotationDocument] = {}
    v3: dict[str, AnnotationDocument] = {}
    for page in report["pages"]:
        by_id = {block["block_id"]: block for block in page["blocks"]}
        records = []
        for position, block_id in enumerate(page["reading_order"]):
            block = by_id[block_id]
            records.append(
                AnnotationRecord(
                    block_id=block_id,
                    reading_order=position,
                    block_type=block["block_type"],
                    text_len=block["text_len"],
                    confidence_low=0.0,
                    confidence_high=1.0,
                    paragraph_role=default_role(block["block_type"]),
                    order_weight=1.0,
                    column_index=block["column_index"],
                    zone_id=block["zone_id"],
                    nesting_path=block["nesting_path"],
                )
            )
        document = AnnotationDocument(
            version="v3",
            records=records,
            source=f"selfcheck:{page['page_id']}",
        )
        page_id = page["page_id"]
        canonical[page_id] = document
        v3[page_id] = parse_annotations(
            write_annotations(document, "v3", allow_loss=True)
        )
        v2[page_id] = parse_annotations(
            write_annotations(document, "v2", allow_loss=True)
        )
        v1[page_id] = parse_annotations(
            write_annotations(document, "v1", allow_loss=True)
        )
    return canonical, v1, v2, v3


def _annotation_checks(report: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    canonical, v1_docs, v2_docs, v3_docs = _annotation_documents_from_report(report)
    examples: dict[str, Any] = {}
    for page_id in sorted(canonical):
        reference = canonical[page_id]
        upgraded_v1 = migrate_annotations(v1_docs[page_id], "v3")
        upgraded_v2 = migrate_annotations(v2_docs[page_id], "v3")
        arbitration = arbitrate_annotations(
            [v1_docs[page_id], v2_docs[page_id], v3_docs[page_id]]
        )
        ref_signature = semantic_signature(reference)
        same_sequence_weight = (
            semantic_signature(upgraded_v1)["sequence"] == ref_signature["sequence"]
            and semantic_signature(upgraded_v2)["sequence"]
            == ref_signature["sequence"]
            and semantic_signature(upgraded_v1)["order_weights"]
            == ref_signature["order_weights"]
            and semantic_signature(upgraded_v2)["order_weights"]
            == ref_signature["order_weights"]
        )
        block_sets_equal = (
            set(ref_signature["block_ids"])
            == set(semantic_signature(v1_docs[page_id])["block_ids"])
            == set(semantic_signature(v2_docs[page_id])["block_ids"])
            == set(semantic_signature(v3_docs[page_id])["block_ids"])
        )
        strict_error = _capture_error(
            lambda reference=reference: migrate_annotations(reference, "v1")
        )
        lossy = migrate_annotations(reference, "v1", allow_loss=True)
        lossy_roundtrip = parse_annotations(
            write_annotations(lossy, "v1", allow_loss=True)
        )
        lossy_signature = semantic_signature(lossy_roundtrip)
        _check(
            checks,
            f"annotation_three_way_equivalence:{page_id}",
            same_sequence_weight
            and block_sets_equal
            and arbitration.winner_version == "v3"
            and bool(upgraded_v1.completions),
            {
                "sequence": ref_signature["sequence"],
                "v1_completions": len(upgraded_v1.completions),
                "v2_completions": len(upgraded_v2.completions),
            },
        )
        _check(
            checks,
            f"annotation_semantic_drift_guard:{page_id}",
            strict_error["raised"]
            and strict_error["error"]["code"] == "E_SEMANTIC_DRIFT",
            strict_error.get("error"),
        )
        _check(
            checks,
            f"annotation_lossy_downgrade:{page_id}",
            bool(lossy.losses)
            and lossy_signature["sequence"] == ref_signature["sequence"]
            and lossy_signature["order_weights"] == ref_signature["order_weights"],
            {"loss_count": len(lossy.losses)},
        )
        if page_id in sorted(canonical)[:2]:
            examples[page_id] = {
                "v1_annotations": write_annotations(
                    canonical[page_id], "v1", allow_loss=True
                ),
                "v2_annotations": write_annotations(
                    canonical[page_id], "v2", allow_loss=True
                ),
                "v3_annotations": write_annotations(canonical[page_id], "v3"),
                "v1_to_v3_completions": upgraded_v1.completions[:10],
                "v3_to_v1_losses": lossy.losses[:10],
                "arbitration": arbitration.to_dict(),
            }
    return {
        "checks": checks,
        "examples": examples,
        "page_count": len(canonical),
    }


def _snapshot_checks(
    base: Path, initial_snapshot: LayoutSnapshot
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    store_dir = base / "snapshot-semantics"
    store = SnapshotStore(store_dir)
    first = store.commit(initial_snapshot)
    _check(checks, "snapshot_initial_commit", first.changed)
    block_content = copy.deepcopy(
        initial_snapshot.section("block_annotations").content
    )
    first_page = sorted(block_content)[0]
    block_content[first_page]["blocks"][0]["x1"] += 3
    newer = replace_snapshot_section(
        initial_snapshot, "block_annotations", block_content
    )
    second = store.commit(newer)
    _check(
        checks,
        "snapshot_whole_replacement",
        second.changed
        and store.current is not None
        and store.current.snapshot_id == newer.snapshot_id
        and store.current.section("block_annotations").content == block_content,
    )
    rollback = _capture_error(lambda: store.commit(initial_snapshot))
    _check(
        checks,
        "snapshot_rollback_rejected",
        rollback["raised"]
        and rollback["error"]["code"] == "E_SNAPSHOT_ROLLBACK",
        rollback.get("error"),
    )
    _check(
        checks,
        "snapshot_rollback_did_not_replace",
        store.current is not None and store.current.snapshot_id == newer.snapshot_id,
    )
    busy = _capture_error(
        lambda: _busy_commit(store, replace_snapshot_section(
            newer, "edge_weights", copy.deepcopy(newer.section("edge_weights").content)
        ))
    )
    _check(
        checks,
        "snapshot_stage_busy_nonblocking",
        busy["raised"] and busy["error"]["code"] == "E_STAGE_BUSY",
        busy.get("error"),
    )
    corrupt_dir = base / "snapshot-corrupt"
    corrupt_store = SnapshotStore(corrupt_dir)
    corrupt_store.commit(initial_snapshot)
    payload = read_json(corrupt_store.current_path)
    payload["sections"]["edge_weights"]["content"]["weights"]["vertical_gap"] = 999
    atomic_write_json(corrupt_store.current_path, payload)
    corrupt = _capture_error(lambda: SnapshotStore(corrupt_dir))
    _check(
        checks,
        "snapshot_checksum_corrupt",
        corrupt["raised"] and corrupt["error"]["code"] == "E_SNAPSHOT_CORRUPT",
        corrupt.get("error"),
    )
    return {
        "checks": checks,
        "final_snapshot": store.current.metadata() if store.current else None,
    }


def _busy_commit(store: SnapshotStore, candidate: LayoutSnapshot) -> None:
    with store.stage_lease():
        store.commit(candidate)


def _anomaly_checks(
    anomaly_files: dict[str, str],
    initial_snapshot: LayoutSnapshot,
    base: Path,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    cases: dict[str, Any] = {}

    def expect(
        name: str,
        operation: Callable[[], Any],
        code: str,
        field: str | None = None,
        line: int | None = None,
    ) -> None:
        result = _capture_error(operation)
        actual = result.get("error") or {}
        passed = (
            result["raised"]
            and actual.get("code") == code
            and (field is None or actual.get("field") == field)
            and (line is None or actual.get("line") == line)
        )
        cases[name] = result
        _check(checks, f"anomaly:{name}", passed, actual)

    expect(
        "field_count_mismatch",
        lambda: load_annotations(anomaly_files["field_count_mismatch"]),
        "E_FORMAT_INVALID",
        line=2,
    )
    expect(
        "required_field_missing",
        lambda: load_annotations(anomaly_files["required_field_missing"]),
        "E_FORMAT_INVALID",
        line=2,
    )
    expect(
        "block_id_unparseable",
        lambda: load_annotations(anomaly_files["block_id_unparseable"]),
        "E_FORMAT_INVALID",
        line=2,
    )
    expect(
        "unknown_version",
        lambda: load_annotations(anomaly_files["unknown_version"]),
        "E_VERSION_UNSUPPORTED",
        line=1,
    )
    expect(
        "reading_order_gap",
        lambda: load_annotations(anomaly_files["order_gap"]),
        "E_FORMAT_INVALID",
    )
    expect(
        "illegal_edge_weight",
        lambda: normalize_cost_config(read_json(anomaly_files["illegal_weight"])),
        "E_CONFIG_INVALID",
        field="weights.type_order_violation",
    )
    expect(
        "missing_block_field",
        lambda: read_samples(anomaly_files["missing_block_field"]),
        "E_FORMAT_INVALID",
    )
    expect(
        "reading_order_out_of_range",
        lambda: normalize_page(read_samples(anomaly_files["out_of_range"])[0]),
        "E_FORMAT_INVALID",
        field="reading_order_true",
    )
    coverage_pages = initial_snapshot.section("block_annotations").content
    coverage_page = normalize_page(coverage_pages[sorted(coverage_pages)[0]])
    expect(
        "candidate_coverage",
        lambda: validate_candidate_coverage(coverage_page, []),
        "E_BLOCK_COVERAGE",
    )
    coverage_edges = build_candidate_edges(coverage_page)["edges"]
    coverage_costs = score_edges(coverage_page, coverage_edges)
    duplicate_sequence = [
        coverage_page["blocks"][0]["block_id"],
        coverage_page["blocks"][0]["block_id"],
    ]
    expect(
        "order_inconsistent",
        lambda: validate_order(coverage_page, duplicate_sequence, coverage_costs),
        "E_ORDER_INCONSISTENT",
    )
    limited_limits = {
        "max_blocks_per_page": 2,
        "max_edge_comparisons": 10_000,
        "max_resident_memory_mb": 64,
        "limit_policy": "error",
    }
    limited_runner = LayoutRunner(
        ContentAddressedCache(base / "anomaly-cache"),
        limits=limited_limits,
    )
    expect(
        "resource_limit_block_count",
        lambda: limited_runner.run(initial_snapshot),
        "E_RESOURCE_LIMIT",
        field="limits.max_blocks_per_page",
    )
    degrade_limits = {
        "max_blocks_per_page": 14,
        "max_edge_comparisons": 1,
        "max_resident_memory_mb": 64,
        "limit_policy": "degrade",
    }
    degrade_runner = LayoutRunner(
        ContentAddressedCache(base / "degrade-cache"),
        limits=degrade_limits,
    )
    degraded_report = degrade_runner.run(initial_snapshot)
    degradation_categories = sorted(
        {
            item["category"]
            for item in degraded_report.get("degradations", [])
            if item.get("active")
        }
    )
    _check(
        checks,
        "degradation_marked",
        "edge_comparison_budget" in degradation_categories,
        {"categories": degradation_categories},
    )
    cases["degradation_categories"] = degradation_categories
    return {"checks": checks, "cases": cases}


def _compare_incremental(
    incremental: dict[str, Any],
    full_recheck: dict[str, Any],
    *,
    expected_geometry_pages: set[str] | None = None,
    expected_unaffected_pages: set[str] | None = None,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    _check(
        checks,
        "incremental_matches_full_content",
        report_content_fingerprint(incremental)
        == report_content_fingerprint(full_recheck),
        {
            "incremental": report_content_fingerprint(incremental),
            "full": report_content_fingerprint(full_recheck),
        },
    )
    execution = incremental["execution"]
    _check(
        checks,
        "incremental_recompute_reuse_disjoint",
        execution["recompute_reuse_disjoint"],
    )
    _check(
        checks,
        "incremental_node_partition_complete",
        execution["node_partition_complete"],
    )
    _check(
        checks,
        "incremental_affected_pages_covered",
        set(execution["affected_pages"])
        == set(execution["recomputed_pages"]),
    )
    if expected_geometry_pages is not None:
        _check(
            checks,
            "incremental_geometry_scope",
            set(execution["geometry_affected_pages"])
            == expected_geometry_pages,
            {
                "expected": sorted(expected_geometry_pages),
                "actual": execution["geometry_affected_pages"],
            },
        )
    if expected_unaffected_pages is not None:
        _check(
            checks,
            "incremental_unaffected_pages_reused",
            expected_unaffected_pages <= set(execution["reused_pages"])
            and not (
                expected_unaffected_pages & set(execution["recomputed_pages"])
            ),
            {
                "expected_unaffected": sorted(expected_unaffected_pages),
                "recomputed_pages": execution["recomputed_pages"],
            },
        )
    for page in incremental["pages"]:
        page_id = page["page_id"]
        _check(
            checks,
            f"incremental_order_once:{page_id}",
            len(page["reading_order"]) == len(page["blocks"])
            and set(page["reading_order"])
            == {block["block_id"] for block in page["blocks"]},
        )
        chain = sum(float(edge["total_cost"]) for edge in page["edge_cost_chain"])
        _check(
            checks,
            f"incremental_cost_recomputed:{page_id}",
            abs(chain - float(page["total_cost"])) <= 1e-8,
        )
    return checks


def _seal_report(report: dict[str, Any]) -> dict[str, Any]:
    sealed = copy.deepcopy(report)
    sealed["report_hash"] = report_hash(sealed)
    return sealed


def run_self_check(
    report_path: str | Path,
    *,
    workdir: str | Path,
    seed: int = DEFAULT_SEED,
    page_count: int = 5,
) -> dict[str, Any]:
    base = Path(workdir) / "layout-selfcheck"
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True, exist_ok=True)
    dataset = generate_dataset(
        base / "data",
        seed=seed,
        page_count=page_count,
        snapshot_dir=base / "initial-snapshots",
    )
    config = dataset["config"]
    initial_snapshot = dataset["snapshot"]
    limits = config["limits"]
    cache_a = ContentAddressedCache(base / "cache-a")
    runner_a = LayoutRunner(cache_a, limits=limits)
    baseline = runner_a.run(initial_snapshot, mode="baseline")
    checks = _validate_algorithm_report(baseline)
    annotation_result = _annotation_checks(baseline)
    checks.extend(annotation_result["checks"])

    page_ids = sorted(initial_snapshot.section("block_annotations").content)
    changed_page = page_ids[0]
    unaffected_pages = set(page_ids[1:])
    block_content = copy.deepcopy(
        initial_snapshot.section("block_annotations").content
    )
    editable = next(
        block
        for block in block_content[changed_page]["blocks"]
        if block["block_type"] == "body"
    )
    editable["x1"] = int(editable["x1"]) + 7
    editable["x2"] = int(editable["x2"]) - 3
    editable["y1"] = int(editable["y1"]) + 2
    snapshot_block = replace_snapshot_section(
        initial_snapshot, "block_annotations", block_content
    )

    incremental_runs: list[dict[str, Any]] = []
    store = SnapshotStore(base / "versioned-snapshots")
    store.commit(initial_snapshot)
    store.commit(snapshot_block)
    block_incremental = runner_a.run(snapshot_block, mode="incremental_block_annotation")
    block_full = LayoutRunner(
        ContentAddressedCache(base / "full-cache-block"), limits=limits
    ).run(snapshot_block, mode="full_recheck_block_annotation")
    block_checks = _compare_incremental(
        block_incremental,
        block_full,
        expected_geometry_pages={changed_page},
        expected_unaffected_pages=unaffected_pages,
    )
    checks.extend(block_checks)
    incremental_runs.append(
        {
            "name": "block_annotation_correction",
            "changed_page": changed_page,
            "snapshot": snapshot_block.metadata(),
            "incremental": block_incremental,
            "full_recheck": block_full,
            "checks": block_checks,
        }
    )

    weight_content = copy.deepcopy(
        snapshot_block.section("edge_weights").content
    )
    weight_content["weights"]["vertical_gap"] = 7.0
    snapshot_weights = replace_snapshot_section(
        snapshot_block, "edge_weights", weight_content
    )
    store.commit(snapshot_weights)
    weight_incremental = runner_a.run(
        snapshot_weights, mode="incremental_edge_weights"
    )
    weight_full = LayoutRunner(
        ContentAddressedCache(base / "full-cache-weights"), limits=limits
    ).run(snapshot_weights, mode="full_recheck_edge_weights")
    weight_checks = _compare_incremental(
        weight_incremental,
        weight_full,
        expected_geometry_pages=set(),
    )
    checks.extend(weight_checks)
    incremental_runs.append(
        {
            "name": "edge_weight_adjustment",
            "snapshot": snapshot_weights.metadata(),
            "incremental": weight_incremental,
            "full_recheck": weight_full,
            "checks": weight_checks,
        }
    )

    type_content = copy.deepcopy(snapshot_weights.section("type_order").content)
    type_content["body"] = 3
    snapshot_types = replace_snapshot_section(
        snapshot_weights, "type_order", type_content
    )
    store.commit(snapshot_types)
    type_incremental = runner_a.run(
        snapshot_types, mode="incremental_type_order"
    )
    type_full = LayoutRunner(
        ContentAddressedCache(base / "full-cache-types"), limits=limits
    ).run(snapshot_types, mode="full_recheck_type_order")
    type_checks = _compare_incremental(
        type_incremental,
        type_full,
        expected_geometry_pages=set(),
    )
    checks.extend(type_checks)
    incremental_runs.append(
        {
            "name": "type_order_change",
            "snapshot": snapshot_types.metadata(),
            "incremental": type_incremental,
            "full_recheck": type_full,
            "checks": type_checks,
        }
    )

    snapshot_result = _snapshot_checks(base, initial_snapshot)
    checks.extend(snapshot_result["checks"])
    anomaly_result = _anomaly_checks(dataset["anomalies"], initial_snapshot, base)
    checks.extend(anomaly_result["checks"])

    repro_report = LayoutRunner(
        ContentAddressedCache(base / "repro-cache"), limits=limits
    ).run(initial_snapshot, mode="baseline")
    baseline_sealed = _seal_report(baseline)
    repro_sealed = _seal_report(repro_report)
    reproducible = baseline_sealed["report_hash"] == repro_sealed["report_hash"]
    _check(
        checks,
        "fixed_seed_report_hash_reproducible",
        reproducible,
        {
            "first": baseline_sealed["report_hash"],
            "second": repro_sealed["report_hash"],
        },
    )

    error_codes = sorted(
        {
            result["error"]["code"]
            for result in anomaly_result["cases"].values()
            if isinstance(result, dict)
            and result.get("raised")
            and isinstance(result.get("error"), dict)
        }
        | {
            check["details"]["code"]
            for check in checks
            if isinstance(check.get("details"), dict)
            and "code" in check["details"]
        }
    )
    degradations = []
    for page in baseline["pages"]:
        if page.get("degraded", {}).get("active"):
            degradations.append(
                {
                    "page_id": page["page_id"],
                    **page["degraded"],
                }
            )
    for category in anomaly_result["cases"].get("degradation_categories", []):
        degradations.append(
            {
                "page_id": "multiple",
                "active": True,
                "category": category,
                "reason": "resource limit policy requested degradation",
            }
        )
    final_report: dict[str, Any] = {
        "schema_version": 1,
        "dataset": {
            "seed": seed,
            "page_count": page_count,
            "samples": "data/samples.jsonl",
            "config_sha256": sha256_obj(config),
            "rng": config["rng"],
        },
        "baseline": baseline,
        "incremental_runs": incremental_runs,
        "annotation_checks": annotation_result,
        "snapshot_checks": snapshot_result,
        "anomaly_checks": anomaly_result,
        "reproducibility": {
            "first_report_hash": baseline_sealed["report_hash"],
            "second_report_hash": repro_sealed["report_hash"],
            "identical": reproducible,
        },
        "checks": checks,
        "passed": all(check["passed"] for check in checks),
        "business_codes": ["OK", *error_codes],
        "degradations": degradations,
    }
    final_report = _seal_report(final_report)
    atomic_write_json(report_path, final_report)
    return final_report

