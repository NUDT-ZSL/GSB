"""命令行入口: 运行热更新会话 / 校验 / 快照 / 重放 / 异常注入。"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .center import ConfigCenter
from .errors import BizError, E_CONFIG, E_PUSH, E_SUB
from .jlog import JsonlLogger
from .latency import KIND_QUERY
from .loader import TaskSpec, load_config_items, load_sample_series, load_task_specs
from .replay import replay_snapshot
from .snapshot import restore_session, save_snapshot
from .tasks import ComputeTask
from .verify import verify_results

DEFAULT_LOG = "logs/session.jsonl"


def _print(doc) -> None:
    print(json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True))


def _build_session(args):
    log = JsonlLogger(getattr(args, "log", DEFAULT_LOG))
    items = load_config_items(args.config)
    specs = load_task_specs(args.tasks, items)
    series = load_sample_series(args.data, items)
    center = ConfigCenter(items, logger=log)
    tasks = {}
    for spec in specs:
        task = ComputeTask(spec, series)
        tasks[spec.id] = task
        center.subscribe(spec.id, spec.keys, center.current_version, handler=task)
    return center, tasks, series, log


def _round_changes(items, round_idx: int) -> dict:
    """确定性地生成第 round_idx 轮变更 (不依赖随机源)。"""
    keys = sorted(items)
    changes = {}
    for offset, key in enumerate(keys[:2]):
        item = items[key]
        if item.type == "int":
            changes[key] = int(item.value) + round_idx + offset
        else:
            changes[key] = round(float(item.value) + 0.1 * (round_idx + offset), 6)
    return changes


def cmd_run(args) -> dict:
    center, tasks, series, log = _build_session(args)
    results = []
    with ThreadPoolExecutor(max_workers=max(1, len(tasks))) as pool:
        for round_idx in range(1, args.commits + 1):
            version = center.commit_changes(_round_changes(center._items, round_idx))
            futures = []
            for tid, task in tasks.items():
                center.update_subscription(tid, version)
                object.__setattr__(task.spec, "expected_version", version)
                futures.append(pool.submit(task.run, center, version))
                for _ in range(args.queries):
                    center.query(tid, version, keys=task.spec.keys)
            results.extend(f.result() for f in futures)
    verification = verify_results(center, tasks, results)
    try:
        latency_stats = center.observer.stats_json(group_version=False)
    except BizError as exc:
        latency_stats = {"error": exc.to_dict()}
    out = {
        "current_version": center.current_version,
        "chain_digest": center.chain_digest(),
        "results": results,
        "verification": verification,
        "latency_stats": latency_stats,
        "push_errors": center.push_errors,
    }
    if args.snapshot_out:
        save_snapshot(center, results, args.snapshot_out)
        out["snapshot"] = args.snapshot_out
    log.emit("run_done", current_version=center.current_version,
             results=len(results))
    return out


def cmd_verify(args) -> dict:
    center, results, _ = restore_session(args.snapshot, None)
    series = load_sample_series(args.data, center._items)
    tasks = {}
    for tid, sub in center.subscriptions().items():
        agg = next((r["aggregation"] for r in results if r["task_id"] == tid), "sum")
        tasks[tid] = ComputeTask(TaskSpec(id=tid, keys=tuple(sub["keys"]),
                                          aggregation=agg), series)
    return verify_results(center, tasks, results)


def cmd_restore(args) -> dict:
    # 当前会话: 由配置文件全新加载 (版本链只有 v1)
    fresh_items = load_config_items(args.config)
    fresh_center = ConfigCenter(fresh_items)
    center, results, replaced = restore_session(args.snapshot, fresh_center)
    out = {
        "replaced": replaced,
        "current_version": center.current_version,
        "chain_digest": center.chain_digest(),
        "results": len(results),
    }
    for round_idx in range(1, args.continue_commits + 1):
        version = center.commit_changes(_round_changes(center._items, 100 + round_idx))
        out["current_version"] = version
    out["continued_commits"] = args.continue_commits
    return out


def cmd_replay(args) -> dict:
    items = load_config_items(args.config)
    series = load_sample_series(args.data, items)
    return replay_snapshot(args.snapshot, series)


def cmd_inject(args) -> dict:
    log = JsonlLogger(args.log)
    if args.kind == "corrupt-config":
        # 仅改变输入文件: 截断配置文件制造损坏
        p = Path(args.config)
        text = p.read_text(encoding="utf-8")
        p.write_text(text[: max(1, len(text) // 2)], encoding="utf-8")
        try:
            load_config_items(p)
        except BizError as exc:
            log.emit("inject", kind=args.kind, error=exc.to_dict())
            return {"injected": args.kind, "error": exc.to_dict()}
        raise AssertionError("corrupt config unexpectedly loaded")
    if args.kind == "missing-sub-ref":
        center, tasks, series, _log = _build_session(args)
        try:
            center.subscribe("ghost_task", ["no.such.key"], center.current_version)
        except BizError as exc:
            if exc.code != E_SUB:
                raise
            log.emit("inject", kind=args.kind, error=exc.to_dict())
            return {"injected": args.kind, "error": exc.to_dict()}
        raise AssertionError("missing subscription reference unexpectedly accepted")
    if args.kind == "push-timeout":
        center, tasks, series, _log = _build_session(args)
        center.ack_timeout_ms = args.ack_timeout_ms
        for tid in tasks:
            center.slow_push_ms[tid] = args.slow_ms
        center.commit_changes(_round_changes(center._items, 1))
        errors = center.push_errors
        if not errors:
            raise AssertionError("push timeout was not triggered")
        for err in errors:
            log.emit("inject", kind=args.kind, error=err["error"])
        return {"injected": args.kind, "push_errors": errors}
    raise BizError(E_CONFIG, f"unknown injection kind: {args.kind}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hotcfg", description=__doc__)
    parser.add_argument("--log", default=DEFAULT_LOG, help="结构化日志路径")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_io(p):
        p.add_argument("--config", default="samples/config.json")
        p.add_argument("--tasks", default="samples/tasks.json")
        p.add_argument("--data", default="samples/data.json")

    p_run = sub.add_parser("run", help="运行一次完整热更新会话")
    add_io(p_run)
    p_run.add_argument("--commits", type=int, default=4)
    p_run.add_argument("--queries", type=int, default=5)
    p_run.add_argument("--snapshot-out", default=None)
    p_run.set_defaults(func=cmd_run)

    p_verify = sub.add_parser("verify", help="校验快照中结果与版本的一致性")
    p_verify.add_argument("--snapshot", required=True)
    p_verify.add_argument("--data", default="samples/data.json")
    p_verify.set_defaults(func=cmd_verify)

    p_restore = sub.add_parser("restore", help="重开快照, 版本不一致时整体替换")
    p_restore.add_argument("--snapshot", required=True)
    p_restore.add_argument("--config", default="samples/config.json")
    p_restore.add_argument("--continue-commits", type=int, default=2)
    p_restore.set_defaults(func=cmd_restore)

    p_replay = sub.add_parser("replay", help="从快照重放并校验可复现性")
    p_replay.add_argument("--snapshot", required=True)
    p_replay.add_argument("--config", default="samples/config.json")
    p_replay.add_argument("--data", default="samples/data.json")
    p_replay.set_defaults(func=cmd_replay)

    p_inject = sub.add_parser("inject", help="注入异常: corrupt-config | missing-sub-ref | push-timeout")
    p_inject.add_argument("kind", choices=["corrupt-config", "missing-sub-ref", "push-timeout"])
    add_io(p_inject)
    p_inject.add_argument("--ack-timeout-ms", type=float, default=1.0)
    p_inject.add_argument("--slow-ms", type=float, default=20.0)
    p_inject.set_defaults(func=cmd_inject)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        out = args.func(args)
    except BizError as exc:
        _print({"error": exc.to_dict()})
        return 1
    _print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
