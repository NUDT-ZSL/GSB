"""End-to-end experiment orchestration and acceptance checks."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean
from typing import Any, Iterable

from .bptree import BPlusTree
from .compare import compare_hits, compare_query_runs, compare_tree_structures
from .generator import describe_edge_cases, generate_batches
from .indexes import INDEX_DEFINITIONS, IndexDefinition, get_index_definition
from .models import Batch, PortRecord, QuerySpec
from .queries import full_table_baseline, generate_queries
from .reporting import (
    assert_writable_directory,
    read_json,
    write_json_atomic,
)


@dataclass(slots=True)
class LabConfig:
    seed: int = 20260701
    batches: int = 4
    page_capacity: int = 12
    fill_factor: float = 0.5
    query_count: int = 10
    output_dir: Path = Path("output")
    stop_after_batch: int | None = None
    resume: bool = False

    def validate(self) -> None:
        if self.batches < 2:
            raise ValueError("--batches must be >= 2 for incremental validation")
        if self.page_capacity < 3:
            raise ValueError("--page-capacity must be >= 3")
        if not 0.05 <= self.fill_factor <= 0.95:
            raise ValueError("--fill-factor must be between 0.05 and 0.95")
        if self.query_count < 2:
            raise ValueError("--queries must be >= 2")
        if self.stop_after_batch is not None:
            if self.resume:
                raise ValueError("--stop-after-batch and --resume are mutually exclusive")
            if not 1 <= self.stop_after_batch <= self.batches:
                raise ValueError("--stop-after-batch must be within the batch range")


def _flatten(batches: Iterable[Batch]) -> list[PortRecord]:
    return [record for batch in batches for record in batch.records]


def _snapshot_path(output_dir: Path, index_name: str, batch_id: int) -> Path:
    return output_dir / "snapshots" / index_name / f"batch_{batch_id:04d}.json"


def _prepare_output(output_dir: Path, *, resume: bool) -> None:
    assert_writable_directory(output_dir)
    if not resume:
        for child in ("snapshots", "reports"):
            target = output_dir / child
            if target.exists():
                shutil.rmtree(target)
    (output_dir / "snapshots").mkdir(parents=True, exist_ok=True)
    (output_dir / "reports").mkdir(parents=True, exist_ok=True)


def _build_with_snapshots(
    *,
    index_definition: IndexDefinition,
    batches: list[Batch],
    config: LabConfig,
    start_batch_index: int = 0,
    tree: BPlusTree | None = None,
) -> tuple[BPlusTree, list[dict[str, Any]]]:
    if tree is None:
        tree = BPlusTree(
            index_definition,
            capacity=config.page_capacity,
            fill_factor=config.fill_factor,
            seed=config.seed,
        )
    per_batch: list[dict[str, Any]] = []
    for batch in batches[start_batch_index:]:
        split_start = tree.split_count
        tree.insert_many(batch.records)
        snapshot = tree.snapshot(
            completed_batch=batch.batch_id,
            batch_split_start=split_start,
        )
        snapshot_path = _snapshot_path(
            config.output_dir,
            index_definition.name,
            batch.batch_id,
        )
        write_json_atomic(snapshot_path, snapshot)
        per_batch.append(
            {
                "batch_id": batch.batch_id,
                "record_count": len(batch.records),
                "split_events": snapshot["batch_split_events"],
                "split_count_before": split_start,
                "split_count_after": tree.split_count,
                "structure": snapshot["structure"],
                "leaf_fill_statistics": snapshot["leaf_fill_statistics"],
                "snapshot_path": str(snapshot_path.relative_to(config.output_dir)),
            }
        )
    return tree, per_batch


def _build_from_records(
    index_definition: IndexDefinition,
    records: Iterable[PortRecord],
    config: LabConfig,
    *,
    completed_batch: int,
) -> BPlusTree:
    tree = BPlusTree(
        index_definition,
        capacity=config.page_capacity,
        fill_factor=config.fill_factor,
        seed=config.seed,
    )
    tree.insert_many(records)
    tree.last_completed_batch = completed_batch
    return tree


def _latest_snapshot(output_dir: Path, index_name: str) -> tuple[Path, dict[str, Any]]:
    directory = output_dir / "snapshots" / index_name
    paths = sorted(directory.glob("batch_*.json"))
    if not paths:
        raise FileNotFoundError(
            f"no snapshot found for index {index_name} under {directory}"
        )
    path = paths[-1]
    return path, read_json(path)


def _batch_metrics_from_snapshots(
    output_dir: Path,
    index_name: str,
    last_batch_id: int,
) -> list[dict[str, Any]]:
    metrics: list[dict[str, Any]] = []
    previous_record_count = 0
    for batch_id in range(1, last_batch_id + 1):
        path = _snapshot_path(output_dir, index_name, batch_id)
        snapshot = read_json(path)
        events = snapshot.get("batch_split_events", [])
        record_count = len(snapshot["records"])
        metrics.append(
            {
                "batch_id": batch_id,
                "record_count": record_count - previous_record_count,
                "split_events": events,
                "split_count_before": snapshot["split_count"] - len(events),
                "split_count_after": snapshot["split_count"],
                "structure": snapshot["structure"],
                "leaf_fill_statistics": snapshot["leaf_fill_statistics"],
                "snapshot_path": str(path.relative_to(output_dir)),
                "recovered_from_existing_snapshot": True,
            }
        )
        previous_record_count = record_count
    return metrics


def _scan_queries(
    tree: BPlusTree,
    queries: list[QuerySpec],
) -> list[dict[str, Any]]:
    return [tree.scan_query(query) for query in queries]


def _query_result_report(
    *,
    tree: BPlusTree,
    records: list[PortRecord],
    queries: list[QuerySpec],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    results: list[dict[str, Any]] = []
    comparisons: list[dict[str, Any]] = []
    for query in queries:
        scan = tree.scan_query(query)
        baseline = full_table_baseline(records, query, tree.index_definition)
        comparison = compare_hits(
            index_name=tree.index_definition.name,
            query=query,
            baseline=baseline,
            index_scan=scan,
        )
        results.append(
            {
                "query": query.to_dict(),
                "access_plan": {
                    "description": scan["access_description"],
                    "lower_key": scan["lower_key"],
                    "upper_key": scan["upper_key"],
                    "full_scan_required_by_index": scan[
                        "full_scan_required_by_index"
                    ],
                },
                "start_leaf_id": scan["start_leaf_id"],
                "termination_leaf_id": scan["termination_leaf_id"],
                "root_lookup_end_leaf_id": scan["root_lookup_end_leaf_id"],
                "visited_leaf_ids": scan["visited_leaf_ids"],
                "visited_page_count": scan["visited_page_count"],
                "stop_reason": scan["stop_reason"],
                "landing_correct": scan["landing_correct"],
                "landing_details": scan["landing_details"],
                "hits_sorted_by_index_key": scan["hits_sorted_by_index_key"],
                "hit_record_ids": scan["hit_record_ids"],
                "hit_records": scan["hits"],
                "hit_index_keys": scan["hit_index_keys"],
                "baseline_record_ids": [
                    record.write_seq for record in baseline
                ],
                "baseline_records": [record.to_dict() for record in baseline],
                "comparison": comparison,
            }
        )
        comparisons.append(comparison)
    return results, comparisons


def _index_metrics(
    *,
    tree: BPlusTree,
    query_results: list[dict[str, Any]],
    comparisons: list[dict[str, Any]],
    batch_metrics: list[dict[str, Any]],
    final_snapshot_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    pages_by_kind: dict[str, list[int]] = {}
    for result in query_results:
        pages_by_kind.setdefault(result["query"]["kind"], []).append(
            result["visited_page_count"]
        )
    mean_pages = {
        kind: round(fmean(values), 6)
        for kind, values in sorted(pages_by_kind.items())
    }
    structure = tree.validate_structure()
    return {
        "name": tree.index_definition.name,
        "label": tree.index_definition.label,
        "definition": tree.index_definition.describe(),
        "final_snapshot_path": str(final_snapshot_path.relative_to(output_dir)),
        "record_count": tree.record_count,
        "split_count": tree.split_count,
        "total_pages": structure["total_pages"],
        "depth": structure["depth"],
        "pages_by_level": structure["pages_by_level"],
        "leaf_count": structure["leaf_count"],
        "leaf_chain_connected": structure["leaf_chain_connected"],
        "leaf_fill_statistics": tree.leaf_fill_statistics(),
        "query_count": len(query_results),
        "consistent_query_count": sum(
            1 for comparison in comparisons if comparison["consistent"]
        ),
        "landing_correct_query_count": sum(
            1 for result in query_results if result["landing_correct"]
        ),
        "total_scan_pages": sum(
            result["visited_page_count"] for result in query_results
        ),
        "mean_scan_pages_by_kind": mean_pages,
        "batch_metrics": batch_metrics,
    }


def _path_consistency(
    *,
    index_definition: IndexDefinition,
    batches: list[Batch],
    records: list[PortRecord],
    queries: list[QuerySpec],
    continuous_tree: BPlusTree,
    config: LabConfig,
) -> dict[str, Any]:
    final_batch_id = batches[-1].batch_id
    base_batches = batches[:-1]
    final_batch = batches[-1]

    incremental = _build_from_records(
        index_definition,
        _flatten(base_batches),
        config,
        completed_batch=base_batches[-1].batch_id,
    )
    incremental.insert_many(final_batch.records)
    incremental.last_completed_batch = final_batch_id

    rebuilt = _build_from_records(
        index_definition,
        records,
        config,
        completed_batch=final_batch_id,
    )

    base_snapshot_path = _snapshot_path(
        config.output_dir,
        index_definition.name,
        base_batches[-1].batch_id,
    )
    base_snapshot = read_json(base_snapshot_path)
    recovered = BPlusTree.from_snapshot(base_snapshot, index_definition)
    recovered.insert_many(final_batch.records)
    recovered.last_completed_batch = final_batch_id

    incremental_scans = _scan_queries(incremental, queries)
    rebuilt_scans = _scan_queries(rebuilt, queries)
    recovered_scans = _scan_queries(recovered, queries)

    incremental_checks = [
        compare_query_runs(
            index_name=index_definition.name,
            query=query,
            left_label="incremental",
            left_scan=left,
            right_label="full_rebuild",
            right_scan=right,
        )
        for query, left, right in zip(queries, incremental_scans, rebuilt_scans)
    ]
    recovery_checks = [
        compare_query_runs(
            index_name=index_definition.name,
            query=query,
            left_label="snapshot_recovery",
            left_scan=left,
            right_label="fresh_continuous_full_write",
            right_scan=right,
        )
        for query, left, right in zip(queries, recovered_scans, rebuilt_scans)
    ]

    continuous_structure = continuous_tree.validate_structure()
    recovery_structure = recovered.validate_structure()
    incremental_structure = incremental.validate_structure()
    rebuilt_structure = rebuilt.validate_structure()
    structural_comparison = {
        "primary_run_matches_fresh_continuous": (
            continuous_tree.fingerprint() == rebuilt.fingerprint()
        ),
        "incremental_matches_full_rebuild": (
            incremental.fingerprint() == rebuilt.fingerprint()
        ),
        "recovery_matches_continuous": (
            recovered.fingerprint() == rebuilt.fingerprint()
        ),
        "recovery_matches_primary_run": (
            recovered.fingerprint() == continuous_tree.fingerprint()
        ),
        "primary_run_vs_fresh_continuous_page_diff": compare_tree_structures(
            continuous_tree,
            rebuilt,
        ),
        "incremental_vs_full_rebuild_page_diff": compare_tree_structures(
            incremental,
            rebuilt,
        ),
        "recovery_vs_continuous_page_diff": compare_tree_structures(
            recovered,
            rebuilt,
        ),
        "fingerprints": {
            "continuous": continuous_tree.fingerprint(),
            "incremental": incremental.fingerprint(),
            "full_rebuild": rebuilt.fingerprint(),
            "snapshot_recovery": recovered.fingerprint(),
        },
        "split_counts": {
            "continuous": continuous_tree.split_count,
            "incremental": incremental.split_count,
            "full_rebuild": rebuilt.split_count,
            "snapshot_recovery": recovered.split_count,
        },
        "depth": {
            "continuous": continuous_structure["depth"],
            "incremental": incremental_structure["depth"],
            "full_rebuild": rebuilt_structure["depth"],
            "snapshot_recovery": recovery_structure["depth"],
        },
        "total_pages": {
            "continuous": continuous_structure["total_pages"],
            "incremental": incremental_structure["total_pages"],
            "full_rebuild": rebuilt_structure["total_pages"],
            "snapshot_recovery": recovery_structure["total_pages"],
        },
        "leaf_chain_connected": {
            "continuous": continuous_structure["leaf_chain_connected"],
            "incremental": incremental_structure["leaf_chain_connected"],
            "full_rebuild": rebuilt_structure["leaf_chain_connected"],
            "snapshot_recovery": recovery_structure["leaf_chain_connected"],
        },
    }
    return {
        "index": index_definition.name,
        "base_batch_count": len(base_batches),
        "appended_batch_id": final_batch.batch_id,
        "appended_record_count": len(final_batch.records),
        "base_snapshot_path": str(base_snapshot_path.relative_to(config.output_dir)),
        "incremental_vs_full_rebuild": {
            "consistent": all(
                check["consistent"] for check in incremental_checks
            )
            and structural_comparison["incremental_matches_full_rebuild"],
            "queries": incremental_checks,
        },
        "snapshot_recovery_vs_continuous": {
            "consistent": all(
                check["consistent"] for check in recovery_checks
            )
            and structural_comparison["recovery_matches_continuous"],
            "queries": recovery_checks,
        },
        "structural_comparison": structural_comparison,
    }


def _choose_indexes(
    metrics: list[dict[str, Any]],
) -> dict[str, Any]:
    overlap_rank = sorted(
        metrics,
        key=lambda item: (
            item["mean_scan_pages_by_kind"].get("berth_overlap", float("inf")),
            len(item["definition"]["fields"]),
            item["split_count"],
            item["total_pages"],
        ),
    )
    time_rank = sorted(
        metrics,
        key=lambda item: (
            item["mean_scan_pages_by_kind"].get(
                "berth_time_closed", float("inf")
            ),
            len(item["definition"]["fields"]),
            item["split_count"],
            item["total_pages"],
        ),
    )
    overlap_best = overlap_rank[0]
    time_best = time_rank[0]
    return {
        "primary_for_berth_overlap": overlap_best["name"],
        "primary_label": overlap_best["label"],
        "secondary_for_global_berth_time": time_best["name"],
        "secondary_label": time_best["label"],
        "overlap_ranking": [
            {
                "index": item["name"],
                "label": item["label"],
                "mean_scan_pages": item["mean_scan_pages_by_kind"].get(
                    "berth_overlap"
                ),
                "split_count": item["split_count"],
                "average_leaf_fill_ratio": item["leaf_fill_statistics"][
                    "average_fill_ratio"
                ],
            }
            for item in overlap_rank
        ],
        "global_time_ranking": [
            {
                "index": item["name"],
                "label": item["label"],
                "mean_scan_pages": item["mean_scan_pages_by_kind"].get(
                    "berth_time_closed"
                ),
                "split_count": item["split_count"],
                "average_leaf_fill_ratio": item["leaf_fill_statistics"][
                    "average_fill_ratio"
                ],
            }
            for item in time_rank
        ],
        "rationale": [
            (
                f"泊位重叠查询平均扫描 "
                f"{overlap_best['mean_scan_pages_by_kind'].get('berth_overlap')} 页，"
                f"首选 {overlap_best['label']}：前导泊位编号可直接裁剪扫描范围。"
            ),
            (
                f"全局靠泊时刻闭区间平均扫描 "
                f"{time_best['mean_scan_pages_by_kind'].get('berth_time_closed')} 页，"
                f"若此类请求占比高，应补充 {time_best['label']} 作为二级索引；"
                "扫描成本相同时优先字段更少、维护成本更低的定义。"
            ),
            (
                "所有候选索引的落点校验与全表基准核对均需通过；正确性由叶链"
                "边界与基准比较保证，不能只看访问页数。"
            ),
        ],
    }


def _write_partial_report(
    *,
    config: LabConfig,
    batches: list[Batch],
    trees: dict[str, BPlusTree],
    batch_metrics: dict[str, list[dict[str, Any]]],
    output_dir: Path,
) -> dict[str, Any]:
    report = {
        "report_version": 1,
        "status": "partial",
        "configuration": {
            "seed": config.seed,
            "batch_count": config.batches,
            "page_capacity": config.page_capacity,
            "fill_factor": config.fill_factor,
            "query_count": config.query_count,
            "stopped_after_batch": config.stop_after_batch,
        },
        "completed_batches": [
            batch.batch_id
            for batch in batches[: config.stop_after_batch or 0]
        ],
        "indexes": {
            name: {
                "structure": tree.validate_structure(),
                "split_count": tree.split_count,
                "leaf_fill_statistics": tree.leaf_fill_statistics(),
                "batch_metrics": batch_metrics[name],
                "latest_snapshot_path": str(
                    _snapshot_path(
                        output_dir,
                        name,
                        tree.last_completed_batch,
                    ).relative_to(output_dir)
                ),
            }
            for name, tree in trees.items()
        },
        "resume_hint": "使用相同参数并追加 --resume，可从各索引最近快照继续。",
    }
    write_json_atomic(output_dir / "reports" / "partial_report.json", report)
    return report


def run_lab(config: LabConfig) -> dict[str, Any]:
    config.validate()
    _prepare_output(config.output_dir, resume=config.resume)
    batches = generate_batches(
        config.seed,
        config.batches,
        config.page_capacity,
    )
    records = _flatten(batches)
    edge_cases = describe_edge_cases(batches, config.page_capacity)

    sample_report = {
        "seed": config.seed,
        "batch_count": len(batches),
        "page_capacity": config.page_capacity,
        "batch_sizes": [len(batch.records) for batch in batches],
        "write_order": [record.write_seq for record in records],
        "edge_cases": edge_cases,
        "batches": [batch.to_dict() for batch in batches],
    }
    write_json_atomic(config.output_dir / "reports" / "samples.json", sample_report)

    trees: dict[str, BPlusTree] = {}
    batch_metrics: dict[str, list[dict[str, Any]]] = {}
    for definition in INDEX_DEFINITIONS:
        tree: BPlusTree | None = None
        start_batch_index = 0
        previous_batch_metrics: list[dict[str, Any]] = []
        build_batches = (
            batches[: config.stop_after_batch]
            if config.stop_after_batch is not None
            else batches
        )
        if config.resume:
            snapshot_path, snapshot = _latest_snapshot(
                config.output_dir,
                definition.name,
            )
            if snapshot["configuration"]["seed"] != config.seed:
                raise ValueError(
                    f"snapshot seed mismatch for {definition.name}: {snapshot_path}"
                )
            if int(snapshot["configuration"]["page_capacity"]) != config.page_capacity:
                raise ValueError(
                    f"snapshot page capacity mismatch for {definition.name}"
                )
            if float(snapshot["configuration"]["fill_factor"]) != config.fill_factor:
                raise ValueError(
                    f"snapshot fill factor mismatch for {definition.name}"
                )
            snapshot_record_count = int(snapshot["record_count"])
            expected_records = [
                record.to_dict() for record in records[:snapshot_record_count]
            ]
            if snapshot["records"] != expected_records:
                raise ValueError(
                    f"snapshot records are not a prefix of the requested run "
                    f"for {definition.name}"
                )
            if not 0 <= int(snapshot["last_completed_batch"]) <= config.batches:
                raise ValueError(
                    f"snapshot batch is outside the requested run for {definition.name}"
                )
            tree = BPlusTree.from_snapshot(snapshot, definition)
            start_batch_index = tree.last_completed_batch
            previous_batch_metrics = _batch_metrics_from_snapshots(
                config.output_dir,
                definition.name,
                tree.last_completed_batch,
            )
        tree, new_batch_metrics = _build_with_snapshots(
            index_definition=definition,
            batches=build_batches,
            config=config,
            start_batch_index=start_batch_index,
            tree=tree,
        )
        trees[definition.name] = tree
        batch_metrics[definition.name] = previous_batch_metrics + new_batch_metrics

    if config.stop_after_batch is not None:
        partial = _write_partial_report(
            config=config,
            batches=batches,
            trees=trees,
            batch_metrics=batch_metrics,
            output_dir=config.output_dir,
        )
        return partial

    queries = generate_queries(records, config.seed, config.query_count)
    write_json_atomic(
        config.output_dir / "reports" / "queries.json",
        {
            "seed": config.seed,
            "query_count": len(queries),
            "queries": [query.to_dict() for query in queries],
        },
    )

    index_reports: dict[str, Any] = {}
    index_metrics: list[dict[str, Any]] = []
    all_comparisons: list[dict[str, Any]] = []

    for definition in INDEX_DEFINITIONS:
        tree = trees[definition.name]
        results, comparisons = _query_result_report(
            tree=tree,
            records=records,
            queries=queries,
        )
        all_comparisons.extend(comparisons)
        snapshot_path = _snapshot_path(
            config.output_dir,
            definition.name,
            batch_id=tree.last_completed_batch,
        )
        metrics = _index_metrics(
            tree=tree,
            query_results=results,
            comparisons=comparisons,
            batch_metrics=batch_metrics[definition.name],
            final_snapshot_path=snapshot_path,
            output_dir=config.output_dir,
        )
        index_metrics.append(metrics)
        index_reports[definition.name] = {
            "metrics": metrics,
            "final_structure": tree.validate_structure(),
            "final_pages": tree.page_report(),
            "split_history": tree.split_history,
            "leaf_fill_statistics": tree.leaf_fill_statistics(),
            "queries": results,
        }

    path_checks = [
        _path_consistency(
            index_definition=definition,
            batches=batches,
            records=records,
            queries=queries,
            continuous_tree=trees[definition.name],
            config=config,
        )
        for definition in INDEX_DEFINITIONS
    ]
    choice = _choose_indexes(index_metrics)
    cross_index_consistency: list[dict[str, Any]] = []
    for query in queries:
        ids_by_index = {
            definition.name: next(
                result["hit_record_ids"]
                for result in index_reports[definition.name]["queries"]
                if result["query"]["query_id"] == query.query_id
            )
            for definition in INDEX_DEFINITIONS
        }
        counters = {
            name: Counter(record_ids)
            for name, record_ids in ids_by_index.items()
        }
        reference_name = INDEX_DEFINITIONS[0].name
        consistent = all(
            counter == counters[reference_name]
            for counter in counters.values()
        )
        cross_index_consistency.append(
            {
                "query_id": query.query_id,
                "query_kind": query.kind,
                "consistent_hit_multiset": consistent,
                "record_ids_by_index": ids_by_index,
            }
        )

    boundary_evidence = {}
    for definition in INDEX_DEFINITIONS:
        snapshot_path = _snapshot_path(
            config.output_dir,
            definition.name,
            batch_id=batches[0].batch_id,
        )
        first_snapshot = read_json(snapshot_path)
        leaves = [
            page for page in first_snapshot["pages"] if page["page_type"] == "leaf"
        ]
        boundary_evidence[definition.name] = {
            "snapshot_path": str(snapshot_path.relative_to(config.output_dir)),
            "split_count_after_first_batch": first_snapshot["split_count"],
            "leaf_key_counts": [page["key_count"] for page in leaves],
            "leaf_capacity": config.page_capacity,
            "reached_capacity_without_split": (
                first_snapshot["split_count"] == 0
                and any(page["key_count"] == config.page_capacity for page in leaves)
            ),
        }

    acceptance_checks = {
        "at_least_three_candidate_indexes": len(INDEX_DEFINITIONS) >= 3,
        "page_capacity_boundary_sample": edge_cases[
            "page_capacity_boundary"
        ]["at_capacity"],
        "page_capacity_boundary_observed_in_tree": all(
            item["reached_capacity_without_split"]
            for item in boundary_evidence.values()
        ),
        "same_berth_time_sample_present": bool(
            edge_cases["same_berth_time_groups"]
        ),
        "zero_length_interval_sample_present": (
            edge_cases["zero_length_intervals"]["count"] > 0
        ),
        "consecutive_duplicate_berth_sample_present": bool(
            edge_cases["consecutive_duplicate_berth_runs"]
        ),
        "all_leaf_chains_connected": all(
            item["leaf_chain_connected"] for item in index_metrics
        ),
        "all_queries_match_full_table_baseline": all(
            comparison["consistent"] for comparison in all_comparisons
        ),
        "all_candidate_indexes_return_same_hit_multiset": all(
            item["consistent_hit_multiset"]
            for item in cross_index_consistency
        ),
        "all_query_landings_correct": all(
            result["landing_correct"]
            for index_report in index_reports.values()
            for result in index_report["queries"]
        ),
        "all_incremental_queries_match_full_rebuild": all(
            check["incremental_vs_full_rebuild"]["consistent"]
            for check in path_checks
        ),
        "all_recovery_queries_match_continuous": all(
            check["snapshot_recovery_vs_continuous"]["consistent"]
            for check in path_checks
        ),
        "all_recovered_structures_match_continuous": all(
            check["structural_comparison"]["recovery_matches_continuous"]
            for check in path_checks
        ),
        "all_run_paths_match_fresh_continuous": all(
            check["structural_comparison"][
                "primary_run_matches_fresh_continuous"
            ]
            for check in path_checks
        ),
    }
    passed = all(acceptance_checks.values())
    report_payload = {
        "report_version": 1,
        "status": "passed" if passed else "failed",
        "configuration": {
            "seed": config.seed,
            "batch_count": config.batches,
            "batch_sizes": [len(batch.records) for batch in batches],
            "record_count": len(records),
            "page_capacity": config.page_capacity,
            "fill_factor": config.fill_factor,
            "query_count": len(queries),
            "output_dir": str(config.output_dir),
            "run_mode": "resume" if config.resume else "fresh",
        },
        "edge_cases": edge_cases,
        "page_capacity_boundary_evidence": boundary_evidence,
        "queries": [query.to_dict() for query in queries],
        "index_comparison": {
            "metrics": index_metrics,
            "choice": choice,
            "cross_index_set_consistency": cross_index_consistency,
        },
        "indexes": index_reports,
        "path_consistency": path_checks,
        "acceptance_checks": acceptance_checks,
        "acceptance_passed": passed,
        "artifacts": {
            "samples": "reports/samples.json",
            "queries": "reports/queries.json",
            "snapshot_directory": "snapshots/",
            "final_report": "reports/final_report.json",
            "acceptance_report": "reports/acceptance_report.json",
        },
    }
    final_report_path = config.output_dir / "reports" / "final_report.json"
    write_json_atomic(final_report_path, report_payload)
    partial_report_path = config.output_dir / "reports" / "partial_report.json"
    if partial_report_path.exists():
        partial_report_path.unlink()
    acceptance_payload = {
        "status": report_payload["status"],
        "acceptance_passed": passed,
        "checks": acceptance_checks,
        "query_comparison_summary": {
            definition.name: {
                "consistent": sum(
                    1
                    for comparison in all_comparisons
                    if comparison["index"] == definition.name
                    and comparison["consistent"]
                ),
                "total": len(index_reports[definition.name]["queries"]),
            }
            for definition in INDEX_DEFINITIONS
        },
        "path_consistency": [
            {
                "index": check["index"],
                "incremental_vs_full_rebuild": check[
                    "incremental_vs_full_rebuild"
                ]["consistent"],
                "snapshot_recovery_vs_continuous": check[
                    "snapshot_recovery_vs_continuous"
                ]["consistent"],
            }
            for check in path_checks
        ],
        "report_fingerprint": hashlib.sha256(
            json.dumps(
                report_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    }
    write_json_atomic(
        config.output_dir / "reports" / "acceptance_report.json",
        acceptance_payload,
    )
    return report_payload
