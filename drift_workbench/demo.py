"""Deterministic, repeatable teaching/troubleshooting demonstration.

Scenarios, in the required order:
  1. first full convergence (with an injected fault and failure isolation);
  2. incremental change of a referenced resource, proving artifact versions
     are preserved except for the changed/propagated set;
  3. deletion of an unreferenced resource plus an E_REF rejection demo.

All generated files land under ``runtime/`` and every printed order is
deterministic for identical inputs.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

from .engine.drift import build_plan, compute_drifts
from .engine.engine import run_convergence
from .engine.errors import (
    CycleError,
    DeclarationError,
    ReferenceError,
    ReplayError,
)
from .engine.loader import load_declaration_directory
from .engine.snapshot import restore_snapshot, save_snapshot
from .engine.workspace import Workspace
from .gui.presentation import render_diagnosis_lines

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_DECL = PROJECT_ROOT / "drift_workbench" / "samples" / "declarations"
SAMPLE_ACTUAL = PROJECT_ROOT / "drift_workbench" / "samples" / "actual" / "snapshot_v1.json"
INVALID_ROOT = PROJECT_ROOT / "drift_workbench" / "samples" / "invalid"
RUNTIME = PROJECT_ROOT / "runtime"


def _write_json(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, indent=2, ensure_ascii=False)
        handle.write("\n")
    return path


def _fresh_workspace(actual_name: str = "actual.json") -> Workspace:
    decl_dir = RUNTIME / "demo_declarations"
    if decl_dir.exists():
        shutil.rmtree(decl_dir)
    shutil.copytree(SAMPLE_DECL, decl_dir)
    actual_path = RUNTIME / actual_name
    shutil.copyfile(SAMPLE_ACTUAL, actual_path)
    return Workspace.from_directories(decl_dir, actual_path)


def _banner(title: str) -> list[str]:
    line = "=" * 78
    return [line, title, line]


def _artifact_versions(workspace: Workspace) -> dict[str, int]:
    view = workspace.current_view
    return {
        key: view.artifacts[key].artifact_version
        for key in sorted(view.artifacts)
    }


def scenario_one_full_convergence() -> tuple[Workspace, list[str]]:
    lines = _banner("场景 1：首次全量收敛（注入 db-primary 故障，验证失败隔离与回滚策略）")
    workspace = _fresh_workspace("scenario1_actual.json")
    drifts = compute_drifts(workspace)
    plan = build_plan(workspace)
    lines.append(f"漂移条目数={len(drifts)}")
    for drift in drifts:
        data = drift.to_dict()
        lines.append(
            f"  drift {data['resource_key']} {data['drift_type']} "
            f"{data['property_name']} expected={data['expected']!r} actual={data['actual']!r}"
        )
    lines.append("计划拓扑序=" + " -> ".join(action.resource_key for action in plan))

    result = run_convergence(
        workspace,
        faults={"db-primary": "simulated storage provision timeout"},
    )
    lines.extend(render_diagnosis_lines(workspace, result))
    lines.append("失败隔离结论：db-primary 失败，下游 svc-order/svc-report 按策略跳过。")
    lines.append("db-primary 显式 rollback_on_failure=true，已成功资源进入回滚集合（不改变失败/跳过集合）。")

    # Re-run without faults to demonstrate deterministic full convergence.
    result_ok = run_convergence(workspace)
    lines.append("故障消除后二次收敛顺序=" + " -> ".join(result_ok.order))
    lines.append(f"二次收敛后剩余漂移={len(compute_drifts(workspace))}")
    snapshot_path = save_snapshot(
        workspace, RUNTIME / "scenario1_snapshot.json", result=result_ok
    )
    lines.append(f"快照已保存：{snapshot_path}")
    return workspace, lines


def scenario_two_incremental(workspace: Workspace) -> tuple[Workspace, list[str]]:
    lines = _banner("场景 2：增量修改被引用资源 subnet-app，验证不可变产物与失效传播")
    before = _artifact_versions(workspace)
    lines.append("变更前产物版本=" + json.dumps(before, sort_keys=True))

    latest = workspace.declaration_history["network"][-1]
    subnet = next(resource for resource in latest.resources if resource.key == "subnet-app")
    changed = replace(
        subnet,
        properties={**subnet.properties, "cidr": "10.0.20.0/24"},
    )
    next_document = replace(
        latest,
        version=latest.version + 1,
        resources=tuple(
            changed if resource.key == "subnet-app" else resource
            for resource in latest.resources
        ),
        source_file=str(RUNTIME / "demo_declarations" / "01_network_v2.json"),
    )
    _write_json(
        RUNTIME / "demo_declarations" / "01_network_v2.json",
        next_document.to_dict(),
    )
    workspace.submit_version(next_document)
    view = workspace.current_view
    after = _artifact_versions(workspace)
    lines.append("变更后产物版本=" + json.dumps(after, sort_keys=True))
    preserved = sorted(key for key in before if before[key] == after.get(key))
    advanced = sorted(key for key in before if before[key] != after.get(key))
    lines.append("产物版本保持不变=" + ",".join(preserved))
    lines.append("产物版本递增=" + ",".join(advanced))
    lines.append("当前视图整体替换：view_id=" + str(view.view_id))
    lines.append("旧视图保留数量=" + str(len(workspace.views)) + "（可在 GUI 视图对比中查看）")
    lines.append("重编译资源=" + ",".join(view.changed_keys))
    lines.append("失效传播集合=" + ",".join(view.invalidated_keys))
    for key in sorted(view.artifacts):
        artifact = view.artifacts[key]
        if artifact.invalid:
            lines.append(
                f"  失效 {key} 触发上游={artifact.invalidated_by} "
                f"路径={'/'.join(artifact.invalidation_path)}"
            )

    plan = build_plan(workspace)
    lines.append("连锁收敛顺序=" + " -> ".join(action.resource_key for action in plan))
    result = run_convergence(workspace)
    lines.append("执行顺序=" + " -> ".join(result.order))
    lines.append("业务码序列=" + ",".join(trace.code for trace in result.traces))

    snapshot_path = RUNTIME / "scenario2_snapshot.json"
    save_snapshot(workspace, snapshot_path, result=result)
    restored = Workspace()
    report = restore_snapshot(restored, snapshot_path)
    lines.append(
        "快照恢复（整体替换）状态="
        + report["status"]
        + " 声明组版本="
        + json.dumps(restored.current_view.group_versions, sort_keys=True)
    )
    lines.append("重放校验产物版本=" + json.dumps(report["artifact_versions"], sort_keys=True))

    # Continue submitting a new incremental version after restore.
    restored_latest = restored.declaration_history["network"][-1]
    vpc = next(resource for resource in restored_latest.resources if resource.key == "vpc-main")
    vpc_changed = replace(vpc, properties={**vpc.properties, "region": "cn-local-2"})
    restored_next = replace(
        restored_latest,
        version=restored_latest.version + 1,
        resources=tuple(
            vpc_changed if resource.key == "vpc-main" else resource
            for resource in restored_latest.resources
        ),
        source_file=str(RUNTIME / "demo_declarations" / "01_network_v3_restore.json"),
    )
    restored.submit_version(restored_next)
    lines.append(
        "恢复后继续增量提交：group_versions="
        + json.dumps(restored.current_view.group_versions, sort_keys=True)
        + " changed="
        + ",".join(restored.current_view.changed_keys)
    )
    return workspace, lines


def scenario_three_delete(workspace: Workspace) -> tuple[Workspace, list[str]]:
    lines = _banner("场景 3：删除无引用资源，并演示 E_REF 删除保护")
    # legacy-monitor was an extra actual resource and has already been removed
    # by convergence. Introduce a fresh unreferenced declaration resource.
    latest = workspace.declaration_history["app"][-1]
    from .engine.models import ConvergencePolicy, ResourceDeclaration

    temporary = ResourceDeclaration(
        key="job-oneoff",
        properties={"schedule": "0 2 * * *", "timeout_s": 600},
        references=(),
        policy=ConvergencePolicy(),
    )
    added_document = replace(
        latest,
        version=latest.version + 1,
        resources=latest.resources + (temporary,),
        source_file=str(RUNTIME / "demo_declarations" / "03_app_v2_oneoff.json"),
    )
    workspace.submit_version(added_document)
    lines.append("新增无引用资源后节点=" + ",".join(workspace.current_view.graph.nodes))

    try:
        workspace.delete_resource("subnet-app")
    except ReferenceError as exc:
        lines.append(
            f"删除被引用资源被拒绝：{exc.code} "
            f"resource={exc.details['resource_key']} referrers={','.join(exc.details['referrers'])}"
        )

    workspace.delete_resource("job-oneoff")
    lines.append("删除无引用资源后节点=" + ",".join(workspace.current_view.graph.nodes))
    lines.append(
        "最新声明组版本="
        + json.dumps(workspace.current_view.group_versions, sort_keys=True)
    )
    result = run_convergence(workspace)
    lines.append("最终收敛顺序=" + " -> ".join(result.order))
    lines.append("最终剩余漂移=" + str(len(compute_drifts(workspace))))
    final_snapshot = RUNTIME / "scenario3_snapshot.json"
    save_snapshot(workspace, final_snapshot, result=result)
    lines.append(f"最终快照：{final_snapshot}")
    return workspace, lines


def error_code_demonstration() -> list[str]:
    lines = _banner("附加：业务码 E_DECL / E_CYCLE / E_REPLAY 确定性演示")
    bad_type = INVALID_ROOT / "decl_bad_type"
    try:
        load_declaration_directory(bad_type)
    except DeclarationError as exc:
        lines.append(
            f"{exc.code} file={exc.details.get('file')} "
            f"field={exc.details.get('field_path')} message={exc.message}"
        )

    bad_ref = INVALID_ROOT / "decl_bad_ref"
    try:
        Workspace.from_directories(bad_ref, SAMPLE_ACTUAL)
    except DeclarationError as exc:
        lines.append(
            f"{exc.code} file={exc.details.get('file')} "
            f"field={exc.details.get('field_path')} message={exc.message}"
        )

    cycle_dir = INVALID_ROOT / "decl_cycle"
    try:
        Workspace.from_directories(cycle_dir, SAMPLE_ACTUAL)
    except CycleError as exc:
        lines.append(f"{exc.code} cycle={' -> '.join(exc.details['cycle'])}")

    workspace = _fresh_workspace("error_actual.json")
    run_convergence(workspace)
    snapshot_path = RUNTIME / "tamper_demo_snapshot.json"
    save_snapshot(workspace, snapshot_path)
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    first_key = sorted(payload["artifacts"])[0]
    payload["artifacts"][first_key]["artifact_version"] += 99
    tampered_path = RUNTIME / "tamper_demo_snapshot.broken.json"
    _write_json(tampered_path, payload)
    try:
        restore_snapshot(Workspace(), tampered_path)
    except ReplayError as exc:
        lines.append(
            f"{exc.code} resource={exc.details.get('resource_key')} "
            f"reason={exc.details.get('reason')} message={exc.message}"
        )
    return lines


def run_demo() -> str:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    workspace, lines_one = scenario_one_full_convergence()
    workspace, lines_two = scenario_two_incremental(workspace)
    _workspace, lines_three = scenario_three_delete(workspace)
    lines_error = error_code_demonstration()
    summary = _banner("稳定诊断总结")
    summary.extend(
        [
            "1. 漂移按属性粒度区分为 missing/value_mismatch/extra，动作按拓扑序+资源键稳定输出。",
            "2. 增量声明只重编译指纹变化资源，未变化资源 artifact_version 严格保持。",
            "3. 上游修改经依赖图传播到全部直接/间接下游并标记失效，删除被引用资源返回 E_REF。",
            "4. 收敛失败按策略隔离下游并记录 E_CONVERGE；显式回滚不改变失败/跳过集合。",
            "5. 快照保存完整声明历史、图版本、产物版本、漂移集合与轨迹，恢复为整体替换。",
            "6. 恢复前执行一致性重放，版本缺失/引用消失/产物版本不一致均以 E_REPLAY 报告。",
        ]
    )
    all_lines = lines_one + [""] + lines_two + [""] + lines_three + [""] + lines_error + [""] + summary
    report = "\n".join(all_lines)
    report_path = RUNTIME / "demo_report.txt"
    report_path.write_text(report + "\n", encoding="utf-8")
    return report


def main() -> int:
    print(run_demo())
    print(f"\n诊断报告已写入：{RUNTIME / 'demo_report.txt'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

