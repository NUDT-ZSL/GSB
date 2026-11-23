"""完整示例命令行入口。

运行：
    python -m rope_attn.demo --tasks tasks.jsonl \
        --cache .cache --state .state --out report.json

报告 JSON 含逐阶段完成计数、偏差明细、失效报告、自检结果与业务码清单，
全程仅依赖本地 NumPy 计算与本地文件，不访问网络。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

from .cache import ContentCache
from .config import RunConfig, load_tasks
from .errors import BusinessError
from .invalidate import run_block_size_change, run_kv_change
from .pipeline import Executor
from .selfcheck import run_self_check


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="RoPE 分块注意力算子完整示例"
    )
    parser.add_argument("--tasks", required=True, help="任务清单 JSONL 路径")
    parser.add_argument("--cache", required=True, help="内容寻址缓存目录")
    parser.add_argument("--state", required=True, help="快照状态目录")
    parser.add_argument("--out", required=True, help="报告输出 JSON 路径")
    parser.add_argument("--workers", type=int, default=2, help="进程池大小")
    parser.add_argument(
        "--resource-policy",
        choices=("error", "degrade"),
        default="degrade",
        help="资源超限策略",
    )
    parser.add_argument(
        "--kill-after-stage",
        type=int,
        default=None,
        help="测试钩子：提交指定数量阶段后模拟强制终止",
    )
    parser.add_argument(
        "--interrupt-after-stage",
        type=int,
        default=None,
        help="测试钩子：提交指定数量阶段后模拟 KeyboardInterrupt",
    )
    return parser.parse_args(argv)


def build_config(args: argparse.Namespace) -> RunConfig:
    return RunConfig(
        cache_dir=args.cache,
        state_dir=args.state,
        workers=args.workers,
        resource_policy=args.resource_policy,
    )


def main(argv=None) -> int:
    args = parse_args(argv)
    report: dict = {"business_codes": []}
    try:
        tasks = load_tasks(args.tasks)
        config = build_config(args)

        executor = Executor(
            tasks,
            config,
            kill_after_stage=args.kill_after_stage,
            interrupt_after_stage=args.interrupt_after_stage,
        )
        run_result = executor.run()
        report["pipeline"] = run_result.to_dict()
        report["business_codes"].extend(run_result.business_codes)

        if run_result.status == "completed":
            # 失效演示在独立缓存/状态目录进行，避免污染主流水线缓存，
            # 保证恢复运行与不间断运行的 output_digest 逐字节可比。
            isolated = _isolated_config(config)
            report["invalidation"] = _demonstrate_invalidation(
                tasks, isolated
            )
            report["cache_inspection"] = _demonstrate_inspection(
                config, tasks[0].case_id
            )
            report["self_check"] = run_self_check(
                tasks, config.cache_dir, config.state_dir,
                tolerance=config.tolerance,
            )
        report["status"] = run_result.status
    except BusinessError as exc:
        report["status"] = "failed"
        report["business_error"] = exc.to_dict()
        report["business_codes"].append(exc.code)
    except Exception as exc:  # noqa: BLE001 - 顶层兜底，保证报告落地
        report["status"] = "failed"
        report["business_error"] = {
            "code": "E_INTERNAL",
            "message": repr(exc),
            "error_type": type(exc).__name__,
        }
        report["business_codes"].append("E_INTERNAL")

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"] != "failed" else 2


def _isolated_config(config: RunConfig) -> RunConfig:
    from dataclasses import replace

    return replace(
        config,
        cache_dir=str(Path(config.cache_dir) / "_invalidation_demo"),
        state_dir=str(Path(config.state_dir) / "_invalidation_demo"),
    )


def _demonstrate_invalidation(tasks, config: RunConfig) -> dict:
    """在第一个 case 的第 0 层演示两类增量失效传播。"""
    base_task = tasks[0]
    target_layer = 0
    # 预热：先在隔离缓存中落齐基线 DAG，复用节点才真实存在。
    from .cache import ContentCache
    from .engine import EngineContext, compute_layer

    engine_cache = ContentCache(config.cache_dir)
    compute_layer(
        EngineContext(base_task, config, engine_cache), target_layer
    )
    kv_task = replace(base_task, kv_revision=base_task.kv_revision + 1)
    kv_result = run_kv_change(
        base_task, kv_task, [target_layer], config
    )
    block_task = replace(
        base_task,
        block_size=max(1, base_task.block_size // 2)
        if base_task.block_size > 1
        else min(base_task.seq_len, base_task.block_size + 1),
    )
    block_result = run_block_size_change(
        base_task, block_task, [target_layer], config
    )
    return {
        "kv_content_change": kv_result.to_dict(),
        "block_size_change": block_result.to_dict(),
    }


def _demonstrate_inspection(config: RunConfig, case_id: str) -> dict:
    cache = ContentCache(config.cache_dir)
    entries = cache.inspect_node(case_id, block=0)
    return {
        "case_id": case_id,
        "block": 0,
        "entry_count": len(entries),
        "entries": entries,
    }


if __name__ == "__main__":
    sys.exit(main())
