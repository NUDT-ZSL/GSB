"""Reproducible end-to-end experiment and report generation."""

from __future__ import annotations

import copy
import shutil
from pathlib import Path
from typing import Any

from .budget import BudgetPolicy, admit_budget
from .compare import compare_paths
from .errors import (
    E_INCONSISTENT,
    E_MATCH_BUDGET,
    E_PATH_INCOMPARABLE,
    E_RESOURCE_LIMIT,
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    QUError,
)
from .metrics import evaluate_results, recompute_metrics
from .pipeline import PATH_APPROX, PATH_EXACT, QueryEngine
from .scale import ScalePolicy, choose_path, observe_scale
from .selfcheck import self_check
from .snapshots import SnapshotManager, write_corrupt_snapshot
from .tokenize import tokenize_text
from .util import (
    canonical_json,
    environment_info,
    file_sha256,
    read_json,
    read_jsonl,
    sha256_obj,
    write_json,
    write_text,
    with_checksum,
)


def verify_data_manifest(data_dir: str | Path) -> dict[str, Any]:
    root = Path(data_dir)
    manifest = read_json(root / "manifest.json")
    file_results = {}
    ok = True
    for name, expected in manifest["files"].items():
        path = root / name
        actual = file_sha256(path) if path.exists() else None
        matches = actual == expected["sha256"]
        ok = ok and matches
        file_results[name] = {
            "expected_sha256": expected["sha256"],
            "actual_sha256": actual,
            "bytes": path.stat().st_size if path.exists() else None,
            "matches": matches,
        }
    return {
        "ok": ok,
        "seed": manifest["seed"],
        "generator_version": manifest["generator_version"],
        "small_query_count": manifest["small_query_count"],
        "large_query_count": manifest["large_query_count"],
        "dictionary_entry_count": manifest["dictionary_entry_count"],
        "manifest_checksum": manifest["manifest_checksum"],
        "files": file_results,
    }


def _coverage(queries: list[dict[str, Any]], results: list[dict[str, Any]]) -> dict[str, Any]:
    expected = [str(query["query_id"]) for query in queries]
    actual = [str(result["query_id"]) for result in results]
    missing = sorted(set(expected) - set(actual))
    duplicate_count = len(actual) - len(set(actual))
    return {
        "complete": not missing and duplicate_count == 0 and len(actual) == len(expected),
        "expected_count": len(expected),
        "actual_count": len(actual),
        "missing_query_ids": missing,
        "duplicate_count": duplicate_count,
    }


def _token_observation(
    engine: QueryEngine,
    queries: list[dict[str, Any]],
) -> tuple[int, int]:
    total_tokens = 0
    mismatched_normalization = 0
    for query in queries:
        normalized = engine.mappings.normalize(query["query_text"])
        tokens = tokenize_text(normalized, engine.dictionary)
        total_tokens += len(tokens)
    return total_tokens, mismatched_normalization


