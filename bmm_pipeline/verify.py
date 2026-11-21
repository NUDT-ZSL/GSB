from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np

from .config import PipelineConfig
from .errors import error
from .jobs import JobSpec, job_from_dict
from .kernels import (
    check_tolerance,
    compare_exact,
    compute_job,
    merge_partials,
)
from .matrices import generate_operands
from .snapshots import (
    STAGES,
    SnapshotStore,
    canonical_json,
    decode_array,
    sha256_bytes,
)
from .strategy import StrategyPlan, choose_strategy, plan_from_dict


@dataclass
class VerificationReport:
    run_dir: str
    ok: bool
    checks_run: int
    checks: list[dict[str, Any]]
    issues: list[dict[str, Any]]
    summary: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_dir": self.run_dir,
            "ok": self.ok,
            "checks_run": self.checks_run,
            "checks": self.checks,
            "issues": self.issues,
            "summary": self.summary,
        }

    def raise_if_failed(self) -> None:
        if self.ok:
            return
        first = self.issues[0]
        raise error(
            "E_INCONSISTENT",
            "跨模块一致性自检失败",
            job_id=first.get("job_id"),
            field="result",
            details={
                "category": first.get("category"),
                "issue_count": len(self.issues),
                "issues": self.issues[:10],
            },
        )


class _Collector:
    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []
        self.issues: list[dict[str, Any]] = []

    def check(
        self, category: str, job_id: str | None, ok: bool, message: str
    ) -> None:
        self.checks.append(
            {
                "category": category,
                "job_id": job_id,
                "ok": bool(ok),
                "message": message,
            }
        )

    def issue(
        self,
        category: str,
        job_id: str | None,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "code": "E_INCONSISTENT",
            "category": category,
            "message": message,
        }
        if job_id is not None:
            payload["job_id"] = job_id
        if details:
            payload["details"] = details
        self.issues.append(payload)
        self.checks.append(
            {
                "category": category,
                "job_id": job_id,
                "ok": False,
                "message": message,
            }
        )


def _tiles_cover(size: int, block: int) -> bool:
    if size <= 0 or block <= 0:
        return False
    cursor = 0
    for start in range(0, size, block):
        if start != cursor:
            return False
        stop = min(size, start + block)
        if stop <= start:
            return False
        cursor = stop
    return cursor == size


def _exact_details(actual: np.ndarray, expected: np.ndarray) -> dict[str, Any]:
    actual64 = np.asarray(actual, dtype=np.float64)
    expected64 = np.asarray(expected, dtype=np.float64)
    if actual64.shape != expected64.shape:
        return {
            "actual_shape": list(actual64.shape),
            "expected_shape": list(expected64.shape),
        }
    difference = np.abs(actual64 - expected64)
    equal = (actual64 == expected64) | (
        np.isnan(actual64) & np.isnan(expected64)
    )
    locations = np.argwhere(~equal)
    first: list[dict[str, Any]] = []
    for location in locations[:10]:
        index = tuple(int(value) for value in location)
        first.append(
            {
                "index": list(index),
                "actual": float(actual64[index]),
                "expected": float(expected64[index]),
                "abs_error": float(difference[index]),
            }
        )
    return {
        "mismatch_count": int(np.count_nonzero(~equal)),
        "max_abs_error": float(np.max(difference)) if difference.size else 0.0,
        "elements": first,
    }


def _allclose_details(
    actual: np.ndarray,
    expected: np.ndarray,
    job_id: str,
    config: PipelineConfig,
) -> dict[str, Any]:
    return check_tolerance(
        actual,
        expected,
        job_id=job_id,
        atol=config.atol,
        rtol=config.rtol,
        enforce=False,
    )


def _stored_output_array(path: Path) -> np.ndarray:
    return np.asarray(np.load(path, allow_pickle=False), dtype=np.float64)


