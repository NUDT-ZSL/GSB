"""确定性演示：一次预算超限 + 一次强制终止恢复，并生成诊断报告。

所有输出写入独立工作目录，报告为规范化 JSON，可重复运行逐字节比对。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .dataset import generate_dataset
from .engine import BenchEngine, InterruptedRun, RunConfig
from .selfcheck import run_selfcheck
from .snapshot import canonical_payload
import json
from pathlib import Path


def write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_payload(payload) + b"\n")


def _effective_lengths(artifacts) -> dict[str, int]:
    profile = artifacts.plan.selected_profile
    lengths: dict[str, int] = {}
    for decision in artifacts.plan.selected.decisions:
        if decision.admitted:
            lengths[decision.request_id] = decision.target_length
    _ = profile
    return lengths


def run_demo(
    base_dir: str | Path,
    *,
    num_requests: int = 12,
    seed: int = 20261124,
) -> dict[str, Path]:
    base = Path(base_dir)
    data_dir = base / "data"
    validation_dir = base / "data_validation"
    budget_work = base / "run_budget"
    crash_work = base / "run_crash"
    report_path = base / "diagnostic_report.json"

    generate_dataset(data_dir, num_requests=num_requests, seed=seed)

    # ------------------------------------------------------------------
    # 输入校验演示：复制一份数据并注入非法字段，验证错误归因与请求隔离。
    # ------------------------------------------------------------------
    generate_dataset(validation_dir, num_requests=6, seed=555)
    request_path = validation_dir / "requests.jsonl"
    rows = [json.loads(line) for line in request_path.read_text(encoding="utf-8").splitlines()]
    rows[2]["target_length"] = -1
    rows[3]["seed"] = "not-an-int"
    rows[4]["sampling_params"]["top_p"] = 2.0
    request_path.write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )
    validation_engine = BenchEngine(
        validation_dir, base / "run_validation", RunConfig()
    )
    validation_report = validation_engine.validate()
    validation_engine.run(fresh=True)

    # ------------------------------------------------------------------
    # 演示一：收紧预算，使低优先级请求触发 E_STEP/CACHE/SLOT_BUDGET。
    # ------------------------------------------------------------------
    tight_config = RunConfig(
        max_workers=2,
        batch_size=2,
        step_budget=22,
        cache_budget=18,
        slot_budget=2,
        wave_budget=2,
    )
    budget_engine = BenchEngine(data_dir, budget_work, tight_config)
    budget_artifacts = budget_engine.run(fresh=True)
    rejected = budget_artifacts.plan.selected.rejected

    # ------------------------------------------------------------------
    # 演示二：充足预算下，在请求边界强制终止，再重启恢复。
    # ------------------------------------------------------------------
    crash_config = RunConfig(
        max_workers=2, batch_size=2, step_budget=10_000, cache_budget=10_000,
        slot_budget=4,
    )
    crash_engine = BenchEngine(data_dir, crash_work, crash_config)
    crash_point = 4
    interrupted: InterruptedRun | None = None
    try:
        crash_engine.run(fresh=True, crash_after_committed=crash_point)
    except InterruptedRun as exc:
        interrupted = exc

    assert interrupted is not None, "forced termination demo did not interrupt"
    partial_ids = sorted(result.request_id for result in interrupted.artifacts.results)

    resumed_engine = BenchEngine(data_dir, crash_work, crash_config)
    resumed_artifacts = resumed_engine.run(fresh=False)

    # 不间断基准运行，用于逐字节比对恢复结果。
    baseline_engine = BenchEngine(data_dir, base / "run_baseline", crash_config)
    baseline_artifacts = baseline_engine.run(fresh=True)

    byte_identical = [
        result.to_dict() for result in resumed_artifacts.results
    ] == [result.to_dict() for result in baseline_artifacts.results]

    # ------------------------------------------------------------------
    # 自检
    # ------------------------------------------------------------------
    valid_requests = resumed_engine.validate().valid_requests
    admitted_ids = resumed_artifacts.plan.selected.admitted_ids
    rejected_ids = [
        decision.request_id for decision in resumed_artifacts.plan.selected.rejected
    ]
    selfcheck = run_selfcheck(
        valid_requests=valid_requests,
        admitted_ids=admitted_ids,
        rejected_ids=rejected_ids,
        results=resumed_artifacts.results,
        ledger=resumed_artifacts.ledger,
        effective_lengths=_effective_lengths(resumed_artifacts),
    )

    report = {
        "dataset": {
            "data_dir": str(data_dir.resolve()),
            "num_requests": num_requests,
            "seed": seed,
            "vocab_version": resumed_engine.vocab.vocab_version,
        },
        "budget_demo": {
            "limits": tight_config.limits().to_dict(),
            "wave_budget": tight_config.wave_budget,
            "selected_profile": budget_artifacts.plan.selected_profile.name,
            "admitted_ids": budget_artifacts.plan.selected.admitted_ids,
            "rejected": [decision.to_dict() for decision in rejected],
            "rejection_codes": sorted({decision.code for decision in rejected}),
            "observed_rejection_codes_across_profiles": sorted(
                {
                    decision.code
                    for evaluation in budget_artifacts.plan.evaluations
                    for decision in evaluation.rejected
                }
            ),
            "per_profile_rejection_codes": {
                evaluation.profile.name: sorted(
                    {decision.code for decision in evaluation.rejected}
                )
                for evaluation in budget_artifacts.plan.evaluations
            },
            "ledger_balances": budget_artifacts.ledger.balances(),
        },
        "crash_recovery_demo": {
            "crash_after_committed": crash_point,
            "committed_before_crash": partial_ids,
            "recovery_report": resumed_artifacts.recovery.to_dict(),
            "byte_identical_to_uninterrupted": byte_identical,
        },
        "snapshots": resumed_artifacts.snapshot_versions,
        "selfcheck": selfcheck.to_dict(),
        "validation_demo": {
            "errors": validation_report.errors,
            "error_codes": sorted({error["code"] for error in validation_report.errors}),
            "valid_requests": [req.request_id for req in validation_report.valid_requests],
        },
        "recent_error_codes": _collect_error_codes(
            budget_engine,
            resumed_engine,
            baseline_engine,
            validation_engine,
        ),
    }
    # 同一工作目录重复演示时，快照版本号按设计单调递增；记录递增后的当前版本，
    # 其余业务负载（采样结果、决策、账本）仍逐字节一致。
    report["snapshot_version_note"] = (
        "snapshot versions increase monotonically across repeated runs in the "
        "same work directory; payloads remain byte-identical"
    )
    write_report(report_path, report)
    return {"report": report_path}


def _collect_error_codes(*engines: BenchEngine) -> list[str]:
    codes: list[str] = []
    for engine in engines:
        for error in engine.recent_errors:
            code = error.get("code")
            if code and code not in codes:
                codes.append(code)
    return codes
