"""GSB 命令行：load / merge / judge / resume / status / verify / patch / replace。"""
from __future__ import annotations

import argparse
import multiprocessing as mp
import sys
from concurrent.futures import ProcessPoolExecutor
from typing import Any, Dict, List, Optional

from .canon import pretty_json
from .engine import Engine, judge_process_entry
from .errors import GsbError


def _print(obj: Any) -> None:
    print(pretty_json(obj))


def _parallel_judge(engine_kwargs: Dict[str, Any], segment_ids: List[int],
                    workers: int) -> List[Dict[str, Any]]:
    chunks: List[List[int]] = [[] for _ in range(workers)]
    for i, sid in enumerate(segment_ids):
        chunks[i % workers].append(sid)
    tasks = [
        {**engine_kwargs, "segment_ids": chunk, "worker_id": f"parallel-{i}"}
        for i, chunk in enumerate(chunks) if chunk
    ]
    ctx = mp.get_context("fork")
    with ProcessPoolExecutor(max_workers=len(tasks), mp_context=ctx) as pool:
        return list(pool.map(judge_process_entry, tasks))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="gsb", description="数据库主从复制延迟诊断与归档水位裁决引擎")
    ap.add_argument("--samples", default="samples", help="样本目录（默认 samples）")
    ap.add_argument("--state", default="state", help="SQLite 状态目录（默认 state）")
    ap.add_argument("--now", type=int, default=None, help="注入逻辑时钟毫秒（默认取种子阈值）")
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("load", help="解析校验并固化位点流与策略摘要")
    m = sub.add_parser("merge", help="合并 v1/v2 策略并持久化合并修订")
    m.add_argument("--crash-phase", choices=["merge"], default=None,
                   help="在 merge 提交前注入强杀")

    j = sub.add_parser("judge", help="计算延迟/余量/精度敏感并裁决段状态")
    j.add_argument("--segment", type=int, action="append", dest="segments", help="只裁决指定段（可重复）")
    j.add_argument("--all", action="store_true", help="裁决全部段（默认补齐缺失段）")
    j.add_argument("--rev", type=int, default=None, help="策略修订号（默认当前活动）")
    j.add_argument("--worker-id", default=None)
    j.add_argument("--parallel", type=int, default=0, help="并行工作进程数")
    j.add_argument("--crash-after", type=int, default=None,
                   help="崩溃注入：N 个段提交后，在下一段 pending 时强杀")
    j.add_argument("--crash-phase", choices=["merge", "delay"], default=None,
                   help="在指定阶段注入强杀")
    j.add_argument("--slow-ms", type=int, default=0, help="每段提交前等待（SIGKILL 测试用）")

    r = sub.add_parser("resume", help="崩溃恢复：识别已提交/半提交并重放缺失项")
    r.add_argument("--worker-id", default=None)

    s = sub.add_parser("status", help="稳定排序 JSON：段状态与延迟证据")
    s.add_argument("--rev", type=int, default=None)

    v = sub.add_parser("verify", help="确定性重算并与持久化裁决核对")
    v.add_argument("--rev", type=int, default=None)

    p = sub.add_parser("patch", help="提交策略补丁并计算影响闭包")
    p.add_argument("--file", required=True)

    rep = sub.add_parser("replace", help="全量策略快照整体替换（含版本回退拒绝）")
    rep.add_argument("--file", required=True)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    engine = Engine(args.samples, args.state, now_override=args.now)
    try:
        if args.command == "load":
            _print(engine.cmd_load())
        elif args.command == "merge":
            _print(engine.cmd_merge(crash_phase=getattr(args, "crash_phase", None)))
        elif args.command == "judge":
            if args.parallel and args.parallel > 1 and not args.crash_after and not args.slow_ms:
                bundle, cfg = engine.load_inputs()
                sids = args.segments or [s.segment_id for s in bundle.segments]
                results = _parallel_judge(
                    {"samples_dir": args.samples, "state_dir": args.state,
                     "now_ms": args.now, "rev_no": args.rev},
                    sids, args.parallel,
                )
                _print({"ok": True, "command": "judge", "parallel": True, "workers": args.parallel,
                        "results": results})
            else:
                _print(engine.cmd_judge(
                    segment_ids=args.segments, rev_no=args.rev,
                    worker_id=args.worker_id, crash_after_commits=args.crash_after,
                    crash_phase=args.crash_phase, slow_ms=args.slow_ms,
                ))
        elif args.command == "resume":
            _print(engine.cmd_resume(worker_id=args.worker_id))
        elif args.command == "status":
            _print(engine.cmd_status(rev_no=args.rev))
        elif args.command == "verify":
            result = engine.cmd_verify(rev_no=args.rev)
            _print(result)
            return 0 if result["ok"] else 3
        elif args.command == "patch":
            _print(engine.cmd_patch(args.file))
        elif args.command == "replace":
            _print(engine.cmd_replace(args.file))
        return 0
    except GsbError as exc:
        print(pretty_json(exc.to_dict()), file=sys.stderr)
        return exc.exit_code
    finally:
        engine.close()


if __name__ == "__main__":
    raise SystemExit(main())
