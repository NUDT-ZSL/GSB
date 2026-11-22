from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

from .engine import Engine
from .errors import BizError, ErrorCode
from .models import load_samples
from .util import atomic_write_bytes, canonical_json


def _common_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--db",
        default="workbench/state.db",
        help="SQLite 状态数据库路径（默认 workbench/state.db）",
    )
    parser.add_argument(
        "--samples",
        default="samples",
        help="确定性样本目录（默认 samples）",
    )
    return parser


def build_parser() -> argparse.ArgumentParser:
    common = _common_parser()
    parser = argparse.ArgumentParser(
        prog="hotgray",
        description="本地模型分片热更新、依赖失效与灰度回退工作台",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", parents=[common], help="只读校验样本，不写数据库")
    validate.add_argument("--json", action="store_true", help="输出完整 JSON")

    subparsers.add_parser("init", parents=[common], help="校验并原子初始化 SQLite 状态")

    apply_patch = subparsers.add_parser("apply-patch", parents=[common], help="物化并提交增量补丁")
    apply_patch.add_argument("--at-ms", type=int, required=True)
    apply_patch.add_argument(
        "--crash-after-materialize",
        type=int,
        default=None,
        help="测试用：完成 N 个分片物化后强制退出（不生成退出报告）",
    )

    warmup = subparsers.add_parser("warmup", parents=[common], help="开始预热")
    warmup.add_argument("--at-ms", type=int, required=True)

    tick = subparsers.add_parser("tick", parents=[common], help="按逻辑时钟推进到期步骤")
    tick.add_argument("--at-ms", type=int, required=True)

    stage = subparsers.add_parser("stage", parents=[common], help="推进灰度档位")
    stage.add_argument("--at-ms", type=int, required=True)
    stage.add_argument("--level", type=int, default=None)

    request = subparsers.add_parser("request", parents=[common], help="绑定并开始推理请求")
    request.add_argument("--at-ms", type=int, required=True)
    request.add_argument("--replica-id", required=True)
    request.add_argument("--request-id", required=True)
    request.add_argument(
        "--input",
        default="[1.0, 0.5, -0.25, 0.125, 0.75]",
        help="JSON 数值数组或 {\"input\": [...]}",
    )

    complete = subparsers.add_parser("complete", parents=[common], help="完成已绑定推理请求")
    complete.add_argument("--at-ms", type=int, required=True)
    complete.add_argument("--request-id", required=True)

    abort = subparsers.add_parser("abort", parents=[common], help="人工中止并进入回退窗口")
    abort.add_argument("--at-ms", type=int, required=True)
    abort.add_argument("--reason", default="manual_abort")
    abort.add_argument("--replica-id", default=None)

    resume = subparsers.add_parser("resume", parents=[common], help="从持久化状态重放未完成步骤")
    resume.add_argument("--at-ms", type=int, default=None)

    run_clock = subparsers.add_parser("run-clock", parents=[common], help="执行注入逻辑时钟脚本")
    run_clock.add_argument("--scenario", default="normal")
    run_clock.add_argument("--output", default=None, help="可选：将执行结果写入 JSON 文件")

    status = subparsers.add_parser("status", parents=[common], help="稳定排序输出诊断快照")
    status.add_argument("--output", default=None)

    report = subparsers.add_parser("report", parents=[common], help="导出完整诊断报告")
    report.add_argument("--output", required=True)

    serve = subparsers.add_parser("serve", parents=[common], help="启动本地可视化诊断工作台")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8088)

    demo = subparsers.add_parser("demo", parents=[common], help="初始化并跑完整正常链路")
    demo.add_argument("--workspace", default="artifacts/demo-workbench")
    demo.add_argument("--reset", action="store_true")
    demo.add_argument("--output", default="artifacts/demo-report.json")
    return parser


def _print(payload: Any, *, compact: bool = False) -> None:
    if compact:
        print(canonical_json(payload))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _open_engine(args: argparse.Namespace) -> Engine:
    return Engine.open(args.db)


def _validate(args: argparse.Namespace) -> dict[str, Any]:
    samples = load_samples(args.samples)
    changed = [
        entry
        for entry in samples.patch.entries
    ]
    result = {
        "ok": True,
        "samples_root": str(samples.root),
        "manifest_digest": samples.manifest.digest,
        "versions": sorted(samples.manifest.models),
        "resources": sorted(samples.manifest.resources),
        "replicas": [replica.replica_id for replica in samples.replicas],
        "plan": {
            "round_id": samples.plan.round_id,
            "baseline_version": samples.plan.baseline_version,
            "target_version": samples.plan.target_version,
            "stages": [stage.as_dict() for stage in samples.plan.stages],
            "replica_ids": list(samples.plan.replica_ids),
        },
        "patch": {
            "patch_id": samples.patch.patch_id,
            "changed_shards": [
                {
                    "layer": entry.layer,
                    "shard_index": entry.shard_index,
                    "digest": entry.digest,
                }
                for entry in changed
            ],
        },
        "clock_scenarios": sorted(samples.clock_scenarios),
    }
    return result