def _make_observation(
    engine: QueryEngine,
    queries: list[dict[str, Any]],
    config: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    total_tokens, _ = _token_observation(engine, queries)
    evaluation = config["evaluation"]
    observation = observe_scale(
        query_count=len(queries),
        dictionary_entry_count=len(engine.dictionary.entries),
        total_tokens=total_tokens,
        memory_limit_mb=float(config["budget_policy"]["memory_limit_mb"]),
        resident_memory_mb=float(evaluation["resident_memory_mb"]),
        memory_reserve_mb=float(evaluation["memory_reserve_mb"]),
        resident_memory_source="fixed_config_for_reproducibility",
    )
    return observation, total_tokens


def _result_set_checksum(results: list[dict[str, Any]]) -> str:
    return sha256_obj(
        [
            {
                "query_id": result["query_id"],
                "result_checksum": result["result_checksum"],
            }
            for result in results
        ]
    )


def _snapshot_demo(
    data_dir: Path,
    report_dir: Path,
) -> dict[str, Any]:
    snapshot_root = report_dir / "snapshot_demo"
    if snapshot_root.exists():
        shutil.rmtree(snapshot_root)
    manager = SnapshotManager(snapshot_root)
    taxonomy = read_json(data_dir / "taxonomy.json")
    dictionary = read_json(data_dir / "dictionary.json")
    config = read_json(data_dir / "config.json")
    mappings = read_json(data_dir / "mappings.json")
    bundle_v1 = {
        "taxonomy": taxonomy,
        "dictionary": dictionary,
        "config": config,
        "mappings": mappings,
    }
    path_v1 = manager.save(1, bundle_v1)
    activated_v1 = manager.activate(1)
    active_v1_checksum = manager.current_checksum

    taxonomy_v2 = copy.deepcopy(taxonomy)
    taxonomy_v2["version"] = 2
    taxonomy_v2 = with_checksum(taxonomy_v2)
    dictionary_v2 = copy.deepcopy(dictionary)
    dictionary_v2["version"] = 2
    removed_entry = dictionary_v2["entries"].pop()
    dictionary_v2 = with_checksum(dictionary_v2)
    config_v2 = copy.deepcopy(config)
    config_v2["version"] = 2
    config_v2 = with_checksum(config_v2)
    mappings_v2 = copy.deepcopy(mappings)
    mappings_v2["version"] = 2
    mappings_v2 = with_checksum(mappings_v2)
    bundle_v2 = {
        "taxonomy": taxonomy_v2,
        "dictionary": dictionary_v2,
        "config": config_v2,
        "mappings": mappings_v2,
    }
    path_v2 = manager.save(2, bundle_v2)
    activated_v2 = manager.activate(2)

    rollback: dict[str, Any]
    try:
        manager.activate(1)
        rollback = {"ok": True, "error_code": None}
    except QUError as error:
        rollback = {
            "ok": False,
            "error_code": error.code,
            "category": error.category,
            "active_version": manager.current_version,
        }

    overwrite: dict[str, Any]
    different_v1 = copy.deepcopy(bundle_v1)
    different_v1["config"] = copy.deepcopy(config)
    different_v1["config"]["strict_self_check"] = False
    different_v1["config"] = with_checksum(different_v1["config"])
    try:
        manager.save(1, different_v1)
        overwrite = {"ok": True, "error_code": None}
    except QUError as error:
        overwrite = {
            "ok": False,
            "error_code": error.code,
            "category": error.category,
        }

    corrupt_path = manager.path_for(3)
    write_corrupt_snapshot(corrupt_path, 3, bundle_v2)
    corrupt: dict[str, Any]
    try:
        manager.load_file(corrupt_path, 3)
        corrupt = {"ok": True, "error_code": None}
    except QUError as error:
        corrupt = {
            "ok": False,
            "error_code": error.code,
            "category": error.category,
        }

    active_entry_ids = {
        entry["entry_id"]
        for entry in manager.active_bundle()["dictionary"]["entries"]
    }
    reloaded_engine = QueryEngine.from_bundle(manager.active_bundle())
    return {
        "ok": (
            not rollback["ok"]
            and rollback["error_code"] == E_SNAPSHOT_ROLLBACK
            and not overwrite["ok"]
            and overwrite["error_code"] == E_SNAPSHOT_ROLLBACK
            and not corrupt["ok"]
            and corrupt["error_code"] == E_SNAPSHOT_CORRUPT
            and removed_entry["entry_id"] not in active_entry_ids
            and len(active_entry_ids) == len(dictionary["entries"]) - 1
            and len(reloaded_engine.dictionary.entries) == len(active_entry_ids)
            and reloaded_engine.taxonomy.version == 2
            and reloaded_engine.mappings.version == 2
        ),
        "version_1_file": str(path_v1.relative_to(report_dir)),
        "version_2_file": str(path_v2.relative_to(report_dir)),
        "version_1_activated": manager.current_version == 2
        and activated_v1["version"] == 1,
        "version_1_checksum": active_v1_checksum,
        "version_2_checksum": activated_v2["content_checksum"],
        "active_version": manager.current_version,
        "version_1_entry_count": len(dictionary["entries"]),
        "version_2_entry_count": len(active_entry_ids),
        "removed_entry_id": removed_entry["entry_id"],
        "removed_entry_absent_after_replace": removed_entry["entry_id"]
        not in active_entry_ids,
        "whole_replacement_not_incremental_merge": len(active_entry_ids)
        == len(dictionary["entries"]) - 1,
        "engine_reloaded_from_active_snapshot": {
            "ok": True,
            "taxonomy_version": reloaded_engine.taxonomy.version,
            "dictionary_version": reloaded_engine.dictionary.version,
            "mappings_version": reloaded_engine.mappings.version,
            "dictionary_entry_count": len(reloaded_engine.dictionary.entries),
        },
        "rollback_attempt": rollback,
        "old_content_overwrite_attempt": overwrite,
        "corrupt_snapshot_attempt": corrupt,
    }


def _fault_demo(
    engine: QueryEngine,
    sample_query: dict[str, Any],
) -> dict[str, Any]:
    stages = ["normalize", "tokenize", "slot", "intent"]
    records: dict[str, Any] = {}
    for stage in stages:
        result = engine.process(sample_query, inject={"stage": stage})
        records[stage] = {
            "faults": result["faults"],
            "degradation_categories": result["degradation_categories"],
            "self_check_ok": result["self_check"]["ok"],
            "intent_class": result["intent_class"],
            "token_count": len(result["tokens"]),
            "slot_count": len(result["slots"]),
        }
    input_triggered = dict(sample_query)
    input_triggered["_fault_stage"] = "slot"
    input_triggered["_fault_code"] = "E_INJECT_SLOT_INPUT"
    input_result = engine.process(input_triggered)
    records["input_triggered_slot"] = {
        "faults": input_result["faults"],
        "degradation_categories": input_result["degradation_categories"],
        "self_check_ok": input_result["self_check"]["ok"],
    }

    corrupted = engine.process(sample_query)
    corrupted = copy.deepcopy(corrupted)
    corrupted["tokens"][0]["text"] = "不一致"
    try:
        self_check(corrupted, engine.taxonomy)
        inconsistent = {"ok": True, "error_code": None}
    except QUError as error:
        inconsistent = {
            "ok": False,
            "error_code": error.code,
            "category": error.category,
            "issue_count": error.details["issue_count"],
        }
    expected = {stage: True for stage in stages}
    ok = all(
        records[stage]["self_check_ok"]
        and records[stage]["degradation_categories"]
        and records[stage]["faults"]
        for stage in stages
    )
    ok = (
        ok
        and not inconsistent["ok"]
        and inconsistent["error_code"] == E_INCONSISTENT
        and input_result["self_check"]["ok"]
    )
    return {
        "ok": ok,
        "expected_stage_coverage": expected,
        "stages": records,
        "self_check_inconsistency": inconsistent,
    }


def _budget_demo(
    observation: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    base = BudgetPolicy.from_dict(config["budget_policy"])
    tight_comparison = BudgetPolicy(
        total_compare_budget=2_000,
        memory_limit_mb=base.memory_limit_mb,
        approximate_candidate_limit=base.approximate_candidate_limit,
        minimum_batch_size=base.minimum_batch_size,
        priority_order=base.priority_order,
    )
    tight_memory_observation = copy.deepcopy(observation)
    tight_memory_observation["memory_available_mb"] = 0.01
    scenarios = {
        "normal": admit_budget(
            observation, PATH_APPROX, base, allow_downbatch=True
        ),
        "comparison_downbatch": admit_budget(
            observation, PATH_APPROX, tight_comparison, allow_downbatch=True
        ),
        "comparison_no_downbatch": admit_budget(
            observation, PATH_APPROX, tight_comparison, allow_downbatch=False
        ),
        "memory_reject": admit_budget(
            tight_memory_observation, PATH_APPROX, base, allow_downbatch=True
        ),
    }
    ok = (
        scenarios["normal"]["status"] == "accepted"
        and scenarios["comparison_downbatch"]["status"] == "downbatch"
        and scenarios["comparison_no_downbatch"]["error_code"] == E_MATCH_BUDGET
        and scenarios["memory_reject"]["error_code"] == E_RESOURCE_LIMIT
    )
    return {
        "ok": ok,
        "priority_order": list(base.priority_order),
        "scenarios": scenarios,
    }


def _summarize_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    return {
        "k": metrics["k"],
        "click_window": metrics["click_window"],
        "confidence_policy": metrics["confidence_policy"],
        "relevance_policy": metrics["relevance_policy"],
        "aggregate": metrics["aggregate"],
    }


def _markdown(report: dict[str, Any]) -> str:
    small = report["experiments"]["small"]
    large = report["experiments"]["large"]
    comparison = report["path_comparison"]
    scale = report["scale_selection"]
    budget = report["budget"]
    snapshot = report["snapshots"]
    faults = report["fault_injection"]
    stress = report["stress_incomparable"]
    disambiguation_stress = report["stress_disambiguation_policy"]
    verification = report["verification"]
    lines = [
        "# 查询理解与意图解析可复现实验报告",
        "",
        "本报告由 `python -m query_understanding run` 确定性生成。所有指标来自本地固定样本，"
        "不包含外部网络数据。",
        "",
        "## 验收结论",
        "",
        f"- 总体通过：`{str(verification['all_passed']).lower()}`",
        f"- 小规模精确路径意图一致率：`{comparison['intent_agreement_rate']}`",
        f"- 槽位偏移差异数：`{comparison['slot_offset_difference_count']}`",
        f"- 最大置信偏差：`{comparison['confidence_bias_spectrum']['statistics']['max_abs']}`",
        f"- 小规模 NDCG@3 / MRR：`{small['metrics']['aggregate']['ndcg_at_k']}` / `{small['metrics']['aggregate']['mrr']}`",
        f"- 大规模选中路径：`{large['selected_path']}`，NDCG@3 / MRR：`{large['metrics']['aggregate']['ndcg_at_k']}` / `{large['metrics']['aggregate']['mrr']}`",
        f"- 规模取舍：小规模 `{scale['small']['selected_path']}`，大规模 `{scale['large']['selected_path']}`",
        "",
        "## 数据与环境",
        "",
        f"- Python：`{report['environment']['python_version']}`",
        f"- NumPy：`{report['environment']['numpy_version']}`",
        f"- 随机种子：`{report['data_manifest']['seed']}`",
        f"- 小/大规模查询数：`{small['query_count']}` / `{large['query_count']}`",
        f"- 词典条目数：`{report['dictionary_entry_count']}`",
        f"- 数据清单校验：`{str(report['data_manifest']['ok']).lower()}`",
        "",
        "## 两路径可比性",
        "",
        f"- 精确路径与近似路径是否可比：`{str(comparison['comparable']).lower()}`",
        f"- 意图一致率：`{comparison['intent_agreement_rate']}`",
        f"- 槽位值差异数：`{comparison['slot_value_difference_count']}`",
        f"- 指标差值（近似 - 精确）：NDCG@3 `{comparison['metrics']['delta_approx_minus_exact']['ndcg_at_k']}`，"
        f"MRR `{comparison['metrics']['delta_approx_minus_exact']['mrr']}`",
        "- 逐查询明细包含 top-k 意图、相关性等级、DCG 与 IDCG，可独立重算。",
        "",
        "## 规模取舍与预算",
        "",
        f"- 小规模观测：{canonical_json(scale['small']['observation'])}",
        f"- 小规模决策：`{scale['small']['selected_path']}`，原因：{canonical_json(scale['small']['exact_threshold_failures'])}",
        f"- 大规模观测：{canonical_json(scale['large']['observation'])}",
        f"- 大规模决策：`{scale['large']['selected_path']}`，原因：{canonical_json(scale['large']['exact_threshold_failures'])}",
        f"- 预算优先级：`{budget['priority_order']}`",
        f"- 正常准入：`{budget['scenarios']['normal']['status']}`",
        f"- 比较预算降批：`{budget['scenarios']['comparison_downbatch']['status']}`，batch_size="
        f"`{budget['scenarios']['comparison_downbatch']['batch_size']}`",
        f"- 禁止降批：`{budget['scenarios']['comparison_no_downbatch']['error_code']}`",
        f"- 内存不足：`{budget['scenarios']['memory_reject']['error_code']}`",
        "",
        "## 版本快照",
        "",
        f"- v1 条目数：`{snapshot['version_1_entry_count']}`，v2 条目数：`{snapshot['version_2_entry_count']}`",
        f"- 整体替换而非增量合并：`{str(snapshot['whole_replacement_not_incremental_merge']).lower()}`",
        f"- 回退尝试：`{snapshot['rollback_attempt']['error_code']}`",
        f"- 旧内容覆盖尝试：`{snapshot['old_content_overwrite_attempt']['error_code']}`",
        f"- 损坏快照：`{snapshot['corrupt_snapshot_attempt']['error_code']}`",
        "",
        "## 异常注入与自检",
        "",
    ]
    for stage in ["normalize", "tokenize", "slot", "intent"]:
        row = faults["stages"][stage]
        lines.append(
            f"- `{stage}`：code=`{row['faults'][0]['business_code']}`，"
            f"降级=`{row['degradation_categories'][0]}`，自检=`{row['self_check_ok']}`"
        )
    lines.extend(
        [
            f"- 自检不一致：`{faults['self_check_inconsistency']['error_code']}` / "
            f"category=`{faults['self_check_inconsistency']['category']}`",
            "",
            "## 压力场景",
            "",
            f"- 剪枝上限压到 1 后：`{stress['error_code']}`，首个不可比查询 "
            f"`{stress['first_incomparable_query']}`",
            f"- 差异归因计数：{canonical_json(stress['difference_causes'])}",
            f"- 保持候选不变、关闭近似路径点击混合后：`{disambiguation_stress['error_code']}`，"
            f"首个不可比查询 `{disambiguation_stress['first_incomparable_query']}`",
            f"- 消歧口径压力归因：{canonical_json(disambiguation_stress['difference_causes'])}",
            "",
            "## 复现",
            "",
            "- 创建环境：`python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt`",
            "- 生成样本：`.venv/bin/python scripts/generate_data.py`",
            "- 运行实验：`.venv/bin/python -m query_understanding run`",
            "- 运行测试：`.venv/bin/python -m unittest discover -s tests -v`",
            f"- 小规模确定性结果校验和：`{report['reproducibility']['small_exact_checksum']}`",
            f"- 报告核心校验和：`{report['report_core_checksum']}`",
            "",
            "完整结构化结果见 `reports/experiment_report.json`。",
            "",
        ]
    )
    return "\n".join(lines)


def run_experiments(
    data_dir: str | Path = "data",
    report_dir: str | Path = "reports",
) -> dict[str, Any]:
    data_root = Path(data_dir)
    report_root = Path(report_dir)
    report_root.mkdir(parents=True, exist_ok=True)
    manifest = verify_data_manifest(data_root)
    config = read_json(data_root / "config.json")
    engine = QueryEngine.from_files(data_root, config)
    policy = engine.click_policy
    small_queries = read_jsonl(data_root / "query_log_small.jsonl")
    large_queries = read_jsonl(data_root / "query_log_large.jsonl")

    scale_policy = ScalePolicy.from_dict(config["scale_policy"])
    budget_policy = BudgetPolicy.from_dict(config["budget_policy"])
    small_observation, small_total_tokens = _make_observation(
        engine, small_queries, config
    )
    large_observation, large_total_tokens = _make_observation(
        engine, large_queries, config
    )
    small_decision = choose_path(
        small_observation, scale_policy, budget_policy, allow_downbatch=True
    )
    large_decision = choose_path(
        large_observation, scale_policy, budget_policy, allow_downbatch=True
    )

    small_exact_results = engine.process_many(small_queries, path=PATH_EXACT)
    small_exact_metrics = evaluate_results(
        small_exact_results, small_queries, policy
    )
    small_recompute = recompute_metrics(small_exact_metrics)
    small_coverage = _coverage(small_queries, small_exact_results)

    comparison = compare_paths(engine, small_queries, click_policy=policy)
    approx_recompute = recompute_metrics(
        comparison["metrics"][PATH_APPROX]
    )
    approx_coverage = _coverage(
        small_queries,
        engine.process_many(small_queries, path=PATH_APPROX),
    )

    exact_repeat = engine.process_many(small_queries, path=PATH_EXACT)
    approx_repeat = engine.process_many(small_queries, path=PATH_APPROX)
    exact_deterministic = canonical_json(small_exact_results) == canonical_json(
        exact_repeat
    )
    approx_deterministic = canonical_json(
        engine.process_many(small_queries, path=PATH_APPROX)
    ) == canonical_json(approx_repeat)

    large_path = large_decision["selected_path"]
    large_results = engine.process_many(large_queries, path=large_path)
    large_metrics = evaluate_results(large_results, large_queries, policy)
    large_recompute = recompute_metrics(large_metrics)
    large_coverage = _coverage(large_queries, large_results)

    budget = _budget_demo(large_observation, config)
    snapshots = _snapshot_demo(data_root, report_root)
    faults = _fault_demo(engine, small_queries[0])

    stress_engine = QueryEngine.from_files(
        data_root,
        {
            **config,
            "approximate": {
                **config["approximate"],
                "max_candidates_per_token": 1,
                "max_candidate_intents": 1,
            },
        },
    )
    stress = compare_paths(stress_engine, small_queries, click_policy=policy)
    disambiguation_stress_engine = QueryEngine.from_files(
        data_root,
        {
            **config,
            "approximate_disambiguation": {
                "click_weight": 0.0,
            },
        },
    )
    disambiguation_stress = compare_paths(
        disambiguation_stress_engine, small_queries, click_policy=policy
    )

    edge_checks: dict[str, Any] = {}
    try:
        mapping_payload = read_json(data_root / "mappings.json")
        mapping_payload["traditional_sources"].append("龘")
        mapping_payload.pop("checksum", None)
        mapping_payload = with_checksum(mapping_payload)
        from .mappings import MappingTable

        MappingTable.from_payload(mapping_payload)
        edge_checks["map_missing"] = {"ok": True, "error_code": None}
    except QUError as error:
        edge_checks["map_missing"] = {
            "ok": False,
            "error_code": error.code,
            "category": error.category,
        }
    try:
        from .dictionary import Dictionary

        Dictionary.from_file(data_root / "dictionary.json", max_entries=10)
        edge_checks["dict_truncated"] = {"ok": True, "error_code": None}
    except QUError as error:
        edge_checks["dict_truncated"] = {
            "ok": False,
            "error_code": error.code,
            "category": error.category,
        }

    verification = {
        "data_manifest_integrity": manifest["ok"],
        "small_scale_selected_exact": small_decision["selected_path"] == PATH_EXACT,
        "large_scale_selected_approx": large_decision["selected_path"]
        == PATH_APPROX,
        "small_exact_coverage": small_coverage["complete"],
        "small_approx_coverage": approx_coverage["complete"],
        "large_selected_path_coverage": large_coverage["complete"],
        "small_metrics_recompute": small_recompute["consistent"],
        "large_metrics_recompute": large_recompute["consistent"],
        "approx_metrics_recompute": approx_recompute["consistent"],
        "path_intent_parity": comparison["intent_agreement_rate"] == 1.0,
        "path_slot_offset_parity": comparison["slot_offset_difference_count"] == 0,
        "path_slot_value_parity": comparison["slot_value_difference_count"] == 0,
        "path_confidence_bias_within_policy": comparison[
            "confidence_bias_spectrum"
        ]["statistics"]["max_abs"]
        <= comparison["policy"]["max_confidence_bias"],
        "paths_comparable": comparison["comparable"],
        "exact_path_byte_reproducible": exact_deterministic,
        "approx_path_byte_reproducible": approx_deterministic,
        "budget_decisions": budget["ok"],
        "snapshot_behavior": snapshots["ok"],
        "fault_injection_and_selfcheck": faults["ok"],
        "stress_returns_path_incomparable": not stress["comparable"]
        and stress["error_code"] == E_PATH_INCOMPARABLE,
        "disambiguation_stress_returns_path_incomparable": not disambiguation_stress[
            "comparable"
        ]
        and disambiguation_stress["error_code"] == E_PATH_INCOMPARABLE
        and disambiguation_stress["difference_causes"]["disambiguation_policy"] > 0,
        "map_missing_code": edge_checks["map_missing"]["error_code"]
        == "E_MAP_MISSING",
        "dict_truncated_code": edge_checks["dict_truncated"]["error_code"]
        == "E_DICT_TRUNCATED",
    }
    verification["all_passed"] = all(verification.values())

    report_core = {
        "report_schema_version": 1,
        "environment": environment_info(),
        "data_manifest": manifest,
        "dictionary_entry_count": len(engine.dictionary.entries),
        "scale_selection": {
            "small": small_decision,
            "large": large_decision,
            "small_total_tokens": small_total_tokens,
            "large_total_tokens": large_total_tokens,
        },
        "experiments": {
            "small": {
                "query_count": len(small_queries),
                "selected_path": PATH_EXACT,
                "coverage": small_coverage,
                "metrics": _summarize_metrics(small_exact_metrics),
                "metric_recompute": small_recompute,
                "result_set_checksum": _result_set_checksum(small_exact_results),
            },
            "large": {
                "query_count": len(large_queries),
                "selected_path": large_path,
                "coverage": large_coverage,
                "metrics": _summarize_metrics(large_metrics),
                "metric_recompute": large_recompute,
                "result_set_checksum": _result_set_checksum(large_results),
            },
        },
        "path_comparison": comparison,
        "budget": budget,
        "snapshots": snapshots,
        "fault_injection": faults,
        "stress_incomparable": {
            "comparable": stress["comparable"],
            "error_code": stress["error_code"],
            "first_incomparable_query": stress["first_incomparable_query"],
            "intent_agreement_rate": stress["intent_agreement_rate"],
            "intent_mismatch_count": stress["intent_mismatch_count"],
            "slot_offset_difference_count": stress[
                "slot_offset_difference_count"
            ],
            "difference_causes": stress["difference_causes"],
            "first_five_noncomparable": [
                row for row in stress["per_query"] if not row["comparable"]
            ][:5],
            "explanation": (
                "candidate_pruning_miss means the exact top intent was removed by "
                "restricted candidate pruning; disambiguation_policy means both paths "
                "retained the intent but their click/lexical reranking口径 diverged."
            ),
        },
        "stress_disambiguation_policy": {
            "comparable": disambiguation_stress["comparable"],
            "error_code": disambiguation_stress["error_code"],
            "first_incomparable_query": disambiguation_stress[
                "first_incomparable_query"
            ],
            "intent_agreement_rate": disambiguation_stress[
                "intent_agreement_rate"
            ],
            "intent_mismatch_count": disambiguation_stress[
                "intent_mismatch_count"
            ],
            "difference_causes": disambiguation_stress["difference_causes"],
            "first_five_noncomparable": [
                row
                for row in disambiguation_stress["per_query"]
                if not row["comparable"]
            ][:5],
            "explanation": (
                "candidate sets are unchanged; only the approximate path's click "
                "mix is disabled, so top-1 differences are attributed to the "
                "disambiguation policy rather than candidate pruning."
            ),
        },
        "edge_checks": edge_checks,
        "reproducibility": {
            "exact_path_byte_identical_on_repeat": exact_deterministic,
            "approx_path_byte_identical_on_repeat": approx_deterministic,
            "small_exact_checksum": _result_set_checksum(small_exact_results),
            "small_approx_checksum": _result_set_checksum(
                engine.process_many(small_queries, path=PATH_APPROX)
            ),
            "decision_checksums": {
                "small": small_decision["decision_checksum"],
                "large": large_decision["decision_checksum"],
            },
        },
        "verification": verification,
    }
    report_core_checksum = sha256_obj(report_core)
    report = {
        **report_core,
        "report_core_checksum": report_core_checksum,
    }
    write_json(report_root / "experiment_report.json", report)
    write_text(report_root / "experiment_report.md", _markdown(report))
    return report

