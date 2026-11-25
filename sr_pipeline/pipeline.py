"""顶层编排：校验 -> 双精度执行 -> 诊断 -> 预算调度 -> 缓存/快照。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .cache import (
    SnapshotStore,
    build_module_keys,
    content_checksum,
    plan_invalidation,
)
from .config import PipelineConfig, load_config, load_json, load_jsonl
from .diagnose import diagnose_query, diagnosis_to_dict
from .errors import BusinessError, ErrorCode
from .precision import MODE_FIXED, PrecisionConfig
from .scheduler import (
    SchedulerReport,
    outputs_to_jsonable,
    run_scheduler,
)
from .selfcheck import check_invalidation_coverage
from .validate import validate_inputs


@dataclass
class PipelineRunResult:
    """一次完整运行的归档结果。"""

    config_checksum: str
    float_results: list[dict[str, Any]]
    fixed_results: list[dict[str, Any]]
    diagnoses: list[dict[str, Any]]
    report: dict[str, Any]
    invalidation: dict[str, Any]
    snapshots: dict[str, Any]
    self_check: dict[str, Any]


def load_bundle(sample_dir: str | Path) -> tuple[list[dict], list[dict], dict, PipelineConfig]:
    sample_dir = Path(sample_dir)
    docs = load_jsonl(sample_dir / "docs.jsonl")
    queries = load_jsonl(sample_dir / "queries.jsonl")
    term_weights = load_json(sample_dir / "term_weights.json")
    config = load_config(sample_dir / "recall_config.json")
    validate_inputs(docs, queries, term_weights, config)
    return docs, queries, term_weights, config


def _report_to_dict(report: SchedulerReport) -> dict[str, Any]:
    return asdict(report)


def run_dual_precision(
    docs: list[dict],
    queries: list[dict],
    term_weights: dict,
    config: PipelineConfig,
) -> tuple[list[dict], list[dict], list[dict], SchedulerReport]:
    """先跑 float64 参考真值，再跑定点档并逐级对账。"""
    float_config = config.with_precision(
        PrecisionConfig(mode="float64", scale=config.precision.scale,
                        rounding=config.precision.rounding)
    )
    float_outputs, float_report = run_scheduler(
        docs, queries, term_weights, float_config
    )
    float_results = outputs_to_jsonable(float_outputs, float_config)

    fixed_config = config.with_precision(
        PrecisionConfig(mode=MODE_FIXED, scale=config.precision.scale,
                        rounding=config.precision.rounding)
    )
    fixed_outputs, fixed_report = run_scheduler(
        docs, queries, term_weights, fixed_config
    )
    fixed_results = outputs_to_jsonable(fixed_outputs, fixed_config)

    float_by_query = {item.query_id: item.fusion for item in float_outputs}
    diagnoses: list[dict[str, Any]] = []
    for item in fixed_outputs:
        diagnosis = diagnose_query(
            item.query_id,
            float_by_query[item.query_id],
            item.fusion,
            fixed_config,
        )
        diagnoses.append(diagnosis_to_dict(diagnosis))
    return float_results, fixed_results, diagnoses, fixed_report


def execute_pipeline(
    sample_dir: str | Path,
    cache_db: str | Path,
) -> PipelineRunResult:
    """端到端执行，含缓存失效规划与版本化快照提交。"""
    docs, queries, term_weights, config = load_bundle(sample_dir)
    sample_dir = Path(sample_dir)

    docs_checksum = content_checksum(docs)
    weights_checksum = content_checksum(term_weights)
    route_config_checksum = content_checksum(
        [route.__dict__ for route in config.routes]
    )
    fusion_config_checksum = content_checksum(config.fusion.__dict__)
    precision_checksum = content_checksum(config.precision.__dict__)

    new_keys = build_module_keys(
        docs_checksum=docs_checksum,
        weights_checksum=weights_checksum,
        route_config_checksum=route_config_checksum,
        fusion_config_checksum=fusion_config_checksum,
        precision_checksum=precision_checksum,
    )

    float_results, fixed_results, diagnoses, report = run_dual_precision(
        docs, queries, term_weights, config
    )

    all_query_ids = [query["query_id"] for query in queries]
    with SnapshotStore(cache_db) as store:
        old_manifest = store.latest_snapshot("module_keys")
        old_keys = old_manifest["payload"] if old_manifest else {}
        plan = plan_invalidation(old_keys, new_keys, all_query_ids)
        check_invalidation_coverage(
            plan.affected_queries,
            plan.recompute_queries,
            plan.reuse_queries,
        )

        # 复用未受影响上游结果：命中缓存的查询只做读取校验
        reuse_manifest: list[dict[str, Any]] = []
        for query_id in plan.reuse_queries:
            entry = store.get_entry("fusion", f"{query_id}:{new_keys['fusion']}")
            reuse_manifest.append(
                {"query_id": query_id, "cache_hit": entry is not None}
            )

        # 提交本批重算产物（受影响后缀整体替换）
        committed_entries: list[dict[str, Any]] = []
        for query_id in plan.recompute_queries:
            fixed_payload = next(
                item for item in fixed_results if item["query_id"] == query_id
            )
            meta = store.put_entry(
                "fusion",
                f"{query_id}:{new_keys['fusion']}",
                fixed_payload,
            )
            committed_entries.append({"query_id": query_id, **meta})

        stats_snapshot = {
            "module_keys": new_keys,
            "queries": all_query_ids,
            "scheduler": _report_to_dict(report),
            "diagnoses_summary": [
                {
                    "query_id": item["query_id"],
                    "dominant_sources": item["dominant_sources"],
                    "threshold_stable": item["threshold"]["stable"],
                }
                for item in diagnoses
            ],
        }
        expected_version = old_manifest["version"] if old_manifest else None
        key_commit = store.commit_snapshot("module_keys", new_keys, expected_version)
        stats_commit = store.commit_snapshot(
            "recall_stats", stats_snapshot
        )

    self_check = {
        "status": "ok",
        "invalidation_coverage": {
            "affected": sorted(plan.affected_queries),
            "recompute": sorted(plan.recompute_queries),
            "reuse": sorted(plan.reuse_queries),
        },
        "committed_entries": len(committed_entries),
        "reuse_manifest": reuse_manifest,
    }
    return PipelineRunResult(
        config_checksum=content_checksum(config.to_dict()),
        float_results=float_results,
        fixed_results=fixed_results,
        diagnoses=diagnoses,
        report=_report_to_dict(report),
        invalidation=plan.to_dict(),
        snapshots={
            "module_keys": key_commit,
            "recall_stats": stats_commit,
        },
        self_check=self_check,
    )


def assert_budget_error(
    docs: list[dict],
    queries: list[dict],
    term_weights: dict,
    config: PipelineConfig,
    expected_code: str,
) -> BusinessError:
    """测试辅助：执行并断言抛出指定业务码。"""
    try:
        run_scheduler(docs, queries, term_weights, config)
    except BusinessError as error:
        if error.code != expected_code:
            raise
        return error
    raise BusinessError(
        ErrorCode.SELF_CHECK_FAILED,
        f"期望抛出 {expected_code} 但执行成功",
    )