def _init(args: argparse.Namespace) -> dict[str, Any]:
    db_path = Path(args.db)
    engine = Engine.initialize(
        workspace=db_path.parent,
        samples_dir=args.samples,
        db_name=db_path.name,
    )
    return {
        "ok": True,
        "db": str(engine.store.db_path),
        "round_id": engine.plan.round_id,
        "status": engine._read_round()["status"],
        "baseline_version": engine.plan.baseline_version,
        "target_version": engine.plan.target_version,
    }


def _request_input(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BizError(
            ErrorCode.INVALID_INPUT,
            f"--input 不是合法 JSON: {exc.msg}",
            {"actual": raw},
        ) from exc


def _demo(args: argparse.Namespace) -> dict[str, Any]:
    workspace = Path(args.workspace)
    if workspace.exists():
        if not args.reset:
            raise BizError(
                ErrorCode.CONFLICT,
                f"演示工作区已存在: {workspace}（使用 --reset 重建）",
                {"path": str(workspace)},
            )
        shutil.rmtree(workspace)
    engine = Engine.initialize(
        workspace=workspace,
        samples_dir=args.samples,
        db_name="state.db",
    )
    script = engine.execute_clock_script("normal")
    snapshot = engine.snapshot()
    report = {
        "ok": True,
        "workspace": str(workspace),
        "script_final_status": script["final_status"],
        "snapshot": snapshot,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(
        output,
        (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return {
        "ok": True,
        "workspace": str(workspace),
        "db": str(engine.store.db_path),
        "report": str(output),
        "status": snapshot["round"]["status"],
        "current_level": snapshot["round"]["current_level"],
        "current_ratio": snapshot["round"]["current_ratio"],
        "requests": [
            {
                "request_id": item["request_id"],
                "replica_id": item["replica_id"],
                "version": item["version"],
                "status": item["status"],
                "result_digest": item["result_digest"],
            }
            for item in snapshot["requests"]
        ],
        "materializations": snapshot["materializations"]["count"],
    }


def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "validate":
        return _validate(args)
    if args.command == "init":
        return _init(args)
    if args.command == "demo":
        return _demo(args)
    if args.command == "serve":
        from .server import serve

        serve(args.db, host=args.host, port=args.port)
        return {"ok": True, "stopped": True}

    engine = _open_engine(args)
    if args.command == "apply-patch":
        return engine.apply_patch(
            at_ms=args.at_ms,
            crash_after_materialize=args.crash_after_materialize,
        )
    if args.command == "warmup":
        return engine.start_warmup(at_ms=args.at_ms)
    if args.command == "tick":
        return engine.tick(at_ms=args.at_ms)
    if args.command == "stage":
        return engine.advance_stage(at_ms=args.at_ms, level=args.level)
    if args.command == "request":
        return engine.begin_request(
            replica_id=args.replica_id,
            request_id=args.request_id,
            input_payload=_request_input(args.input),
            at_ms=args.at_ms,
        )
    if args.command == "complete":
        return engine.complete_request(request_id=args.request_id, at_ms=args.at_ms)
    if args.command == "abort":
        return engine.trigger_rollback(
            at_ms=args.at_ms,
            reason=args.reason,
            replica_id=args.replica_id,
        )
    if args.command == "resume":
        return engine.resume(at_ms=args.at_ms)
    if args.command == "run-clock":
        result = engine.execute_clock_script(args.scenario)
        if args.output:
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(
                output,
                (json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
                    "utf-8"
                ),
            )
        return result
    if args.command == "status":
        result = engine.snapshot()
        if args.output:
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(
                output,
                (json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
                    "utf-8"
                ),
            )
        return result
    if args.command == "report":
        result = engine.snapshot()
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(
            output,
            (json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
        return {"ok": True, "output": str(output), "round_status": result["round"]["status"]}
    raise BizError(
        ErrorCode.INVALID_INPUT,
        f"未知命令: {args.command}",
        {"actual": args.command},
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = dispatch(args)
    except BizError as exc:
        _print(exc.as_dict())
        return 2
    except KeyboardInterrupt:
        _print({"ok": False, "error": {"code": "HG-INTERRUPTED", "message": "操作被中断"}})
        return 130
    _print(result, compact=bool(getattr(args, "json", False)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
