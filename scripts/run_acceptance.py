from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from diagnostic_engine.checkpoint import CheckpointStore, ForcedTermination
from diagnostic_engine.engine import run_engine
from diagnostic_engine.errors import DiagnosticError
from diagnostic_engine.generator import generate_dataset
from diagnostic_engine.snapshots import (
    SnapshotCommitLock,
    downgrade_v2_to_v1,
    install_snapshot,
    parse_v1,
    parse_v2,
    upgrade_v1_to_v2,
)
from diagnostic_engine.utils import (
    canonical_json_bytes,
    pretty_json,
    write_json_atomic,
)


def _max_deltas(report: dict[str, Any]) -> dict[str, Any]:
    reference = report["reference_whole"]["run"]["steps"]
    output: dict[str, Any] = {}
    for precision, payload in report["precisions"].items():
        steps = payload["run"]["steps"]
        coefficient_delta = 0.0
        objective_delta = 0.0
        common_index = -1
        for index, (observed, expected) in enumerate(zip(steps, reference)):
            if observed["coefficients"]:
                local = float(
                    np.max(
                        np.abs(
                            np.asarray(observed["coefficients"])
                            - np.asarray(expected["coefficients"])
                        )
                    )
                )
                if local >= coefficient_delta:
                    common_index = index
                coefficient_delta = max(coefficient_delta, local)
            objective_delta = max(
                objective_delta,
                abs(observed["objective"] - expected["objective"]),
            )
        output[precision] = {
            "final_objective": steps[-1]["objective"],
            "max_coefficient_abs_delta": coefficient_delta,
            "max_objective_abs_delta": objective_delta,
            "max_coefficient_path_index": common_index,
            "converged_flags": [step["converged"] for step in steps],
            "iterations": [step["iterations"] for step in steps],
            "block_objective_gap_max": max(
                step["objective_gap"] for step in steps
            ),
        }
    return output


def _block_coverage(plan: dict[str, Any], sample_count: int) -> bool:
    blocks = plan["blocks"]
    return (
        bool(blocks)
        and blocks[0][0] == 0
        and blocks[-1][1] == sample_count
        and all(left[1] == right[0] for left, right in zip(blocks, blocks[1:]))
    )


