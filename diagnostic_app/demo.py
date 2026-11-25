from __future__ import annotations

import json
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .errors import AppError, ErrorLedger
from .samples import generate_all, sample_config
from .selfcheck import run_cross_module_self_check
from .service import DiagnosticService


def print_header(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def grid_progress(plan: dict[str, Any], ready: list[int]) -> str:
    rows = int(plan["grid"]["rows"])
    cols = int(plan["grid"]["cols"])
    ready_set = set(ready)
    lines = []
    for row in range(rows):
        cells = []
        for col in range(cols):
            index = row * cols + col
            if index == plan["center_block_index"]:
                cells.append("C" if index in ready_set else "c")
            else:
                cells.append("█" if index in ready_set else "·")
        lines.append("  " + " ".join(cells))
    return "\n".join(lines)


def run_demo(samples_dir: str | Path = "samples",
             workspace: str | Path = "artifacts/demo",
             task_id: str = "tdc-002",
             run_fault_diagnostics: bool = True) -> dict[str, Any]:
    samples_dir = Path(samples_dir)
    workspace = Path(workspace)
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True, exist_ok=True)

    print_header("0. 生成固定种子样本")
    manifest = generate_all(samples_dir)
    print(f"任务清单: {manifest['tasks_jsonl']}")
    print(f"v1 描述: {manifest['v1']['path']}")
    print(f"v2 SQLite: {manifest['v2']['database_path']}")

    ledger = ErrorLedger(workspace / "error_ledger.jsonl")
    service = DiagnosticService(
        workspace=workspace / "runtime",
        config=sample_config(),
        tasks_path=samples_dir / "tasks.jsonl",
        error_ledger=ledger,
    )
    for task in service.tasks.values():
        reference = samples_dir / "references" / f"{task['task_id']}.npy"
        if reference.exists():
            service.set_reference(task["task_id"], reference)

    print_header("1. 块网格规划与联合裁决")
    submission = service.submit_plan(task_id)
    plan = submission["plan"]
    estimate = plan["selected_estimate"]
    print(f"任务 {task_id}; 选择网格 {plan['grid']['rows']}x{plan['grid']['cols']}; "
          f"中心块 {plan['center_block_index']}")
    print(f"中心块像素数={estimate['block_pixels']}, 估算耗时="
          f"{estimate['estimated_first_block_ms']:.4f} ms, 预算="
          f"{estimate['latency_budget_ms']:.2f} ms")
    for rationale in plan["adjudication"]["rationale"]:
        print(f"  - {rationale}")
    print(f"快照版本: fusion=v{submission['fusion_snapshot']['version']}, "
          f"plan=v{submission['plan_snapshot']['version']}, "
          f"ready=v{submission['ready_snapshot']['version']}")

    print_header("2. 先请求中心区域块")
    center = service.preview_center(task_id, anchor_strength=0.18)
    timing = center["timing"]
    print(f"块 {center['block_index']} 区间 {center['bounds']['half_open_text']} 已可查询")
    print(f"实际生成循环耗时 {timing['observed_generation_ms']:.4f} ms; "
          f"估算 {timing['estimated_generation_ms']:.4f} ms; "
          f"预算内={timing['within_budget']}")
    print(f"产物哈希 {center['product_hash'][:16]}...")
    print(grid_progress(plan, service.completed_block_indexes()))

    print_header("3. 并发请求其余区域块并观察就绪进度")
    remaining = [
        int(block["block_index"]) for block in plan["blocks"]
        if int(block["block_index"]) != int(plan["center_block_index"])
    ]
    with ThreadPoolExecutor(max_workers=min(4, len(remaining))) as executor:
        futures = {executor.submit(service.preview_block, task_id, index, 0.18): index
                   for index in remaining}
        for future in as_completed(futures):
            index = futures[future]
            try:
                result = future.result()
                print(f"块 {index:02d} 就绪: {result['bounds']['half_open_text']} "
                      f"({result['timing']['observed_generation_ms']:.3f} ms)")
                print(grid_progress(plan, service.completed_block_indexes()))
            except AppError as error:
                print(f"块 {index:02d} 失败: {error.code} {error.message}")

    print_header("4. 逐字节一致性与跨模块自检")
    verification = service.verify_blocks(task_id, anchor_strength=0.18)
    self_check = run_cross_module_self_check(service, task_id)
    print(f"分块拼接 vs 全局一次生成: bytewise={verification['bytewise_equal']}, "
          f"digest={verification['global_digest'][:16]}...")
    print(f"跨模块自检: ok={self_check['ok']}; "
          f"像素多重集合={self_check['pixel_set_consistency']['order_independent_multiset_match']}; "
          f"缓存键/哈希={self_check['cache_key_and_hash_audit']['ok']}; "
          f"独立复算={self_check['independent_condition_recompute']['ok']}; "
          f"覆盖审计={self_check['reuse_recompute_coverage']['ok']}")

    fault = None
    if run_fault_diagnostics:
        print_header("5. 安全故障诊断与最近业务错误码")
        fault = service.mismatch_diagnostic(task_id)
        details = fault["details"]
        print(f"注入单像素差异后裁决: {fault['error_code']}; "
              f"首个偏差坐标={details['first_coordinate']}; block={details['block_index']}; "
              f"delta={details['delta_value']:.6g}")
        try:
            from .pipeline import build_context
            build_context(service.get_task(task_id), service.config,
                          reference=service._references[task_id], anchor_strength=9.9)  # noqa: SLF001
        except AppError as error:
            ledger.record(error)
            print(f"锚定参数诊断: {error.code}: {error.message}")

    latest_errors = ledger.all()[-5:]
    print("\n最近业务错误码:")
    if not latest_errors:
        print("  (无)")
    for item in latest_errors:
        print(f"  - {item['error_code']}: {item['message']}")

    report = {
        "ok": verification["ok"] and self_check["ok"],
        "task_id": task_id,
        "grid": plan["grid"],
        "center": {
            "block_index": center["block_index"],
            "interval": center["bounds"]["half_open_text"],
            "timing": timing,
        },
        "verification": {
            "bytewise_equal": verification["bytewise_equal"],
            "global_digest": verification["global_digest"],
        },
        "self_check_ok": self_check["ok"],
        "snapshot_versions": {
            "fusion": submission["fusion_snapshot"]["version"],
            "plan": service.snapshots.current_version("plan"),
            "ready_blocks": service.snapshots.current_version("ready_blocks"),
        },
        "recent_errors": latest_errors,
        "fault_diagnostic": fault,
    }
    report_path = workspace / "demo-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print_header("演示完成")
    print(f"报告: {report_path}")
    print(f"快照账本: {workspace / 'runtime' / 'snapshots' / 'snapshot_ledger.jsonl'}")
    print(f"错误账本: {workspace / 'error_ledger.jsonl'}")
    return report
