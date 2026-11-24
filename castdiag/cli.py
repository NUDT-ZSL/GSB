"""命令行入口: python -m castdiag <command> ...

所有命令在输入非法时输出带 E_xxx 业务码的 JSON 错误并以非零码退出,
且不产生任何输出文件。
"""

from __future__ import annotations

import argparse
import json
import sys

from .errors import DiagError
from .generator import DEFAULT_SEED, generate
from .persist import recover
from .pipeline import InterruptFlag, prepare, run
from .verify import verify_replay
from .replay import sort_events


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="castdiag", description="投屏会话诊断工作台")
    sub = parser.add_subparsers(dest="command", required=True)
    p_gen = sub.add_parser("generate", help="生成固定种子确定性样本")
    p_gen.add_argument("--out", required=True, help="样本输出目录")
    p_gen.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p_val = sub.add_parser("validate", help="仅校验输入")
    p_val.add_argument("--data", required=True)
    p_merge = sub.add_parser("merge", help="合并两代快照并输出统一视图")
    p_merge.add_argument("--data", required=True)
    p_verify = sub.add_parser("verify", help="重放事件流并与合并快照比对")
    p_verify.add_argument("--data", required=True)
    p_run = sub.add_parser("run", help="完整诊断流程(含分块持久化)")
    p_run.add_argument("--data", required=True)
    p_run.add_argument("--state", required=True)
    p_run.add_argument("--workers", type=int, default=4)
    p_run.add_argument("--budget-bytes", type=int, default=64 * 1024 * 1024)
    p_run.add_argument("--chunks", type=int, default=4)
    p_rec = sub.add_parser("recover", help="仅从已持久化且摘要有效的分块恢复")
    p_rec.add_argument("--data", required=True)
    p_rec.add_argument("--state", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            _print({"ok": True, "result": generate(args.out, seed=args.seed)})
            return 0
        if args.command == "validate":
            prep = prepare(args.data)
            _print({"ok": True, "version_id": prep["version_id"],
                    "events": len(prep["events"]), "screens": sorted(prep["view"].keys())})
            return 0
        if args.command == "merge":
            prep = prepare(args.data)
            _print({"ok": True, "version_id": prep["version_id"],
                    "fallback": prep["fallback"],
                    "rejected_snapshots": prep["rejected_snapshots"],
                    "view": prep["view"], "conflicts": prep["conflicts"]})
            return 0
        if args.command == "verify":
            prep = prepare(args.data)
            report = verify_replay(prep["view"], sort_events(prep["events"]))
            _print(report)
            return 0 if report["ok"] else 1
        if args.command == "run":
            flag = InterruptFlag()
            flag.install()
            report = run(args.data, args.state, workers=args.workers,
                         budget_bytes=args.budget_bytes, chunk_count=args.chunks,
                         interrupt=flag)
            _print(report)
            return 0 if report["ok"] else 1
        if args.command == "recover":
            prep = prepare(args.data)
            bodies = recover(args.state, prep["version_id"])
            _print({"ok": True, "chunks_recovered": len(bodies),
                    "final_states": bodies[-1]["states"] if bodies else None})
            return 0
    except DiagError as exc:
        print(exc.to_json())
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