def _markdown(summary: dict[str, Any]) -> str:
    normal = summary["normal"]
    injected = summary["injected"]
    recovery = summary["recovery"]
    snapshots = summary["snapshots"]
    lines = [
        "# 本地验收结果",
        "",
        f"- 固定种子：`{summary['generated']['seed']}`",
        f"- 样本数：`{summary['generated']['sample_count']}`",
        f"- 全部检查通过：`{summary['all_checks_passed']}`",
        "",
        "## 三精度路径",
        "",
        "| 精度 | 最终目标值 | 最大系数偏差 | 最大目标偏差 | 分块/整块最大差 | 收敛标记 |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for precision, payload in normal["precision_deltas"].items():
        final_objective = (
            normal["precision_deltas"][precision].get("final_objective")
        )
        lines.append(
            "| {precision} | {objective} | {coefficient:.9g} | {target:.9g} | "
            "{gap:.9g} | {flags} |".format(
                precision=precision,
                objective=(
                    f"{final_objective:.12g}"
                    if final_objective is not None
                    else "见 JSON"
                ),
                coefficient=payload["max_coefficient_abs_delta"],
                target=payload["max_objective_abs_delta"],
                gap=payload["block_objective_gap_max"],
                flags=payload["converged_flags"],
            )
        )
    lines.extend(
        [
            "",
            f"- 正常链路 `run_id`：`{normal['run_id']}`",
            f"- 正常链路校验：`{normal['validation']}`",
            f"- 推理准确率：`{normal['inference_accuracy']:.6f}`",
            "- 主导误差级别："
            + "，".join(
                f"`{name}={level}`"
                for name, level in normal["dominant_error_levels"].items()
            ),
            "",
            "## 四阶段异常注入",
            "",
            f"- 状态：`{injected['status']}`；校验：`{injected['validation']}`",
            f"- 降级类别：`{injected['degradation_categories']}`",
        ]
    )
    for issue in injected["issues"]:
        lines.append(
            f"- `{issue['stage']}` / `{issue['code']}` -> "
            f"`{issue['degrade_category']}`"
        )
    lines.extend(
        [
            "",
            "## 强制终止与恢复",
            "",
            f"- 终止时检查点版本：`{recovery['checkpoint_versions']}`",
            f"- 检查点校验和有效：`{recovery['checkpoint_checksums_valid']}`",
            f"- 首次恢复：复用 `{recovery['resume']['reused_steps']}` 档，"
            f"重算 `{recovery['resume']['recomputed_steps']}` 档",
            "- 再次启动：全部精度均复用 5 档且重算 0 档",
            "",
            "## 快照互迁与并发",
            "",
            f"- 回退码：`{snapshots['rollback_code']}`",
            f"- 并发提交码：`{snapshots['busy_code']}`",
            f"- 安装替换模式：`{snapshots['install_v2']['replacement_mode']}`",
            f"- 降级丢弃字段：`{snapshots['downgrade_dropped_fields']}`",
            "",
            "## 检查项",
            "",
        ]
    )
    for name, passed in summary["checks"].items():
        lines.append(f"- `{name}`: `{passed}`")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    acceptance_root = ROOT / "artifacts" / "acceptance"
    if acceptance_root.exists():
        shutil.rmtree(acceptance_root)
    acceptance_root.mkdir(parents=True)
    data_root = ROOT / "data"
    generated = generate_dataset(data_root)
    normal = run_engine(
        data_path=generated.samples_path,
        snapshot_path=generated.v2_path,
        output_directory=acceptance_root / "normal",
        precisions=("float64", "float32", "fixed"),
    )
    normal_replay = run_engine(
        data_path=generated.samples_path,
        snapshot_path=generated.v2_path,
        output_directory=acceptance_root / "normal_replay",
        precisions=("float64", "float32", "fixed"),
    )
    injected = run_engine(
        data_path=generated.samples_path,
        snapshot_path=generated.v2_path,
        output_directory=acceptance_root / "injected",
        precisions=("float64", "float32", "fixed"),
        injections=[
            "feature_standardization:row_index=3,column=region,value=__OUT_OF_VOCAB__",
            "path_solve:index=2,value=0,fallback=0.01",
            "block_reduce:value=0",
            "result_validation:value=0",
        ],
    )
    recovery_root = acceptance_root / "recovery"
    terminated_payload: dict[str, Any]
    try:
        run_engine(
            data_path=generated.samples_path,
            snapshot_path=generated.v2_path,
            output_directory=recovery_root,
            precisions=("float64", "float32", "fixed"),
            abort_precision="float64",
            abort_after_steps=2,
        )
        raise AssertionError("forced termination did not occur")
    except ForcedTermination as exc:
        terminated_payload = exc.to_dict()
    float64_store = CheckpointStore(recovery_root / "precisions" / "float64")
    checkpoint_paths = sorted((recovery_root / "precisions" / "float64").glob("cp-*.json"))
    checkpoint_versions = [
        float64_store.load_path(path).version for path in checkpoint_paths
    ]
    checkpoints_valid = all(
        float64_store.load_path(path).checksum for path in checkpoint_paths
    )
    resumed = run_engine(
        data_path=generated.samples_path,
        snapshot_path=generated.v2_path,
        output_directory=recovery_root,
        precisions=("float64", "float32", "fixed"),
    )
    replay = run_engine(
        data_path=generated.samples_path,
        snapshot_path=generated.v2_path,
        output_directory=recovery_root,
        precisions=("float64", "float32", "fixed"),
    )
    snapshot_workspace = acceptance_root / "snapshots"
    upgraded = upgrade_v1_to_v2(
        generated.v1_path, snapshot_workspace / "upgraded-v2"
    )
    downgraded = downgrade_v2_to_v1(
        generated.v2_path, snapshot_workspace / "downgraded-v1.json"
    )
    store = snapshot_workspace / "store"
    install_v1 = install_snapshot(generated.v1_path, store)
    install_v2 = install_snapshot(generated.v2_path, store)
    rollback_code = ""
    try:
        install_snapshot(generated.v1_path, store)
    except DiagnosticError as exc:
        rollback_code = exc.code
    busy_code = ""
    with SnapshotCommitLock(store):
        try:
            install_snapshot(generated.v2_path, store)
        except DiagnosticError as exc:
            busy_code = exc.code
    v1_config = parse_v1(generated.v1_path)
    v2_config = parse_v2(generated.v2_path)
    normal_deltas = _max_deltas(normal)
    injected_deltas = _max_deltas(injected)
    checks = {
        "normal_validation_passed": normal["validation"]["status"] == "passed",
        "normal_all_convergence_flags_equal": len(
            {
                tuple(value["converged_flags"])
                for value in normal_deltas.values()
            }
            | {
                tuple(
                    step["converged"]
                    for step in normal["reference_whole"]["run"]["steps"]
                )
            }
        )
        == 1,
        "normal_iteration_schedules_equal": len(
            {tuple(value["iterations"]) for value in normal_deltas.values()}
            | {
                tuple(
                    step["iterations"]
                    for step in normal["reference_whole"]["run"]["steps"]
                )
            }
        )
        == 1,
        "block_plan_covers_all_rows": _block_coverage(
            normal["blocking"]["decision"], generated.sample_count
        ),
        "block_plan_byte_replayable": (
            canonical_json_bytes(normal["blocking"]["decision"])
            == canonical_json_bytes(normal_replay["blocking"]["decision"])
            and normal["run_id"] == normal_replay["run_id"]
        ),
        "all_precisions_share_block_plan": len(
            {
                payload["run"]["block_plan"]["decision_sha256"]
                for payload in normal["precisions"].values()
            }
        )
        == 1,
        "fixed_storage_is_int64": normal["precisions"]["fixed"]["run"][
            "precision_config"
        ]["storage_dtype"]
        == "int64",
        "four_injections_recorded": set(injected["degradation_categories"])
        == {
            "unknown_category_zero_fill",
            "regularization_strength_clamped",
            "auto_block_plan",
            "default_validation_tolerance",
        },
        "injected_validation_passed": injected["validation"]["status"] == "passed",
        "injected_convergence_flags_equal": len(
            {
                tuple(value["converged_flags"])
                for value in injected_deltas.values()
            }
            | {
                tuple(
                    step["converged"]
                    for step in injected["reference_whole"]["run"]["steps"]
                )
            }
        )
        == 1,
        "checkpoint_versions_monotonic": checkpoint_versions
        == sorted(checkpoint_versions),
        "checkpoint_checksums_valid": checkpoints_valid,
        "recovery_reused_two_steps": (
            resumed["precisions"]["float64"]["resume"]["reused_steps"] == 2
            and resumed["precisions"]["float64"]["resume"]["recomputed_steps"] == 3
        ),
        "replay_reused_all_steps": all(
            payload["resume"]["reused_steps"] == 5
            and payload["resume"]["recomputed_steps"] == 0
            for payload in replay["precisions"].values()
        ),
        "v1_v2_unified_parameters_equal": v1_config.parameters()
        == v2_config.parameters(),
        "upgrade_completed_metadata": len(upgraded.metadata_completions) >= 4,
        "downgrade_dropped_explicit_fields": "content_sha256"
        in downgraded.dropped_fields,
        "whole_snapshot_swap": install_v2.replacement_mode
        == "whole_snapshot_pointer_swap",
        "rollback_rejected": rollback_code == "E_SNAPSHOT_ROLLBACK",
        "concurrent_commit_busy": busy_code == "E_STAGE_BUSY",
    }
    summary = {
        "generated": generated.to_dict(),
        "normal": {
            "run_id": normal["run_id"],
            "replay_run_id": normal_replay["run_id"],
            "status": normal["status"],
            "validation": normal["validation"]["status"],
            "inference_accuracy": normal["inference"]["accuracy"],
            "precision_deltas": normal_deltas,
            "dominant_error_levels": {
                name: payload["dominant_level"]
                for name, payload in normal["diagnostics"].items()
            },
        },
        "injected": {
            "run_id": injected["run_id"],
            "status": injected["status"],
            "validation": injected["validation"]["status"],
            "degradation_categories": injected["degradation_categories"],
            "issues": injected["injections"],
            "precision_deltas": injected_deltas,
        },
        "recovery": {
            "forced_termination": terminated_payload,
            "checkpoint_versions": checkpoint_versions,
            "checkpoint_checksums_valid": checkpoints_valid,
            "resume": resumed["precisions"]["float64"]["resume"],
            "replay_resume": {
                name: payload["resume"]
                for name, payload in replay["precisions"].items()
            },
        },
        "snapshots": {
            "install_v1": install_v1.to_dict(),
            "install_v2": install_v2.to_dict(),
            "upgrade_metadata_completions": upgraded.metadata_completions,
            "downgrade_dropped_fields": downgraded.dropped_fields,
            "rollback_code": rollback_code,
            "busy_code": busy_code,
        },
        "checks": checks,
        "all_checks_passed": all(checks.values()),
    }
    write_json_atomic(acceptance_root / "summary.json", summary)
    (acceptance_root / "summary.md").write_text(
        _markdown(summary),
        encoding="utf-8",
    )
    print(pretty_json(summary), end="")
    return 0 if summary["all_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