def verify_run(
    run_dir: str | Path,
    config: PipelineConfig | None = None,
) -> VerificationReport:
    store = SnapshotStore(run_dir)
    snapshots = store.load_committed()
    snapshot_by_stage = {snapshot.stage: snapshot for snapshot in snapshots}
    for stage in STAGES:
        if stage not in snapshot_by_stage:
            raise error(
                "E_STAGE_INCOMPLETE",
                "一致性自检要求四个阶段均已提交",
                stage=stage,
                version=len(snapshots),
            )
    validation = snapshot_by_stage["validation"]
    planning = snapshot_by_stage["planning"]
    accumulation = snapshot_by_stage["accumulation"]
    aggregation = snapshot_by_stage["aggregation"]
    stored_config = validation.payload.get("config")
    effective_config = config
    if effective_config is None:
        effective_config = (
            PipelineConfig(**stored_config)
            if isinstance(stored_config, dict)
            else PipelineConfig()
        )
    jobs = [job_from_dict(payload) for payload in validation.payload["jobs"]]
    job_ids = [job.job_id for job in jobs]
    plans = {
        job_id: plan_from_dict(payload)
        for job_id, payload in planning.payload["plans"].items()
    }
    results = accumulation.payload["results"]
    outputs = aggregation.payload["outputs"]
    collector = _Collector()

    for stage in STAGES:
        snapshot = snapshot_by_stage[stage]
        complete = snapshot.completed_jobs == sorted(job_ids)
        collector.check(
            "snapshot_completion",
            None,
            complete,
            f"{stage} 已提交任务集合完整",
        )
        if not complete:
            collector.issue(
                "snapshot_completion",
                None,
                f"{stage} 快照的已完成任务集合不完整",
                {
                    "stage": stage,
                    "completed": snapshot.completed_jobs,
                    "expected": sorted(job_ids),
                },
            )

    recomputed: dict[str, Any] = {}
    for job in jobs:
        plan = plans.get(job.job_id)
        if plan is None:
            collector.issue(
                "strategy_missing",
                job.job_id,
                "策略快照中缺少任务",
            )
            continue
        coverage_ok = (
            plan.dimensions
            == {
                "batch": job.batch_size,
                "m": job.m,
                "n": job.n,
                "k": job.k,
            }
            and plan.covers()
            and _tiles_cover(job.m, plan.block_size["m"])
            and _tiles_cover(job.n, plan.block_size["n"])
            and _tiles_cover(job.k, plan.block_size["k"])
        )
        collector.check(
            "strategy_coverage",
            job.job_id,
            coverage_ok,
            "分块区间完整覆盖 M/N/K",
        )
        if not coverage_ok:
            collector.issue(
                "strategy_coverage",
                job.job_id,
                "分块区间存在遗漏、重叠或维度不一致",
                {"plan": plan.to_dict(), "job": job.to_dict()},
            )
        deterministic_plan = choose_strategy(job, effective_config)
        plan_byte_identical = (
            deterministic_plan.canonical_json() == plan.canonical_json()
        )
        collector.check(
            "strategy_determinism",
            job.job_id,
            plan_byte_identical,
            "相同任务与配置下策略记录逐字节一致",
        )
        if not plan_byte_identical:
            collector.issue(
                "strategy_determinism",
                job.job_id,
                "重新生成的策略记录与已提交策略不一致",
                {
                    "stored_checksum": plan.checksum(),
                    "current_checksum": deterministic_plan.checksum(),
                },
            )

        stored_payload = results.get(job.job_id)
        if stored_payload is None:
            collector.issue(
                "result_missing",
                job.job_id,
                "累加快照缺少任务结果",
            )
            continue
        stored_result = decode_array(stored_payload["array"])
        output_record = outputs.get(job.job_id)
        output_ok = False
        if output_record is None:
            collector.issue(
                "output_missing",
                job.job_id,
                "聚合快照缺少输出记录",
            )
        else:
            output_path = store.results_dir / str(output_record["file"])
            if not output_path.exists():
                collector.issue(
                    "output_missing",
                    job.job_id,
                    "最终输出文件不存在",
                    {"output_file": str(output_path)},
                )
            else:
                raw_checksum = sha256_bytes(output_path.read_bytes())
                try:
                    output_array = _stored_output_array(output_path)
                except Exception as exc:
                    collector.issue(
                        "output_file_mismatch",
                        job.job_id,
                        "最终输出文件无法读取",
                        {
                            "error": str(exc),
                            "type": type(exc).__name__,
                            "output_file": str(output_path),
                        },
                    )
                else:
                    checksum_ok = raw_checksum == output_record["checksum"]
                    array_ok = compare_exact(output_array, stored_result)
                    output_ok = checksum_ok and array_ok
                    if not output_ok:
                        collector.issue(
                            "output_file_mismatch",
                            job.job_id,
                            "最终输出文件与已提交累加结果不一致",
                            {
                                "checksum_ok": checksum_ok,
                                "array_equal": array_ok,
                                "expected_checksum": output_record["checksum"],
                                "actual_checksum": raw_checksum,
                                "array": _exact_details(
                                    output_array, stored_result
                                ),
                            },
                        )
        collector.check(
            "output_file",
            job.job_id,
            output_ok,
            "输出文件校验和与逐元素内容可复核",
        )

        try:
            run = compute_job(
                job,
                plan,
                effective_config,
                collect_partials=True,
                enforce_tolerance=False,
            )
        except Exception as exc:
            collector.issue(
                "recompute_error",
                job.job_id,
                "独立重算失败",
                {"error": str(exc), "type": type(exc).__name__},
            )
            continue
        recomputed[job.job_id] = run
        recompute_ok = compare_exact(run.result, stored_result)
        collector.check(
            "independent_recompute",
            job.job_id,
            recompute_ok,
            "独立分块重算与已提交结果逐元素一致",
        )
        if not recompute_ok:
            collector.issue(
                "recompute_mismatch",
                job.job_id,
                "独立重算结果与已提交结果不一致",
                _exact_details(run.result, stored_result),
            )
        merged = merge_partials(
            job, plan, effective_config, run.partials or []
        )
        merge_ok = compare_exact(merged, run.result)
        collector.check(
            "partial_merge",
            job.job_id,
            merge_ok,
            "分块部分和合并可还原整体结果",
        )
        if not merge_ok:
            collector.issue(
                "partial_merge",
                job.job_id,
                "分块部分和合并结果与整体结果不一致",
                _exact_details(merged, run.result),
            )
        tolerance_ok = run.diagnostics["mismatch_count"] == 0
        collector.check(
            "reference_tolerance",
            job.job_id,
            tolerance_ok,
            "分块路径处于 float64 参考容差内",
        )
        if not tolerance_ok:
            collector.issue(
                "tolerance",
                job.job_id,
                "分块路径超出 float64 参考容差",
                {
                    "max_abs_error": run.diagnostics["max_abs_error"],
                    "mismatch_count": run.diagnostics["mismatch_count"],
                    "violations": run.diagnostics["violations"],
                },
            )

        alternate = replace(
            job,
            layout="col" if job.layout == "row" else "row",
        )
        alternate_plan = choose_strategy(alternate, effective_config)
        alternate_run = compute_job(
            alternate,
            alternate_plan,
            effective_config,
            enforce_tolerance=False,
        )
        layout_spectrum = _allclose_details(
            run.result,
            alternate_run.result,
            job.job_id,
            effective_config,
        )
        layout_ok = layout_spectrum["mismatch_count"] == 0
        collector.check(
            "row_col_layout",
            job.job_id,
            layout_ok,
            "行主序与列主序结果在容差内一致",
        )
        if not layout_ok:
            collector.issue(
                "row_col_layout",
                job.job_id,
                "行主序与列主序结果超出容差",
                layout_spectrum,
            )

        left, right = generate_operands(job)
        result_for_transpose = (
            run.result
            if job.op_kind == "bmm"
            else run.result[np.newaxis, :, :]
        )
        actual_transposed = np.swapaxes(result_for_transpose, -1, -2)
        expected_transposed = np.matmul(
            np.swapaxes(right, -1, -2),
            np.swapaxes(left, -1, -2),
        )
        if expected_transposed.ndim == 2:
            expected_transposed = expected_transposed[np.newaxis, :, :]
        transpose_spectrum = _allclose_details(
            actual_transposed,
            expected_transposed,
            job.job_id,
            effective_config,
        )
        transpose_ok = transpose_spectrum["mismatch_count"] == 0
        collector.check(
            "transpose_semantics",
            job.job_id,
            transpose_ok,
            "结果转置满足 (A@B)^T = B^T@A^T",
        )
        if not transpose_ok:
            collector.issue(
                "transpose_semantics",
                job.job_id,
                "转置语义核对失败",
                transpose_spectrum,
            )

    gemm_jobs = [job for job in jobs if job.op_kind == "gemm"]
    candidates = gemm_jobs or [
        job for job in jobs if job.op_kind == "bmm" and job.batch_size == 1
    ]
    if not candidates:
        source = jobs[0]
        synthetic_gemm = replace(
            source,
            job_id="__selfcheck_gemm",
            op_kind="gemm",
            matrix_shapes=((source.m, source.k), (source.k, source.n)),
        )
        synthetic_bmm = replace(
            source,
            job_id="__selfcheck_bmm1",
            op_kind="bmm",
            matrix_shapes=((1, source.m, source.k), (1, source.k, source.n)),
        )
        gemm_plan = choose_strategy(synthetic_gemm, effective_config)
        bmm_plan = choose_strategy(synthetic_bmm, effective_config)
        gemm_run = compute_job(
            synthetic_gemm,
            gemm_plan,
            effective_config,
            enforce_tolerance=False,
        )
        bmm_run = compute_job(
            synthetic_bmm,
            bmm_plan,
            effective_config,
            enforce_tolerance=False,
        )
        equivalent = compare_exact(gemm_run.result, bmm_run.result[0])
        collector.check(
            "gemm_bmm_batch1",
            source.job_id,
            equivalent,
            "GEMM 与 batch=1 BMM 合成复核一致",
        )
        if not equivalent:
            collector.issue(
                "gemm_bmm_batch1",
                source.job_id,
                "合成 GEMM 与 batch=1 BMM 结果不一致",
                _exact_details(bmm_run.result[0], gemm_run.result),
            )
    for base_job in candidates:
        base_run = recomputed.get(base_job.job_id)
        if base_run is None:
            continue
        if base_job.op_kind == "gemm":
            single_job = replace(
                base_job,
                op_kind="bmm",
                matrix_shapes=(
                    (1, base_job.m, base_job.k),
                    (1, base_job.k, base_job.n),
                ),
            )
            expected = base_run.result
        else:
            single_job = replace(
                base_job,
                op_kind="gemm",
                matrix_shapes=(
                    (base_job.m, base_job.k),
                    (base_job.k, base_job.n),
                ),
            )
            expected = base_run.result[0]
        single_plan = choose_strategy(single_job, effective_config)
        single_run = compute_job(
            single_job,
            single_plan,
            effective_config,
            enforce_tolerance=False,
        )
        actual = single_run.result[0] if single_job.op_kind == "bmm" else single_run.result
        equivalent = compare_exact(actual, expected)
        collector.check(
            "gemm_bmm_batch1",
            base_job.job_id,
            equivalent,
            "GEMM 与 batch=1 BMM 独立结果一致",
        )
        if not equivalent:
            collector.issue(
                "gemm_bmm_batch1",
                base_job.job_id,
                "GEMM 与 batch=1 BMM 结果不一致",
                _exact_details(actual, expected),
            )

    summary_name = aggregation.payload.get("summary_file")
    summary_path = (
        store.run_dir / str(summary_name)
        if isinstance(summary_name, str) and summary_name
        else store.run_dir / "final_results.json"
    )
    summary_ok = False
    if not summary_path.is_file():
        collector.issue(
            "summary_file",
            None,
            "final_results.json 不存在",
            {"path": str(summary_path)},
        )
    else:
        summary_bytes = summary_path.read_bytes()
        recorded_checksum = aggregation.payload.get("summary_checksum")
        checksum_ok = sha256_bytes(summary_bytes) == recorded_checksum
        try:
            summary_document = json.loads(summary_bytes.decode("utf-8"))
            content_ok = (
                canonical_json(summary_document)
                == canonical_json(aggregation.payload["summary"])
            )
        except (UnicodeDecodeError, json.JSONDecodeError):
            content_ok = False
        summary_ok = checksum_ok and content_ok
        if not summary_ok:
            collector.issue(
                "summary_file_mismatch",
                None,
                "final_results.json 与聚合阶段快照不一致",
                {
                    "checksum_ok": checksum_ok,
                    "content_equal": content_ok,
                    "recorded_checksum": recorded_checksum,
                },
            )
    collector.check(
        "summary_file",
        None,
        summary_ok,
        "逐任务摘要文件与聚合快照一致",
    )

    summary = {
        "job_count": len(jobs),
        "check_count": len(collector.checks),
        "issue_count": len(collector.issues),
        "categories": sorted({issue["category"] for issue in collector.issues}),
    }
    return VerificationReport(
        run_dir=str(Path(run_dir)),
        ok=not collector.issues,
        checks_run=len(collector.checks),
        checks=collector.checks,
        issues=collector.issues,
        summary=summary,
    )
